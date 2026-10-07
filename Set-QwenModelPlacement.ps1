# Keep one model on the CPU for every caller (Open WebUI, the router, RAG embeddings, the
# video worker, OpenClaw), or give it back to the automatic GPU choice. Added 2026-09-29.
#
#   Set-QwenModelPlacement.ps1                                   show every model's placement
#   Set-QwenModelPlacement.ps1 -Model qwen3-embedding:4b -Placement cpu [-NumCtx 8192]
#   Set-QwenModelPlacement.ps1 -Model qwen3-embedding:4b -Placement auto
#   -Json  machine-readable output (web page, chat tool)
#
# Why the model itself is changed: several callers talk to Ollama directly (Open WebUI's
# RAG embeddings go to :11434), so only a model parameter reaches all of them. "cpu" asks
# Ollama to re-create the same tag with PARAMETER num_gpu 0 (weights are shared, nothing is
# copied); Ollama then loads it with no GPU backend at all, and - measured in the 0.32.15
# source - skips the VRAM check that made the embedding model evict qwen3.8 on every RAG
# lookup. The price: no GPU help at all for that model (it reads prompts much more slowly).
#
# Exactly reversible: before the change the manifest and every small blob it points to
# (config, params, template, license - never the weights) are copied to
# data\placement-backups\<model>\<time>\ ; "auto" puts them back byte for byte, so the model
# digest returns to the original. Ollama itself deletes the old small blobs when it re-creates
# the tag, which is why the backup lives outside the models folder.
#
# Per-model placement is independent of the server-level choice (Select-QwenCompute.ps1) and
# needs no Ollama restart. The state is in data\model-placement.json. An "ollama pull" or a
# reinstall of the model silently undoes a pin; Test-QwenGpu.ps1 reports that.

param(
    [string]$Model,
    [ValidateSet('status', 'cpu', 'auto')][string]$Placement = 'status',
    [int]$NumCtx = 0,
    [switch]$Json,
    [string]$By = 'Set-QwenModelPlacement.ps1'
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'Resolve-OllamaGpu.ps1')
$Api = 'http://127.0.0.1:11434'
$StatePath = Join-Path $Root 'data\model-placement.json'
$BackupRoot = Join-Path $Root 'data\placement-backups'
$WeightTypes = '\.(model|projector|adapter|draft)$'
# The models the router sends chats to: pinning one of these makes every chat slow.
$MainModels = @('qwen3.8:27b', 'qwen3-coder:30b', 'muse-glimmer:30b', 'granite4.2:8b')

function Invoke-OllamaJson([string]$Path, $Body, [int]$TimeoutSec = 30) {
    if ($null -eq $Body) { return Invoke-RestMethod "$Api$Path" -TimeoutSec $TimeoutSec }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 6 -Compress))
    return Invoke-RestMethod "$Api$Path" -Method Post -Body $bytes -ContentType 'application/json' -TimeoutSec $TimeoutSec
}

function Get-ModelsDir {
    $dir = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
    if (-not $dir) { $dir = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'Machine') }
    if (-not $dir) { $dir = Join-Path $env:USERPROFILE '.ollama\models' }
    return $dir
}

function Get-ManifestPath([string]$Name) {
    # name[:tag]; "library" is the namespace of plain names such as qwen3:4b.
    $tag = 'latest'
    $base = $Name
    if ($Name -match '^(.+):([^:/]+)$') { $base = $Matches[1]; $tag = $Matches[2] }
    if ($base -match '[\\]|\.\.' -or $tag -match '[\\/]|\.\.') { throw "invalid model name '$Name'" }
    $parts = @($base -split '/')
    $rel = if ($parts.Count -eq 1) { "registry.ollama.ai\library\$base" } elseif ($parts.Count -eq 2) { "registry.ollama.ai\$($parts[0])\$($parts[1])" } else { $parts -join '\' }
    return (Join-Path (Get-ModelsDir) "manifests\$rel\$tag")
}

function Get-BlobPath([string]$Digest) { return (Join-Path (Get-ModelsDir) ('blobs\' + ($Digest -replace ':', '-'))) }

function Get-Sha256([string]$Path) { return 'sha256:' + (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) { return [ordered]@{ version = 1; models = [ordered]@{} } }
    $raw = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $state = [ordered]@{ version = 1; models = [ordered]@{} }
    if ($raw.models) { foreach ($p in $raw.models.PSObject.Properties) { $state.models[$p.Name] = $p.Value } }
    return $state
}

function Save-State($state) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $StatePath) | Out-Null
    if (-not (Write-QwenGpuProfileText -Path $StatePath -Text (ConvertTo-Json -InputObject $state -Depth 6) -KeepOnFailure)) { throw "could not write $StatePath" }
}

function Get-TagsByName {
    $tags = @{}
    foreach ($m in @((Invoke-OllamaJson '/api/tags' $null).models)) { $tags[[string]$m.name] = $m }
    return $tags
}

function Get-OllamaExe {
    $candidates = @((Join-Path $Root 'runtime\ollama\ollama.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'))
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    return ($candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1)
}

function Stop-LoadedModel([string]$Name) {
    # A loaded runner keeps its launch options; it has to go for the change to show. Unload
    # through the API (what "ollama stop" does) - the CLI writes escape codes to stderr,
    # which PowerShell 5.1 turns into an error.
    try {
        $loaded = @((Invoke-OllamaJson '/api/ps' $null).models | Where-Object { $_.name -eq $Name })
        if ($loaded.Count -eq 0) { return 'not loaded' }
        [void](Invoke-OllamaJson '/api/generate' @{ model = $Name; keep_alive = 0 } 60)
        return 'unloaded'
    } catch {
        return 'still loaded until its keep-alive ends'
    }
}

function Get-Placement($state, $tags, [string]$Name) {
    $entry = $state.models[$Name]
    $tag = $tags[$Name]
    if (-not $tag) { return [pscustomobject]@{ model = $Name; placement = 'missing'; note = 'model not installed' } }
    $digest = 'sha256:' + [string]$tag.digest
    if ($entry -and $entry.placement -eq 'cpu') {
        if ($entry.pending) { return [pscustomobject]@{ model = $Name; placement = 'pending'; note = 'a change was interrupted; run -Placement auto to restore the original' } }
        if ($digest -eq [string]$entry.pinned_digest) { return [pscustomobject]@{ model = $Name; placement = 'cpu'; note = $(if ($entry.num_ctx) { "num_ctx $($entry.num_ctx)" } else { '' }) } }
        return [pscustomobject]@{ model = $Name; placement = 'lost'; note = 'the CPU pin was undone (model re-pulled or reinstalled); set it again or run -Placement auto to clear this' }
    }
    return [pscustomobject]@{ model = $Name; placement = 'auto'; note = '' }
}

function Get-ModelParams([string]$Name) {
    $params = @{}
    $show = Invoke-OllamaJson '/api/show' @{ model = $Name }
    foreach ($line in ([string]$show.parameters -split "`r?`n")) {
        if ($line -match '^\s*(\S+)\s+(.+?)\s*$') { $params[$Matches[1]] = $Matches[2].Trim('"') }
    }
    return $params
}

function Restore-FromBackup([string]$Name, [string]$BackupDir) {
    # Put the backed-up manifest (and any small blob Ollama deleted) back, byte for byte.
    $manifestBackup = Join-Path $BackupDir 'manifest'
    if (-not (Test-Path -LiteralPath $manifestBackup)) { throw "backup missing: $manifestBackup" }
    $manifestPath = Get-ManifestPath $Name
    $manifest = Get-Content -LiteralPath $manifestBackup -Raw | ConvertFrom-Json
    # Hold the server-start lock: a starting server prunes blobs nothing references.
    $lock = New-Object System.Threading.Mutex($false, 'Local\QwenSuite-Ollama')
    $owns = $false
    try {
        try { $owns = $lock.WaitOne(120000) } catch [System.Threading.AbandonedMutexException] { $owns = $true }
        if (-not $owns) { throw 'an Ollama start is in progress; try again in a minute' }
        foreach ($layer in (@($manifest.config) + @($manifest.layers))) {
            if (-not $layer.digest) { continue }
            $blob = Get-BlobPath $layer.digest
            if (Test-Path -LiteralPath $blob) { continue }
            if ($layer.mediaType -match $WeightTypes) { throw "weights $($layer.digest) of $Name are missing; cannot restore" }
            $saved = Join-Path $BackupDir ('blobs\' + (Split-Path -Leaf $blob))
            if ((Get-Sha256 $saved) -ne [string]$layer.digest) { throw "backup blob $saved is damaged" }
            Copy-Item -LiteralPath $saved -Destination $blob
        }
        # Atomic replace from a temp file on the manifest's own volume, outside manifests\
        # and blobs\ (Ollama parses the one and prunes the other).
        $tmp = Join-Path (Get-ModelsDir) ('placement-restore-' + [guid]::NewGuid().ToString('N'))
        Copy-Item -LiteralPath $manifestBackup -Destination $tmp
        try {
            if (Test-Path -LiteralPath $manifestPath) { [System.IO.File]::Replace($tmp, $manifestPath, [NullString]::Value) }
            else { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $manifestPath) | Out-Null; Move-Item -LiteralPath $tmp -Destination $manifestPath }
        } finally {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    } finally {
        if ($owns) { try { $lock.ReleaseMutex() } catch { } }
        $lock.Dispose()
    }
}

function New-Backup([string]$Name, [string]$ManifestPath, [string]$Digest) {
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $backupDir = Join-Path $BackupRoot (($Name -replace '[^A-Za-z0-9._-]', '_') + '\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force -Path (Join-Path $backupDir 'blobs') | Out-Null
    Copy-Item -LiteralPath $ManifestPath -Destination (Join-Path $backupDir 'manifest')
    $small = @($manifest.config) + @($manifest.layers | Where-Object { $_.mediaType -notmatch $WeightTypes })
    foreach ($layer in $small) {
        if (-not $layer.digest) { continue }
        $blob = Get-BlobPath $layer.digest
        if (-not (Test-Path -LiteralPath $blob)) { throw "blob $($layer.digest) of $Name is missing; not touching it" }
        Copy-Item -LiteralPath $blob -Destination (Join-Path $backupDir ('blobs\' + (Split-Path -Leaf $blob)))
    }
    @{ model = $Name; original_digest = $Digest; manifest_path = $ManifestPath; at = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupDir 'record.json') -Encoding UTF8
    return $backupDir
}

function Invoke-Pin([string]$Name, $state, $tags) {
    $tag = $tags[$Name]
    if (-not $tag) { throw "model '$Name' is not installed (see Set-QwenModelPlacement.ps1 -Json for the list)" }
    $manifestPath = Get-ManifestPath $Name
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw "manifest not found: $manifestPath" }
    $digest = 'sha256:' + [string]$tag.digest
    if ((Get-Sha256 $manifestPath) -ne $digest) { throw "the manifest on disk does not match what Ollama reports for $Name; not touching it" }
    if ($NumCtx -gt 0 -and $MainModels -contains $Name) {
        # A num_ctx in the model reaches only the callers that send none; the router, OpenClaw
        # and the Open WebUI presets send 32768, so the two kinds would reload each other.
        throw "-NumCtx is only for models whose callers all leave num_ctx out (such as qwen3-embedding:4b); $Name is also called with num_ctx 32768, and the two would make Ollama reload it on every switch"
    }
    $existing = $state.models[$Name]
    $current = ($existing -and $existing.placement -eq 'cpu' -and -not $existing.pending -and [string]$existing.pinned_digest -eq $digest)
    if ($current -and ([int]$existing.requested_ctx -eq $NumCtx)) {
        return @{ status = 'unchanged'; message = "$Name already runs on the CPU." }
    }
    if ($existing -and $existing.pending) { throw "an earlier change of $Name was interrupted; run -Placement auto first to restore the original" }
    if (-not $current -and (Get-ModelParams $Name).ContainsKey('num_gpu')) {
        throw "$Name already carries a num_gpu setting that this tool did not make; not touching it"
    }
    if ($current) {
        $originalDigest = [string]$existing.original_digest
        $backupDir = [string]$existing.backup
    } else {
        $originalDigest = $digest
        $backupDir = New-Backup $Name $manifestPath $digest
    }
    # Recorded before touching the model, so an interruption can always be undone with 'auto'.
    $state.models[$Name] = [ordered]@{
        placement = 'cpu'; pending = $true; num_ctx = $null; requested_ctx = $NumCtx
        original_digest = $originalDigest; pinned_digest = $null; backup = $backupDir
        updated_at = (Get-Date).ToString('o'); updated_by = $By
    }
    Save-State $state
    # Different num_ctx: start again from the original so no old value survives (Ollama
    # merges parameters and never drops one).
    if ($current) { Restore-FromBackup $Name $backupDir }
    $parameters = @{ num_gpu = 0 }
    if ($NumCtx -gt 0) { $parameters.num_ctx = $NumCtx }
    [void](Invoke-OllamaJson '/api/create' @{ model = $Name; from = $Name; parameters = $parameters; stream = $false } 180)
    $params = Get-ModelParams $Name
    if ([string]$params['num_gpu'] -ne '0') { throw "Ollama did not keep num_gpu 0 for $Name; run -Placement auto to restore the original" }
    $ctxNow = if ($params.ContainsKey('num_ctx')) { [int]$params['num_ctx'] } else { $null }
    $newDigest = 'sha256:' + [string]((Get-TagsByName)[$Name].digest)
    $state.models[$Name] = [ordered]@{
        placement = 'cpu'; pending = $false; num_ctx = $ctxNow; requested_ctx = $NumCtx
        original_digest = $originalDigest; pinned_digest = $newDigest; backup = $backupDir
        updated_at = (Get-Date).ToString('o'); updated_by = $By
    }
    Save-State $state
    Write-QwenGpuLog -Root $Root -Message ("placement: {0} -> cpu (num_ctx {1}) by {2}; backup {3}" -f $Name, $ctxNow, $By, $backupDir)
    $warn = @()
    if ($MainModels -contains $Name) { $warn += "$Name is a model the router sends chats to: on the CPU alone it is usually much slower, especially at reading long prompts." }
    $twins = @($tags.Keys | Where-Object { $_ -ne $Name } | Where-Object {
        try { $o = Get-Content -LiteralPath (Get-ManifestPath $_) -Raw | ConvertFrom-Json; $a = Get-Content -LiteralPath (Get-ManifestPath $Name) -Raw | ConvertFrom-Json
              (@($o.layers | Where-Object { $_.mediaType -match '\.model$' })[0].digest) -eq (@($a.layers | Where-Object { $_.mediaType -match '\.model$' })[0].digest) } catch { $false } })
    if ($twins.Count -gt 0) { $warn += ("{0} shares its weights with {1}; requests that alternate between them reload the model each time." -f $Name, ($twins -join ', ')) }
    $ctxText = if ($ctxNow) { " with num_ctx $ctxNow" } else { '' }
    return @{ status = 'pinned'; message = "$Name now runs on the CPU for every caller$ctxText (the next request loads it there)."; warnings = $warn }
}

function Invoke-Unpin([string]$Name, $state, $tags) {
    $entry = $state.models[$Name]
    if (-not $entry -or $entry.placement -ne 'cpu') { return @{ status = 'unchanged'; message = "$Name is not pinned to the CPU." } }
    $now = if ($tags[$Name]) { 'sha256:' + [string]$tags[$Name].digest } else { '' }
    if (-not $now) {
        $state.models.Remove($Name)
        Save-State $state
        Write-QwenGpuLog -Root $Root -Message ("placement: {0} is no longer installed; pin forgotten by {1}" -f $Name, $By)
        return @{ status = 'cleared'; message = "$Name is no longer installed; its CPU pin was forgotten." }
    }
    if (-not $entry.pending -and $now -ne [string]$entry.pinned_digest) {
        # Someone re-pulled or reinstalled the model: the pin is gone already, and restoring
        # the old manifest would roll that update back. Just forget the pin.
        $state.models.Remove($Name)
        Save-State $state
        Write-QwenGpuLog -Root $Root -Message ("placement: {0} pin already undone (digest {1}); state cleared by {2}" -f $Name, $now, $By)
        return @{ status = 'cleared'; message = "The CPU pin of $Name had already been undone (model re-pulled or reinstalled); nothing restored." }
    }
    Restore-FromBackup $Name ([string]$entry.backup)
    $after = 'sha256:' + [string]((Get-TagsByName)[$Name].digest)
    if ($after -ne [string]$entry.original_digest) { throw "restored, but Ollama reports digest $after instead of $($entry.original_digest)" }
    $state.models.Remove($Name)
    Save-State $state
    $unload = Stop-LoadedModel $Name
    Write-QwenGpuLog -Root $Root -Message ("placement: {0} -> auto by {1}; restored {2}" -f $Name, $By, $entry.original_digest)
    return @{ status = 'restored'; message = "$Name follows the automatic GPU choice again (original model restored, $unload)." }
}

$result = @{ status = 'listed'; message = '' }
$exit = 0
$placementLock = New-Object System.Threading.Mutex($false, 'Local\QwenSuite-ModelPlacement')
$ownsPlacement = $false
try {
    if ($Placement -ne 'status') {
        if (-not $Model) { throw 'give -Model <name>' }
        try { $ownsPlacement = $placementLock.WaitOne(30000) } catch [System.Threading.AbandonedMutexException] { $ownsPlacement = $true }
        if (-not $ownsPlacement) { throw 'another placement change is running; try again' }
        $state = Read-State
        $tags = Get-TagsByName
        $result = if ($Placement -eq 'cpu') { Invoke-Pin $Model $state $tags } else { Invoke-Unpin $Model $state $tags }
    }
} catch {
    $result = @{ status = 'error'; message = $_.Exception.Message }
    $exit = 1
} finally {
    if ($ownsPlacement) { try { $placementLock.ReleaseMutex() } catch { } }
    $placementLock.Dispose()
}

$rows = @()
try {
    $state = Read-State
    $tags = Get-TagsByName
    $rows = @($tags.Keys | Sort-Object | ForEach-Object { Get-Placement $state $tags $_ })
} catch {
    if ($result.status -eq 'listed') { $result = @{ status = 'error'; message = 'Ollama is not answering: ' + $_.Exception.Message }; $exit = 1 }
}
if ($Json) {
    [pscustomobject]@{ status = $result.status; message = $result.message; warnings = @($result.warnings | Where-Object { $_ }); models = $rows } | ConvertTo-Json -Depth 5 -Compress
} else {
    Write-Host ''
    Write-Host 'Where each model runs' -ForegroundColor Cyan
    foreach ($r in $rows) {
        $color = switch ($r.placement) { 'cpu' { 'Yellow' } 'lost' { 'Red' } 'pending' { 'Red' } default { 'White' } }
        Write-Host ('  {0,-26} {1,-7} {2}' -f $r.model, $r.placement, $r.note) -ForegroundColor $color
    }
    if ($result.message) { Write-Host ''; Write-Host ('  ' + $result.message) -ForegroundColor $(if ($exit) { 'Red' } else { 'Green' }) }
    foreach ($w in @($result.warnings)) { if ($w) { Write-Host ('  NOTE: ' + $w) -ForegroundColor Yellow } }
    Write-Host ''
}
exit $exit
