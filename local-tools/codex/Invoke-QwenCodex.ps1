# Codex-Qwen bridge: runs the installed Codex CLI harness on the local Ollama model as an
# unattended job (-Exec, -Review), or opens its interactive TUI (-Tui). Added 2026-09-30.
#
# What every run guarantees (details in INSTRUCTIONS.md next to this file):
#  - Its own CODEX_HOME, <suite>\data\codex-home, rebuilt from the templates next to this
#    file before every run. C:\Users\EGAT\.codex is never read or written.
#  - The provider is Ollama at 127.0.0.1:11434/v1. The Codex process gets a dead proxy
#    (127.0.0.1:9) for everything that is not 127.0.0.1, so it cannot reach the internet.
#    Never --oss: that path can pull models from the internet.
#  - One bridge job at a time (file lock data\codex-home\run.lock), and only when Ollama is
#    idle: no runner slot processing a request, and no OTHER model used in the last few
#    minutes - loading qwen3-coder evicts the chat model, which then has to read its
#    ~20k-token prompt again (13-23 min on this CPU).
#  - Default job mode is read-only. With no Windows sandbox configured, Codex 0.146 then
#    runs only its known-safe read commands (Get-ChildItem, Get-Content, git status...)
#    and rejects everything else as "blocked by policy" - verified 2026-09-30 with a mock
#    model. Write mode needs "write_mode" in data\codex-bridge.json: the owner decides
#    between Codex's Windows sandbox (changes folder ACLs) and full user rights.
#  - Exec rules (qwen-bridge.rules) hard-block, by command name, the credential-store tools
#    (cmdkey, vaultcmd ...), services, installs, network tools and nested shells, with a
#    Thai reason the model sees. They do NOT stop reads: a read-only job can Get-Content any
#    file the user can, credential files included. list_codex_jobs therefore hides the answer
#    of a job whose commands touched a secret folder and masks secret-looking strings.
#  - Policy hook (2026-09-30): Codex runs policy_hook.py before every tool call; it applies the
#    gateway's command_policy (run_command's hard blocks, secret folders included) and denies
#    with a Thai reason. Codex skips an untrusted hook and runs the command when a hook fails,
#    so before every job the bridge runs the hook's self-test and checks through
#    `codex app-server` hooks/list that Codex sees it as trusted (Confirm-PolicyHook); a write
#    job is refused when that fails. Every job folder is written into config.toml as
#    trust_level = "untrusted": a repo's own .codex\config.toml would otherwise load in a
#    full-access run and could switch hooks off (verified). --enable hooks is passed as well.
#  - -Source openclaw|webui|mcp marks a job started from chat (codex_dispatch.py): its end
#    notification goes where notifier.resolve_channels sends chat tasks.
#  - At the end our model is unloaded (only if it is loaded, and only if this run started
#    Codex - a refused or timed-out run never evicts the model another job or chat is using)
#    and notifier.py is called.
#
# Exit codes: 0 ok, 1 job failed, 2 setup or argument error, 3 Ollama busy too long,
# 4 another bridge job is running, 5 write mode not enabled, 6 answer does not match
# -OutputSchema, 7 timed out, 8 review diff too large.
#
# Test hooks (environment): QWEN_CODEX_EXE, QWEN_CODEX_ROOT, QWEN_CODEX_OLLAMA_URL,
# QWEN_CODEX_SLOTS_URLS, QWEN_CODEX_PROJECTS_ROOT, QWEN_CODEX_PYTHON, QWEN_CODEX_POLL_SECONDS.

[CmdletBinding()]
param(
    [switch]$Exec,
    [switch]$Review,
    [switch]$Tui,
    [switch]$Check,
    [switch]$List,
    [string]$Prompt = '',
    [string]$PromptFile = '',
    [string]$Cwd = '',
    [string[]]$AddDir = @(),
    [string]$Model = '',
    [switch]$ReadOnly,
    [switch]$Write,
    [string]$OutputSchema = '',
    [switch]$Ephemeral,
    [switch]$ResumeLast,
    [string]$ResumeId = '',
    [switch]$JsonOut,
    [switch]$Uncommitted,
    [string]$Base = '',
    [string]$Commit = '',
    [switch]$Docx,
    [string]$Title = '',
    [int]$IdleWaitMinutes = -1,
    [int]$MaxMinutes = 0,
    [switch]$NoNotify,
    [switch]$NotifyDryRun,
    [switch]$KeepModel,
    [switch]$Detach,
    [string]$JobId = '',
    [string[]]$Config = @(),
    [ValidateSet('', 'desktop', 'webui', 'openclaw', 'mcp')]
    [string]$Source = ''
)

# Continue, not Stop: in Windows PowerShell 5.1 a native command that writes to stderr
# (codex, git) would otherwise throw. Failures are checked explicitly instead.
$ErrorActionPreference = 'Continue'

# ------------------------------------------------------------------------------ paths
$ToolsDir = Split-Path -Parent $PSScriptRoot
$Root = if ($env:QWEN_CODEX_ROOT) { $env:QWEN_CODEX_ROOT.TrimEnd('\') } else { Split-Path -Parent $ToolsDir }
$HelperToolsDir = Join-Path $Root 'local-tools'
$CodexHome = Join-Path $Root 'data\codex-home'
$LogDir = Join-Path $Root 'logs\codex-bridge'
$SettingsPath = Join-Path $Root 'data\codex-bridge.json'
$ProjectsRoot = if ($env:QWEN_CODEX_PROJECTS_ROOT) { $env:QWEN_CODEX_PROJECTS_ROOT.TrimEnd('\') } else { 'E:\qwen-projects' }
$DefaultOllama = 'http://127.0.0.1:11434'
$OllamaUrl = if ($env:QWEN_CODEX_OLLAMA_URL) { $env:QWEN_CODEX_OLLAMA_URL.TrimEnd('/') } else { $DefaultOllama }
$PollSeconds = 60
if ($env:QWEN_CODEX_POLL_SECONDS) { $PollSeconds = [Math]::Max(1, [int]$env:QWEN_CODEX_POLL_SECONDS) }
$Helper = Join-Path $PSScriptRoot 'codex_jobs.py'
$PolicyHookScript = Join-Path $PSScriptRoot 'policy_hook.py'
# Codex's own hash of our hook as last read through hooks/list (see Confirm-PolicyHook).
$PolicyTrustFile = Join-Path $CodexHome 'policy-hook.trust.json'
$script:HookRunDir = ''
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$KnownModels = @('qwen3-coder:30b', 'gpt-oss:20b')
$script:CurrentJobId = $JobId

if ($JsonOut) {
    # A caller that parses stdout gets UTF-8 JSON and nothing else.
    try { [Console]::OutputEncoding = $Utf8 } catch { }
}

function Say([string]$Message) {
    if (-not $JsonOut) { Write-Host $Message }
}

function Write-BridgeLog([string]$Message) {
    try {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        $id = if ($script:CurrentJobId) { $script:CurrentJobId } else { '-' }
        $line = '[{0}] [{1}] {2}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $id, $Message
        [System.IO.File]::AppendAllText((Join-Path $LogDir 'bridge.log'), $line + "`r`n", $Utf8)
    } catch { }
}

function Add-JobEvent([hashtable]$Data) {
    $Data['ts'] = (Get-Date).ToString('o')
    try {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        [System.IO.File]::AppendAllText((Join-Path $LogDir 'jobs.jsonl'), ($Data | ConvertTo-Json -Compress -Depth 6) + "`n", $Utf8)
    } catch { Write-BridgeLog ("jobs.jsonl write failed: " + $_.Exception.Message) }
}

function Stop-Bridge([int]$Code, [string]$Message) {
    # Errors before a job is queued: nothing to record but the log line and the message.
    Write-BridgeLog ("exit {0}: {1}" -f $Code, $Message)
    if ($JsonOut) {
        Write-Output (@{ status = 'error'; exit_code = $Code; error = $Message } | ConvertTo-Json -Compress)
    } else {
        Write-Host $Message -ForegroundColor Yellow
    }
    exit $Code
}

function Format-NativeArg([string]$Value) {
    # Windows argv quoting (the rules CommandLineToArgvW and Rust's std parse by).
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $slashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append('\' * ($slashes * 2 + 1)); [void]$sb.Append('"'); $slashes = 0; continue
        }
        if ($slashes) { [void]$sb.Append('\' * $slashes); $slashes = 0 }
        [void]$sb.Append($ch)
    }
    if ($slashes) { [void]$sb.Append('\' * ($slashes * 2)) }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Get-Sha256Text([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Utf8.GetBytes($Text))) -replace '-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-Prop($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { return $Object[$Name] }
    $p = $Object.PSObject.Properties[$Name]
    if ($p) { return $p.Value }
    return $null
}

# --------------------------------------------------------------------------- settings
$DefaultSettings = [ordered]@{
    _help                   = 'Codex-Qwen bridge settings. write_mode: disabled (default: read-only jobs only) | windows-sandbox (Codex restricted-token sandbox; its first use changes folder ACLs) | full-access (commands run with your full user rights, no sandbox). Decide before enabling; see local-tools\codex\INSTRUCTIONS.md.'
    write_mode              = 'disabled'
    default_model           = 'qwen3-coder:30b'
    review_model            = 'qwen3-coder:30b'
    idle_wait_minutes       = 60
    recent_activity_minutes = 15
    max_minutes             = 120
    max_review_diff_lines   = 2000
    unload_after            = $true
    notify                  = $true
}

function Get-BridgeSettings {
    $settings = [ordered]@{}
    foreach ($k in $DefaultSettings.Keys) { $settings[$k] = $DefaultSettings[$k] }
    if (Test-Path -LiteralPath $SettingsPath) {
        try {
            $raw = [System.IO.File]::ReadAllText($SettingsPath, $Utf8) | ConvertFrom-Json
            foreach ($p in $raw.PSObject.Properties) { $settings[$p.Name] = $p.Value }
        } catch {
            Write-BridgeLog ("settings unreadable, using defaults: " + $_.Exception.Message)
        }
    } else {
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SettingsPath) | Out-Null
            [System.IO.File]::WriteAllText($SettingsPath, ($DefaultSettings | ConvertTo-Json -Depth 4), $Utf8)
        } catch { }
    }
    return $settings
}

# ------------------------------------------------------------------------- codex.exe
function Invoke-WithCodexEnv([scriptblock]$Body) {
    # Only the Codex process sees the dead proxy: the notifier (Telegram) and the python
    # helper run with the normal environment.
    $names = 'CODEX_HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY', 'QWEN_CODEX_RUN_DIR', 'QWEN_CODEX_JOB_ID'
    $saved = @{}
    foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
    try {
        [Environment]::SetEnvironmentVariable('CODEX_HOME', $CodexHome, 'Process')
        foreach ($n in 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') { [Environment]::SetEnvironmentVariable($n, 'http://127.0.0.1:9', 'Process') }
        [Environment]::SetEnvironmentVariable('NO_PROXY', '127.0.0.1,localhost', 'Process')
        foreach ($n in 'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY') { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
        # policy_hook.py (a child of codex.exe) writes the commands it blocks into the run folder.
        $hookDir = if ($script:HookRunDir) { $script:HookRunDir } else { $null }
        [Environment]::SetEnvironmentVariable('QWEN_CODEX_RUN_DIR', $hookDir, 'Process')
        [Environment]::SetEnvironmentVariable('QWEN_CODEX_JOB_ID', $(if ($script:CurrentJobId) { $script:CurrentJobId } else { $null }), 'Process')
        & $Body
    } finally {
        foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
    }
}

function Get-CodexVersion([string]$Exe) {
    # Always under the bridge's CODEX_HOME: even --version sets up helper aliases in the home.
    New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
    $text = Invoke-WithCodexEnv { & $Exe --version 2>$null | Out-String }
    if ($text -match 'codex-cli\s+(\d+)\.(\d+)\.(\d+)') { return [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3]) }
    return [version]'0.0.0'
}

function Resolve-CodexExe {
    if ($env:QWEN_CODEX_EXE) {
        if (Test-Path -LiteralPath $env:QWEN_CODEX_EXE -PathType Leaf) { return (Resolve-Path -LiteralPath $env:QWEN_CODEX_EXE).ProviderPath }
        Stop-Bridge 2 ("QWEN_CODEX_EXE ชี้ไปที่ไฟล์ที่ไม่มีอยู่: " + $env:QWEN_CODEX_EXE)
    }
    # The Codex app keeps each CLI build in its own hashed folder and adds a new one when it
    # updates (0.146 today, 0.158 in the next app); some folders hold only rg.exe.
    $bin = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $found = @(Get-ChildItem -LiteralPath $bin -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'codex.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    if ($found.Count -eq 0) { return $null }
    $ranked = @($found | ForEach-Object {
            [pscustomobject]@{ Path = $_; Version = (Get-CodexVersion $_); Time = (Get-Item -LiteralPath $_).LastWriteTime }
        } | Sort-Object -Property @{ Expression = 'Version'; Descending = $true }, @{ Expression = 'Time'; Descending = $true })
    return $ranked[0].Path
}

# ------------------------------------------------------------------------ CODEX_HOME
function ConvertTo-TomlKey([string]$Text) {
    # A literal '...' key when possible (what Codex itself writes), else an escaped "..." key.
    if (-not $Text.Contains("'") -and $Text -notmatch '[\r\n]') { return "'" + $Text + "'" }
    return '"' + ($Text.Replace('\', '\\').Replace('"', '\"')) + '"'
}

function Remove-ProjectEntries([string]$Text) {
    # [projects.'<dir>'] / [projects."<dir>"] tables with one trust_level line: the bridge
    # appends "untrusted" ones per job, Codex itself may append "trusted" ones.
    if ($null -eq $Text) { return $null }
    return [regex]::Replace($Text, '(?m)^\r?\n?\[projects\.(?:''[^'']*''|"(?:[^"\\\r\n]|\\.)*")\]\r?\ntrust_level = "(trusted|untrusted)"\r?\n?', '')
}

function Get-PolicyHookCommand {
    # Codex runs a hook command through cmd.exe, so the rendered command must not need quoting:
    # no spaces, quotes or cmd metacharacters in the python or script path. On this machine
    # both are plain (E:\localllm\webui-venv\Scripts\python.exe, ...\codex\policy_hook.py).
    $python = Resolve-Python
    if (-not $python -or -not (Test-Path -LiteralPath $PolicyHookScript -PathType Leaf)) { return '' }
    foreach ($p in @($python, $PolicyHookScript)) { if ($p -match '[\s"''&|<>^%!]') { return '' } }
    # -I -S: no script dir, user site, site-packages or .pth - nothing a job wrote can load before the policy
    return ('{0} -I -S -X utf8 {1}' -f $python, $PolicyHookScript)
}

function Get-PolicyTrust {
    try { return ([System.IO.File]::ReadAllText($PolicyTrustFile, $Utf8) | ConvertFrom-Json) } catch { return $null }
}

function Initialize-CodexHome([string[]]$UntrustedDirs = @()) {
    if ($Root.Contains("'")) { Stop-Bridge 2 "โฟลเดอร์ของชุดโปรแกรมมีเครื่องหมาย ' ในชื่อ ใช้กับ config ของ Codex ไม่ได้: $Root" }
    New-Item -ItemType Directory -Force -Path $CodexHome, (Join-Path $CodexHome 'rules') | Out-Null
    $stateFile = Join-Path $CodexHome '.bridge-generated.json'
    $state = @{}
    if (Test-Path -LiteralPath $stateFile) {
        try { ([System.IO.File]::ReadAllText($stateFile, $Utf8) | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $state[$_.Name] = [string]$_.Value } } catch { }
    }
    # Policy hook: its command, and the trust Codex needs before it runs it (hash from the last
    # hooks/list, kept only while the command is unchanged).
    $hookCommand = Get-PolicyHookCommand
    if (-not $hookCommand) { $hookCommand = 'policy-hook-unavailable' }  # never trusted: write jobs are refused
    $trustText = ''
    $trust = Get-PolicyTrust
    if ($trust -and [string]$trust.command -eq $hookCommand -and [string]$trust.key -and [string]$trust.hash -match '^sha256:[0-9a-f]{64}$') {
        $trustText = "`n[hooks.state.{0}]`ntrusted_hash = `"{1}`"`n" -f (ConvertTo-TomlKey ([string]$trust.key)), [string]$trust.hash
    }
    $projectsText = ''
    foreach ($d in @($UntrustedDirs | Where-Object { $_ } | Select-Object -Unique)) {
        $projectsText += "`n[projects.{0}]`ntrust_level = `"untrusted`"`n" -f (ConvertTo-TomlKey $d)
    }
    $pairs = @(
        @{ Name = 'config'; Template = 'config.template.toml'; Target = (Join-Path $CodexHome 'config.toml') },
        @{ Name = 'models'; Template = 'qwen-models.template.json'; Target = (Join-Path $CodexHome 'qwen-models.json') },
        @{ Name = 'rules'; Template = 'qwen-bridge.rules.template'; Target = (Join-Path $CodexHome 'rules\qwen-bridge.rules') }
    )
    foreach ($p in $pairs) {
        $template = Join-Path $PSScriptRoot $p.Template
        if (-not (Test-Path -LiteralPath $template)) { Stop-Bridge 2 "ไม่พบไฟล์แม่แบบ $template" }
        $text = [System.IO.File]::ReadAllText($template, $Utf8).Replace('{{ROOT}}', $Root)
        if ($p.Name -eq 'config') {
            if (-not $projectsText -and (Test-Path -LiteralPath $p.Target)) {
                # A launch or -Check that has no job folders must not drop the untrusted entry a
                # starting job just wrote (without it a repo's .codex\config.toml could load and
                # turn hooks off). Only "untrusted" entries are carried over, never trusted ones.
                $existing = [System.IO.File]::ReadAllText($p.Target, $Utf8)
                foreach ($m in [regex]::Matches($existing, '(?m)^\[projects\.(?<k>[^\r\n]+)\]\r?\ntrust_level = "untrusted"\r?$')) {
                    $projectsText += "`n[projects.{0}]`ntrust_level = `"untrusted`"`n" -f $m.Groups['k'].Value
                }
            }
            # The trust placeholder is a comment line, so the template itself stays valid TOML.
            $text = [regex]::Replace($text, '(?m)^# \{\{POLICY_HOOK_TRUST\}\}.*$', $trustText.Replace('$', '$$'))
            $text = $text.Replace('{{POLICY_HOOK_COMMAND}}', $hookCommand) + $projectsText
        }
        $current = $null
        if (Test-Path -LiteralPath $p.Target) { $current = [System.IO.File]::ReadAllText($p.Target, $Utf8) }
        # The recorded hash leaves out [projects] trust entries: the bridge writes one per job
        # folder, and Codex may add its own; neither is an outside change worth a backup.
        $expected = Get-Sha256Text (Remove-ProjectEntries $text)
        if ($current -cne $text) {
            if ($null -ne $current -and $state.ContainsKey($p.Name) -and (Get-Sha256Text $current) -ne $state[$p.Name] -and (Get-Sha256Text (Remove-ProjectEntries $current)) -ne $state[$p.Name]) {
                # Changed since the bridge wrote it (a TUI session saving a choice, or a hand
                # edit). The template wins; the changed copy is kept for whoever made it.
                $bak = $p.Target + '.' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.bak'
                Copy-Item -LiteralPath $p.Target -Destination $bak -Force
                Write-BridgeLog ("{0} was changed outside the bridge; kept as {1}, rewritten from the template" -f $p.Target, $bak)
            }
            [System.IO.File]::WriteAllText($p.Target, $text, $Utf8)
        }
        $state[$p.Name] = $expected
    }
    [System.IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json), $Utf8)
    # Only the bridge's own rules stay active. A TUI "always allow" writes an allow rule
    # (rules\default.rules) that would let later unattended jobs skip the read-only policy.
    $disabled = Join-Path $CodexHome 'rules-disabled'
    foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $CodexHome 'rules') -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'qwen-bridge.rules' })) {
        New-Item -ItemType Directory -Force -Path $disabled | Out-Null
        $dest = Join-Path $disabled ($f.Name + '.' + (Get-Date).ToString('yyyyMMdd-HHmmss'))
        Move-Item -LiteralPath $f.FullName -Destination $dest -Force
        Write-BridgeLog ("moved extra rules file {0} to {1}" -f $f.Name, $dest)
    }
}

# ---------------------------------------------------------------------- Ollama checks
function Get-OllamaPs {
    try { return Invoke-RestMethod -Uri "$OllamaUrl/api/ps" -TimeoutSec 5 } catch { return $null }
}

function Get-LoadedModels {
    $ps = Get-OllamaPs
    return @(@(Get-Prop $ps 'models') | Where-Object { $_ })
}

function Test-OllamaUp {
    try { Invoke-RestMethod -Uri "$OllamaUrl/api/version" -TimeoutSec 5 | Out-Null; return $true } catch { return $false }
}

function Get-RunnerSlotUrls {
    if ($env:QWEN_CODEX_SLOTS_URLS) { return @($env:QWEN_CODEX_SLOTS_URLS -split '[,;]' | Where-Object { $_ }) }
    if ($OllamaUrl -ne $DefaultOllama) { return @() }  # a test server has no runners
    $owner = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -First 1
    if (-not $owner) { return @() }
    return @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $owner } |
        ForEach-Object { if ([string]$_.CommandLine -match '--port\s+(\d+)') { "http://127.0.0.1:{0}/slots" -f $Matches[1] } })
}

function Test-RunnerProcessing {
    # The same signal Restart-QwenOllama.ps1 waits on: it also sees a long prompt being read.
    foreach ($url in (Get-RunnerSlotUrls)) {
        try {
            $slots = Invoke-RestMethod -Uri $url -TimeoutSec 3
            if (@($slots | Where-Object { (Get-Prop $_ 'is_processing') -eq $true }).Count -gt 0) { return $true }
        } catch { }
    }
    return $false
}

function Get-LastModelRequestTime {
    # Ollama writes one [GIN] line per finished request to the launcher's log. Only real
    # model work counts (200 on chat/generate/embed and the /v1 routes), not /api/ps or tags.
    $latest = $null
    foreach ($name in 'ollama-server.out', 'ollama.log') {
        $path = Join-Path (Join-Path $Root 'logs') $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($line in @(Get-Content -LiteralPath $path -Tail 300 -ErrorAction SilentlyContinue)) {
            if ($line -match '^\[GIN\] (\d{4}/\d{2}/\d{2} - \d{2}:\d{2}:\d{2}) \|\s*200\s*\|[^|]*\|[^|]*\|\s*POST\s+"/(api/(chat|generate|embed|embeddings)|v1/(chat/completions|completions|responses|messages|embeddings))"') {
                try {
                    $t = [datetime]::ParseExact($Matches[1], 'yyyy/MM/dd - HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
                    if (-not $latest -or $t -gt $latest) { $latest = $t }
                } catch { }
            }
        }
    }
    return $latest
}

function Get-KeepAlive {
    $raw = [Environment]::GetEnvironmentVariable('OLLAMA_KEEP_ALIVE', 'User')
    if (-not $raw) { $raw = '5m' }
    if ($raw -match '^(\d+)\s*h$') { return [timespan]::FromHours([int]$Matches[1]) }
    if ($raw -match '^(\d+)\s*m$') { return [timespan]::FromMinutes([int]$Matches[1]) }
    if ($raw -match '^(\d+)\s*s?$') { return [timespan]::FromSeconds([int]$Matches[1]) }
    return [timespan]::FromHours(2)
}

function Get-OllamaBusyReason([string]$OurModel, [int]$RecentMinutes) {
    if (Test-RunnerProcessing) { return 'โมเดลกำลังประมวลผลคำขออื่นอยู่ (llama-server ยังไม่ว่าง)' }
    $others = @(Get-LoadedModels | Where-Object { ([string](Get-Prop $_ 'name') -ne $OurModel) -and ([string](Get-Prop $_ 'model') -ne $OurModel) })
    if ($others.Count -eq 0 -or $RecentMinutes -le 0) { return $null }
    $last = Get-LastModelRequestTime
    if (-not $last) {
        # No request log on this machine: estimate the last use from the keep-alive clock.
        foreach ($m in $others) {
            try {
                $t = ([datetime](Get-Prop $m 'expires_at')) - (Get-KeepAlive)
                if (-not $last -or $t -gt $last) { $last = $t }
            } catch { }
        }
    }
    if ($last -and $last -gt (Get-Date).AddMinutes(-$RecentMinutes)) {
        return ("มีการใช้โมเดล {0} ล่าสุดเวลา {1} (ภายใน {2} นาที) งานนี้จะดันโมเดลแชตออกจากหน่วยความจำ จึงรอให้ว่างก่อน" -f (Get-Prop $others[0] 'name'), $last.ToString('HH:mm'), $RecentMinutes)
    }
    return $null
}

function Invoke-ModelUnload([string]$Name) {
    # Frees the RAM for the chat model - only our model, and only when it is loaded.
    if (@(Get-LoadedModels | Where-Object { [string](Get-Prop $_ 'name') -eq $Name }).Count -eq 0) { return }
    try {
        Invoke-RestMethod -Uri "$OllamaUrl/api/generate" -Method Post -ContentType 'application/json' -Body (@{ model = $Name; keep_alive = 0 } | ConvertTo-Json -Compress) -TimeoutSec 30 | Out-Null
        Write-BridgeLog ("unloaded " + $Name)
    } catch { Write-BridgeLog ("unload failed: " + $_.Exception.Message) }
}

# ------------------------------------------------------------------------------ lock
function Get-LockHolder {
    try {
        $info = [System.IO.File]::ReadAllText((Join-Path $CodexHome 'run.lock.json'), $Utf8) | ConvertFrom-Json
        return ("งาน {0}, pid {1}, เริ่ม {2}" -f $info.id, $info.pid, $info.since)
    } catch { return 'ไม่ทราบรายละเอียด' }
}

function Enter-BridgeLock([double]$WaitMinutes, [hashtable]$Info) {
    $path = Join-Path $CodexHome 'run.lock'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $announced = $false
    while ($true) {
        try {
            # FileShare None: a second bridge process cannot open it, and Windows frees it
            # when this process dies, so a killed job never leaves a stale lock behind.
            $fs = [System.IO.File]::Open($path, 'OpenOrCreate', 'ReadWrite', 'None')
            $Info['pid'] = $PID
            $Info['since'] = (Get-Date).ToString('o')
            [System.IO.File]::WriteAllText((Join-Path $CodexHome 'run.lock.json'), ($Info | ConvertTo-Json -Compress), $Utf8)
            return $fs
        } catch [System.IO.IOException] {
            if ($sw.Elapsed.TotalMinutes -ge $WaitMinutes) { return $null }
            if (-not $announced) {
                Say ("มีงาน Codex อื่นกำลังทำอยู่ ({0}) - รอคิว" -f (Get-LockHolder))
                Write-BridgeLog ("waiting for the bridge lock held by " + (Get-LockHolder))
                $announced = $true
            }
            Start-Sleep -Seconds ([Math]::Min($PollSeconds, 60))
        }
    }
}

# ---------------------------------------------------------------------------- helpers
function Resolve-Python {
    $candidates = @($env:QWEN_CODEX_PYTHON, (Join-Path $Root 'webui-venv\Scripts\python.exe'), (Join-Path (Split-Path -Parent $ToolsDir) 'webui-venv\Scripts\python.exe'))
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return $c } }
    $py = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }
    return $null
}

function Invoke-Helper([string[]]$HelperArgs) {
    # The answer comes back through a UTF-8 file: console code pages mangle Thai on stdout.
    $python = Resolve-Python
    if (-not $python) { Write-BridgeLog 'no python for codex_jobs.py'; return $null }
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        $log = & $python -X utf8 $Helper @HelperArgs --out $tmp 2>&1 | Out-String
        $text = [System.IO.File]::ReadAllText($tmp, $Utf8).Trim()
        if (-not $text) { Write-BridgeLog ("helper {0} returned nothing: {1}" -f $HelperArgs[0], $log.Trim()); return $null }
        return ($text | ConvertFrom-Json)
    } catch {
        Write-BridgeLog ("helper {0} failed: {1}" -f $HelperArgs[0], $_.Exception.Message)
        return $null
    } finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
}

function Test-PolicyHookSelf {
    # policy_hook.py --selftest: python starts, command_policy imports and known commands get
    # the right answer. Codex runs a hook that cannot start as if it had said "allow".
    $python = Resolve-Python
    if (-not $python) { return 'ไม่พบ python สำหรับ policy_hook.py' }
    $out = (& $python -X utf8 $PolicyHookScript --selftest 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        $short = ($out -replace '\s+', ' ')
        if ($short.Length -gt 300) { $short = $short.Substring(0, 300) }
        return ('policy_hook.py --selftest ไม่ผ่าน (exit {0}): {1}' -f $LASTEXITCODE, $short)
    }
    return $null
}

function Confirm-PolicyHook([string]$Cwd, [string[]]$UntrustedDirs = @()) {
    # $null when Codex will run our policy hook before every tool call in $Cwd, else the reason.
    # Codex computes the hook's hash itself; hooks/list returns it, and the bridge trusts
    # exactly the command its own template rendered (first run, or after a Codex update that
    # changed the hash), then asks again to confirm.
    $cmd = Get-PolicyHookCommand
    if (-not $cmd) { return 'ไม่พบ python หรือ policy_hook.py (หรือ path มีช่องว่าง/อักขระพิเศษ) จึงเปิด hook นโยบายคำสั่งไม่ได้' }
    $self = Test-PolicyHookSelf
    if ($self) { return $self }
    $h = $null
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $listing = Invoke-Helper @('hooks', $CodexExe, $CodexHome, $Cwd)
        if (-not $listing -or (Get-Prop $listing 'ok') -ne $true) { return ('ถาม Codex เรื่อง hooks ไม่ได้: ' + [string](Get-Prop $listing 'error')) }
        $all = @(@(Get-Prop $listing 'hooks') | Where-Object { $_ })
        $ours = @($all | Where-Object { [string](Get-Prop $_ 'source') -eq 'user' -and [string](Get-Prop $_ 'command') -eq $cmd })
        if ($ours.Count -ne 1) { return ('Codex ไม่เห็น hook นโยบายคำสั่งใน config.toml (พบ {0} รายการ)' -f $ours.Count) }
        $h = $ours[0]
        $foreign = @($all | Where-Object { [string](Get-Prop $_ 'command') -ne $cmd })
        if ($foreign.Count) {
            Write-BridgeLog ('other hooks Codex sees (not run unless trusted): ' + (($foreign | ForEach-Object { '{0} [{1}]' -f (Get-Prop $_ 'key'), (Get-Prop $_ 'trustStatus') }) -join '; '))
        }
        $ready = ([string](Get-Prop $h 'trustStatus') -eq 'trusted') -and ((Get-Prop $h 'enabled') -ne $false) -and
            ([string](Get-Prop $h 'eventName') -eq 'preToolUse') -and ([string](Get-Prop $h 'matcher') -eq '*')
        if ($ready) { return $null }
        if ($attempt -eq 2) { break }
        $hash = [string](Get-Prop $h 'currentHash')
        $key = [string](Get-Prop $h 'key')
        if ($hash -notmatch '^sha256:[0-9a-f]{64}$' -or -not $key) { return ('Codex ไม่ส่ง hash ของ hook มา (trustStatus {0})' -f (Get-Prop $h 'trustStatus')) }
        $trust = @{ key = $key; command = $cmd; hash = $hash; codex_version = [string]$CodexVersion; trusted_at = (Get-Date).ToString('o') }
        [System.IO.File]::WriteAllText($PolicyTrustFile, ($trust | ConvertTo-Json -Compress), $Utf8)
        Write-BridgeLog ("policy hook trusted for Codex {0}: {1} = {2}" -f $CodexVersion, $key, $hash)
        Initialize-CodexHome $UntrustedDirs
    }
    return ('Codex ยังไม่เชื่อถือ hook นโยบายคำสั่ง (trustStatus {0}, enabled {1})' -f (Get-Prop $h 'trustStatus'), (Get-Prop $h 'enabled'))
}

function Get-UntrustedDirs([string[]]$Dirs) {
    # Job folders to mark trust_level = "untrusted" (case-insensitive de-dup: TOML rejects a
    # repeated table).
    $seen = @{}
    $out = @()
    foreach ($d in @($Dirs | Where-Object { $_ })) {
        $v = ([string]$d).TrimEnd('\')
        if ($v -match '^[A-Za-z]:$') { $v += '\' }
        $k = $v.ToLowerInvariant()
        if (-not $seen.ContainsKey($k)) { $seen[$k] = $true; $out += $v }
    }
    return $out
}

$BlockedParts = @('.aws', '.azure', '.gnupg', '.kube', '.ssh', 'credentials', 'login data', 'web data', '.claude', '.codex', '.openclaw')

function Resolve-WorkFolder([string]$Path, [string]$What) {
    if (-not $Path) { Stop-Bridge 2 "ต้องระบุ $What" }
    $clean = $Path.Trim().Trim('"')
    if (-not (Test-Path -LiteralPath $clean -PathType Container)) { Stop-Bridge 2 "ไม่พบโฟลเดอร์ ${What}: $clean" }
    $full = (Resolve-Path -LiteralPath $clean).ProviderPath.TrimEnd('\')
    if ($full -match '^[A-Za-z]:$') { Stop-Bridge 2 "ใช้ทั้งไดรฟ์ ($full\) เป็น $What ไม่ได้ ให้เลือกโฟลเดอร์งาน" }
    foreach ($part in ($full -split '\\')) {
        if ($BlockedParts -contains $part.ToLowerInvariant()) { Stop-Bridge 2 "โฟลเดอร์นี้เก็บข้อมูลลับหรือประวัติของเอเจนต์ ห้ามใช้เป็น ${What}: $full" }
    }
    $homeFull = [System.IO.Path]::GetFullPath($CodexHome).TrimEnd('\')
    if ($full.Equals($homeFull, [StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($homeFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Bridge 2 "ห้ามใช้ CODEX_HOME ของ bridge เป็น ${What}: $full"
    }
    return $full
}

function Get-ProjectInfo([string]$Folder) {
    $prefix = $ProjectsRoot.TrimEnd('\') + '\'
    if (-not $Folder.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    $slug = ($Folder.Substring($prefix.Length) -split '\\')[0]
    if (-not $slug) { return $null }
    $dir = Join-Path $ProjectsRoot $slug
    if (-not (Test-Path -LiteralPath (Join-Path $dir 'project.json'))) { return $null }
    return @{ slug = $slug; dir = $dir; outputs = (Join-Path $dir 'outputs') }
}

function Get-UniquePath([string]$Folder, [string]$Stem, [string]$Ext) {
    $candidate = Join-Path $Folder ($Stem + $Ext)
    $n = 2
    while (Test-Path -LiteralPath $candidate) { $candidate = Join-Path $Folder ("{0}-{1}{2}" -f $Stem, $n, $Ext); $n++ }
    return $candidate
}

function Get-GitTop([string]$Folder) {
    $top = & git -C $Folder rev-parse --show-toplevel 2>$null | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or -not $top) { return $null }
    return ([string]$top).Replace('/', '\').TrimEnd('\')
}

function Test-Under([string]$Path, [string]$Folder) {
    if (-not $Path -or -not $Folder) { return $false }
    return ($Path.Equals($Folder, [StringComparison]::OrdinalIgnoreCase) -or $Path.StartsWith($Folder.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase))
}

function Test-GitRef([string]$Repo, [string]$Ref) {
    & git -C $Repo --no-optional-locks rev-parse --verify --quiet ($Ref + '^{commit}') 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Get-ReviewDiffLines([string]$Repo) {
    # Read-only git only: --no-optional-locks keeps even `git diff` from refreshing the index.
    $numstat = @()
    if ($Commit) {
        $numstat = @(& git -C $Repo --no-optional-locks show --numstat --format= $Commit 2>$null)
    } elseif ($Base) {
        $numstat = @(& git -C $Repo --no-optional-locks diff --numstat ($Base + '...HEAD') 2>$null)
    } elseif (Test-GitRef $Repo 'HEAD') {
        $numstat = @(& git -C $Repo --no-optional-locks diff --numstat HEAD 2>$null)
    } else {
        $numstat = @(& git -C $Repo --no-optional-locks diff --numstat --cached 2>$null) + @(& git -C $Repo --no-optional-locks diff --numstat 2>$null)
    }
    $total = 0
    foreach ($line in $numstat) {
        if ($line -match '^(\d+)\s+(\d+)\s') { $total += [int]$Matches[1] + [int]$Matches[2] }
    }
    if (-not $Commit -and -not $Base) {
        # Untracked files are part of an --uncommitted review.
        foreach ($rel in @(& git -C $Repo --no-optional-locks ls-files --others --exclude-standard 2>$null)) {
            if (-not $rel) { continue }
            $f = Join-Path $Repo $rel
            try {
                if ((Get-Item -LiteralPath $f -ErrorAction Stop).Length -le 2MB) { $total += @([System.IO.File]::ReadAllLines($f)).Count }
            } catch { }
        }
    }
    return $total
}

function Stop-ProcessTree([int]$ProcessId) {
    & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
}

function New-JobId {
    return (Get-Date).ToString('yyyyMMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0, 4))
}

# ------------------------------------------------------------------------- mode pick
$modeCount = @($Exec, $Review, $Tui, $Check, $List | Where-Object { $_ }).Count
if ($modeCount -gt 1) { Stop-Bridge 2 'เลือกได้ทีละโหมด: -Exec, -Review, -Tui, -Check หรือ -List' }
$Kind = 'exec'
if ($Review) { $Kind = 'review' } elseif ($Tui) { $Kind = 'tui' } elseif ($Check) { $Kind = 'check' } elseif ($List) { $Kind = 'list' }
if ($ReadOnly -and $Write) { Stop-Bridge 2 'ใช้ -ReadOnly กับ -Write พร้อมกันไม่ได้' }
foreach ($ref in @($Base, $Commit)) { if ($ref -and $ref.StartsWith('-')) { Stop-Bridge 2 "ชื่อ branch/commit ต้องไม่ขึ้นต้นด้วย '-': $ref" } }
# Arrays arrive as one string through powershell -File (the launcher, the .cmd shim).
$AddDir = @($AddDir | ForEach-Object { $_ -split ';' } | Where-Object { $_ -and $_.Trim() })
$Config = @($Config | ForEach-Object { $_ -split "`n" } | Where-Object { $_ -and $_.Trim() })
$Settings = Get-BridgeSettings

# ------------------------------------------------------------------------------ list
if ($Kind -eq 'list') {
    # Windows PowerShell drops empty-string arguments to native programs, so the optional
    # job id travels as a flag.
    $listArgs = @('list', $Root, '--limit', '15')
    if ($JobId) { $listArgs += @('--job', $JobId) }
    $listing = Invoke-Helper $listArgs
    if ($JsonOut) { Write-Output ($listing | ConvertTo-Json -Depth 8 -Compress); exit 0 }
    if (-not $listing -or (Get-Prop $listing 'ok') -eq $false) { Say ('อ่านรายการงานไม่ได้: ' + (Get-Prop $listing 'error')); exit 1 }
    if ($JobId) { Say ((Get-Prop $listing 'job') | ConvertTo-Json -Depth 6); exit 0 }
    Say ("งาน Codex ที่กำลังทำ/รอคิว: {0}" -f (Get-Prop $listing 'running_count'))
    foreach ($j in @(Get-Prop $listing 'jobs')) {
        if (-not $j) { continue }
        $mins = Get-Prop $j 'minutes'
        Say ("- {0}  {1,-11} {2,-6} {3}  {4}" -f (Get-Prop $j 'id'), (Get-Prop $j 'status'), (Get-Prop $j 'kind'), $(if ($null -ne $mins) { "$mins นาที" } else { '' }), (Get-Prop $j 'title'))
    }
    exit 0
}

# ----------------------------------------------------------------------------- setup
$CodexExe = Resolve-CodexExe
if (-not $CodexExe) {
    Stop-Bridge 2 'ไม่พบ Codex CLI (codex.exe) ใน %LOCALAPPDATA%\OpenAI\Codex\bin - เปิดแอป Codex หนึ่งครั้งให้ติดตั้ง CLI หรือกำหนด QWEN_CODEX_EXE (bridge ไม่ดาวน์โหลดเอง)'
}
$CodexVersion = Get-CodexVersion $CodexExe
Initialize-CodexHome

$providerOverride = @()
if ($OllamaUrl -ne $DefaultOllama) { $providerOverride = @('-c', ('model_providers.ollama_local.base_url="{0}/v1"' -f $OllamaUrl)) }
$extraConfig = @()
foreach ($c in $Config) { $extraConfig += @('-c', $c) }

# ----------------------------------------------------------------------------- check
if ($Kind -eq 'check') {
    $problems = 0
    Say 'Codex-Qwen bridge - ตรวจสอบ (ไม่เรียกโมเดล)'
    Say ("codex.exe : {0} (v{1})" -f $CodexExe, $CodexVersion)
    Say ("CODEX_HOME: {0}" -f $CodexHome)
    $features = @(Invoke-WithCodexEnv { & $CodexExe features list 2>$null })
    $mustBeOff = 'apps', 'plugins', 'browser_use', 'in_app_browser', 'computer_use', 'image_generation', 'multi_agent', 'goals', 'guardian_approval', 'code_mode_host', 'skill_mcp_dependency_install', 'tool_suggest', 'fast_mode', 'remote_compaction_v2', 'secret_auth_storage', 'in_app_updates', 'memories', 'workspace_dependencies'
    $on = @($features | Where-Object { $_ -match '^(\S+)\s.*\strue\s*$' -and $mustBeOff -contains $Matches[1] } | ForEach-Object { ($_ -split '\s+')[0] })
    $hooksOn = @($features | Where-Object { $_ -match '^hooks\s.*\strue\s*$' }).Count -gt 0
    if ($features.Count -eq 0) { $problems++; Say '[!] อ่านรายการฟีเจอร์ของ Codex ไม่ได้' }
    elseif ($on.Count) { $problems++; Say ('[!] ฟีเจอร์ที่ควรปิดแต่ยังเปิด: ' + ($on -join ', ')) }
    elseif (-not $hooksOn) { $problems++; Say '[!] ฟีเจอร์ hooks ปิดอยู่ - hook นโยบายคำสั่งจะไม่ทำงาน' }
    else { Say '[ok] ฟีเจอร์คลาวด์/เบราว์เซอร์/ปลั๊กอิน ปิดครบ, hooks เปิด (เฉพาะ hook นโยบายคำสั่งของ bridge)' }
    $modelsJson = Invoke-WithCodexEnv { & $CodexExe debug models 2>$null | Out-String }
    $slugs = @()
    try { $slugs = @(($modelsJson | ConvertFrom-Json).models | ForEach-Object { $_.slug }) } catch { }
    $foreign = @($slugs | Where-Object { $KnownModels -notcontains $_ })
    if ($slugs.Count -eq 0 -or $foreign.Count) { $problems++; Say ('[!] รายชื่อโมเดลของ Codex ไม่ตรง: ' + ($slugs -join ', ')) } else { Say ('[ok] โมเดลที่ Codex เห็น: ' + ($slugs -join ', ')) }
    $probeDir = Join-Path $CodexHome 'check-workspace'
    New-Item -ItemType Directory -Force -Path $probeDir | Out-Null
    Push-Location -LiteralPath $probeDir
    try { $promptInput = Invoke-WithCodexEnv { & $CodexExe debug prompt-input hi 2>$null | Out-String } } finally { Pop-Location }
    $bytes = $Utf8.GetByteCount([string]$promptInput)
    if ($bytes -lt 200 -or $bytes -gt 2500 -or $promptInput -match 'skills_instructions|## Skills') { $problems++; Say ("[!] prompt เริ่มต้นผิดปกติ: {0} ไบต์" -f $bytes) } else { Say ("[ok] prompt เริ่มต้น {0} ไบต์ (ไม่มีบล็อก skills)" -f $bytes) }
    # --strict-config against a dead port: a config problem shows up as a config error,
    # anything else ends in a connection error - and no model is ever asked.
    $stdin = Join-Path $probeDir 'prompt.txt'; [System.IO.File]::WriteAllText($stdin, 'check', $Utf8)
    $errFile = Join-Path $probeDir 'stderr.txt'; $outFile = Join-Path $probeDir 'events.jsonl'
    $argsCheck = @('exec', '--strict-config', '--enable', 'hooks', '--json', '--ephemeral', '--skip-git-repo-check', '-s', 'read-only', '-C', $probeDir, '-c', 'model_providers.ollama_local.base_url="http://127.0.0.1:9/v1"', '-')
    $p = Invoke-WithCodexEnv { Start-Process -FilePath $CodexExe -ArgumentList (($argsCheck | ForEach-Object { Format-NativeArg $_ }) -join ' ') -RedirectStandardInput $stdin -RedirectStandardOutput $outFile -RedirectStandardError $errFile -WorkingDirectory $probeDir -NoNewWindow -PassThru }
    $null = $p.Handle
    if (-not $p.WaitForExit(120000)) { Stop-ProcessTree $p.Id }
    $errText = [string](Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue) + [string](Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue)
    # 0.146 says "Error loading config.toml: ... unknown configuration field `x`" (verified).
    if ($errText -match '(?i)unknown configuration field|unknown (field|variant|key)|invalid type|error loading config|unrecognized') {
        $problems++; Say '[!] config.toml มีคีย์ที่ Codex รุ่นนี้ไม่รู้จัก (--strict-config):'; Say (($errText.Trim() -split "`n" | Select-Object -Last 3) -join "`n")
    } else { Say '[ok] config ผ่าน --strict-config (ทดสอบกับพอร์ตปิด 127.0.0.1:9 ไม่มีการเรียกโมเดล)' }
    # Policy hook: self-test + Codex sees it as trusted (trusts it on the first check).
    $hookProblem = Confirm-PolicyHook $probeDir (Get-UntrustedDirs @($probeDir))
    if ($hookProblem) {
        $problems++; Say ('[!] hook นโยบายคำสั่ง: ' + $hookProblem)
        if ([string]$Settings.write_mode -ne 'disabled') { Say '    งานแก้ไขไฟล์ (-Write) จะถูกปฏิเสธจนกว่าจะแก้' }
    } else { Say '[ok] hook นโยบายคำสั่ง (policy_hook.py) ผ่าน self-test และ Codex เชื่อถือแล้ว - ตรวจทุกคำสั่งก่อนรัน' }
    Say ("โหมดแก้ไขไฟล์ (write_mode): {0}" -f $Settings.write_mode)
    if (Test-OllamaUp) {
        $loaded = @(Get-LoadedModels | ForEach-Object { Get-Prop $_ 'name' })
        Say ("[ok] Ollama ตอบที่ {0}; โมเดลในหน่วยความจำ: {1}" -f $OllamaUrl, $(if ($loaded.Count) { $loaded -join ', ' } else { '(ไม่มี)' }))
        $busy = Get-OllamaBusyReason ([string]$Settings.default_model) ([int]$Settings.recent_activity_minutes)
        if ($busy) { Say ('[i] ตอนนี้ยังไม่ว่าง: ' + $busy) } else { Say '[ok] ตอนนี้ Ollama ว่างพอเริ่มงานได้' }
    } else { $problems++; Say ('[!] Ollama ไม่ตอบที่ ' + $OllamaUrl) }
    try {
        $fs = [System.IO.File]::Open((Join-Path $CodexHome 'run.lock'), 'OpenOrCreate', 'ReadWrite', 'None'); $fs.Dispose(); Say '[ok] ไม่มีงาน Codex อื่นค้างอยู่'
    } catch { Say ('[i] มีงาน Codex กำลังทำอยู่: ' + (Get-LockHolder)) }
    if (Test-Path -LiteralPath (Join-Path $HelperToolsDir 'notifier.py')) { Say '[ok] พบ notifier.py (แจ้งเตือนเมื่องานเสร็จ)' } else { Say '[i] ยังไม่มี notifier.py - งานจะไม่แจ้งเตือน' }
    if (-not (Resolve-Python)) { $problems++; Say '[!] ไม่พบ python สำหรับ codex_jobs.py' }
    Write-BridgeLog ("check: {0} problem(s)" -f $problems)
    exit ([int]($problems -gt 0))
}

# ---------------------------------------------------------------------- job settings
if ($IdleWaitMinutes -lt 0) { $IdleWaitMinutes = if ($Kind -eq 'tui') { 0 } else { [int]$Settings.idle_wait_minutes } }
if ($MaxMinutes -le 0) { $MaxMinutes = [int]$Settings.max_minutes }
if (-not $Model) { $Model = if ($Kind -eq 'review') { [string]$Settings.review_model } else { [string]$Settings.default_model } }
if ($KnownModels -notcontains $Model) { Stop-Bridge 2 ("โมเดลที่ใช้ได้กับ bridge: {0} (ได้รับ: {1})" -f ($KnownModels -join ' หรือ '), $Model) }

$JobMode = 'read-only'
if ($Write -and $Kind -ne 'review') {
    switch ([string]$Settings.write_mode) {
        'windows-sandbox' { $JobMode = 'write-sandbox' }
        'full-access' { $JobMode = 'write-full' }
        default {
            Stop-Bridge 5 ("โหมดแก้ไขไฟล์ยังไม่เปิด: ต้องเลือกก่อนว่าจะใช้ Windows sandbox ของ Codex (การใช้ครั้งแรกจะเปลี่ยนสิทธิ์ ACL ของโฟลเดอร์) หรือให้คำสั่งรันด้วยสิทธิ์ผู้ใช้เต็ม - ตั้ง write_mode ใน {0} (ดู INSTRUCTIONS.md) หรือสั่งงานแบบอ่านอย่างเดียว" -f $SettingsPath)
        }
    }
}
$sandboxArgs = switch ($JobMode) {
    'write-sandbox' { @('-s', 'workspace-write', '-c', 'windows.sandbox="unelevated"') }
    'write-full' { @('-s', 'danger-full-access') }
    default { @('-s', 'read-only') }
}

$WorkDir = Resolve-WorkFolder $Cwd 'โฟลเดอร์งาน (-Cwd)'
$AddDirs = @($AddDir | ForEach-Object { Resolve-WorkFolder $_ 'โฟลเดอร์เพิ่ม (-AddDir)' })
$Project = Get-ProjectInfo $WorkDir

# ------------------------------------------------------------------------------- TUI
if ($Kind -eq 'tui') {
    if (-not (Test-OllamaUp)) { Stop-Bridge 2 "Ollama ไม่ตอบที่ $OllamaUrl - เปิด Qwen Suite ก่อน" }
    $lock = Enter-BridgeLock 0 @{ id = 'tui'; kind = 'tui'; cwd = $WorkDir }
    if (-not $lock) { Stop-Bridge 4 ("มีงาน Codex อื่นกำลังทำอยู่ ({0}) ลองใหม่เมื่อเสร็จ" -f (Get-LockHolder)) }
    $tuiStarted = $false
    try {
        $busy = Get-OllamaBusyReason $Model ([int]$Settings.recent_activity_minutes)
        if ($busy) { Stop-Bridge 3 ('ยังไม่เปิดโหมดโต้ตอบ: ' + $busy) }
        $untrusted = Get-UntrustedDirs (@($WorkDir, (Get-GitTop $WorkDir)) + $AddDirs)
        Initialize-CodexHome $untrusted
        $hookProblem = Confirm-PolicyHook $WorkDir $untrusted
        if ($hookProblem) {
            Write-BridgeLog ('tui policy hook: ' + $hookProblem)
            if ($JobMode -ne 'read-only') { Stop-Bridge 2 ('ไม่เปิดโหมดโต้ตอบแบบแก้ไขไฟล์: ' + $hookProblem) }
            Say ('คำเตือน: hook นโยบายคำสั่งไม่พร้อม (' + $hookProblem + ')')
        }
        try { $Host.UI.RawUI.WindowTitle = 'Qwen Codex - โหมดโต้ตอบ' } catch { }
        Say ("Qwen Codex (โหมดโต้ตอบ) - โฟลเดอร์ {0}, โมเดล {1}, โหมด {2}" -f $WorkDir, $Model, $JobMode)
        Say 'ทุกขั้นช้า (โมเดลรันบน CPU) ถ้า Codex เสนอให้ติดตั้ง sandbox ของ Windows ให้เลือกไม่ติดตั้ง/ออก จนกว่าจะตัดสินใจเรื่องนี้'
        Write-BridgeLog ("tui start cwd={0} model={1} mode={2}" -f $WorkDir, $Model, $JobMode)
        # Interactive: the person at the console answers Codex's own approval questions.
        $tuiArgs = @('--strict-config', '--enable', 'hooks') + $sandboxArgs + @('-a', 'on-request', '-C', $WorkDir, '-m', $Model) + $providerOverride + $extraConfig
        foreach ($d in $AddDirs) { $tuiArgs += @('--add-dir', $d) }
        Push-Location -LiteralPath $WorkDir
        $tuiStarted = $true
        try { Invoke-WithCodexEnv { & $CodexExe @tuiArgs } } finally { Pop-Location }
        Write-BridgeLog ("tui exit {0}" -f $LASTEXITCODE)
    } finally {
        # A refused TUI (Ollama busy) never loaded the model: it may be a coder chat's.
        if ($tuiStarted -and -not $KeepModel -and $Settings.unload_after) { Invoke-ModelUnload $Model }
        $lock.Dispose()
    }
    exit 0
}

# --------------------------------------------------------------------- exec / review
$promptText = ''
if ($PromptFile) {
    if (-not (Test-Path -LiteralPath $PromptFile -PathType Leaf)) { Stop-Bridge 2 "ไม่พบไฟล์คำสั่ง: $PromptFile" }
    $PromptFile = (Resolve-Path -LiteralPath $PromptFile).ProviderPath
    $promptText = [System.IO.File]::ReadAllText($PromptFile, $Utf8)
} else { $promptText = $Prompt }
if ($Kind -eq 'exec') {
    if (-not $promptText.Trim()) { Stop-Bridge 2 'ต้องมีคำสั่งงาน (-Prompt หรือ -PromptFile)' }
    if ($ResumeLast -and $ResumeId) { Stop-Bridge 2 'เลือก -ResumeLast หรือ -ResumeId อย่างใดอย่างหนึ่ง' }
} else {
    if (@(@($Uncommitted.IsPresent, [bool]$Base, [bool]$Commit) | Where-Object { $_ }).Count -gt 1) { Stop-Bridge 2 'เลือกขอบเขตรีวิวได้อย่างเดียว: -Uncommitted, -Base หรือ -Commit' }
    & git -C $WorkDir rev-parse --is-inside-work-tree 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Stop-Bridge 2 "โฟลเดอร์นี้ไม่ใช่ git repo รีวิวได้เฉพาะ repo ที่มีอยู่แล้ว: $WorkDir" }
    foreach ($ref in @($Base, $Commit)) { if ($ref -and -not (Test-GitRef $WorkDir $ref)) { Stop-Bridge 2 "ไม่พบ branch/commit '$ref' ใน $WorkDir" } }
}
if ($OutputSchema) {
    if (-not (Test-Path -LiteralPath $OutputSchema -PathType Leaf)) { Stop-Bridge 2 "ไม่พบไฟล์ schema: $OutputSchema" }
    $OutputSchema = (Resolve-Path -LiteralPath $OutputSchema).ProviderPath
    try { [System.IO.File]::ReadAllText($OutputSchema, $Utf8) | ConvertFrom-Json | Out-Null } catch { Stop-Bridge 2 "ไฟล์ schema ไม่ใช่ JSON: $OutputSchema" }
}

# ------------------------------------------------------------------ detach (launcher)
if ($Detach) {
    # Everything above was checked here, so the desktop launcher shows argument errors at
    # once; the hidden child checks again and does the waiting and the work.
    if (-not $JobId) { $JobId = New-JobId }
    $script:CurrentJobId = $JobId
    if (-not $PromptFile -and $Prompt) {
        $pending = Join-Path $LogDir 'pending'
        New-Item -ItemType Directory -Force -Path $pending | Out-Null
        $PromptFile = Join-Path $pending ($JobId + '.prompt.md')
        [System.IO.File]::WriteAllText($PromptFile, $Prompt, $Utf8)
    }
    $argList = New-Object System.Collections.Generic.List[string]
    foreach ($a in '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $PSCommandPath) { $argList.Add($a) }
    foreach ($key in @($PSBoundParameters.Keys)) {
        if ($key -in @('Detach', 'Prompt', 'JobId', 'PromptFile', 'JsonOut', 'AddDir', 'Config', 'Cwd', 'OutputSchema')) { continue }
        $value = $PSBoundParameters[$key]
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) { $argList.Add('-' + $key) }
        } else {
            $argList.Add('-' + $key); $argList.Add([string]$value)
        }
    }
    # Resolved paths, without a trailing backslash (it would escape the closing quote).
    $argList.Add('-Cwd'); $argList.Add($WorkDir)
    if ($PromptFile) { $argList.Add('-PromptFile'); $argList.Add($PromptFile) }
    if ($OutputSchema) { $argList.Add('-OutputSchema'); $argList.Add($OutputSchema) }
    if ($AddDirs.Count) { $argList.Add('-AddDir'); $argList.Add($AddDirs -join ';') }
    if ($Config.Count) { $argList.Add('-Config'); $argList.Add($Config -join "`n") }
    $argList.Add('-JobId'); $argList.Add($JobId)
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $child = Start-Process -FilePath $shell -ArgumentList (($argList | ForEach-Object { Format-NativeArg $_ }) -join ' ') -WindowStyle Hidden -PassThru
    Write-BridgeLog ("detached {0} job started in pid {1}" -f $Kind, $child.Id)
    if ($JsonOut) {
        Write-Output (@{ status = 'started'; id = $JobId; pid = $child.Id; log = (Join-Path $LogDir 'bridge.log') } | ConvertTo-Json -Compress)
    } else {
        Say ("เริ่มงาน Codex แล้ว รหัส {0} (ทำงานเบื้องหลัง เสร็จแล้วจะแจ้งเตือน)" -f $JobId)
    }
    exit 0
}

if (-not $JobId) { $JobId = New-JobId }
$script:CurrentJobId = $JobId
if (-not $Title) {
    if ($Kind -eq 'review') { $Title = 'รีวิวโค้ด ' + (Split-Path -Leaf $WorkDir) }
    else { $Title = (($promptText.Trim() -split "`n")[0]).Trim() }
}
if ($Title.Length -gt 60) { $Title = $Title.Substring(0, 60) + '...' }
# Run files live with the project (E:\qwen-projects\<slug>\.codex-runs\<id>), except when
# that folder is inside the job's git repo: they would turn up as untracked files in the
# owner's repo and in the next review.
$GitTop = Get-GitTop $WorkDir
$RunDir = Join-Path $LogDir ('runs\' + $JobId)
if ($Project) {
    $candidate = Join-Path $Project.dir ('.codex-runs\' + $JobId)
    if (-not (Test-Under $candidate $GitTop)) { $RunDir = $candidate }
}
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$eventsFile = Join-Path $RunDir 'events.jsonl'
$stderrFile = Join-Path $RunDir 'stderr.log'
$lastFile = Join-Path $RunDir 'last.md'
$promptCopy = Join-Path $RunDir 'prompt.md'
# A prompt the launcher parked in logs\codex-bridge\pending moves into the run folder.
$pendingDir = Join-Path $LogDir 'pending'
if ($PromptFile -and $PromptFile.StartsWith($pendingDir + '\', [StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $PromptFile -Force -ErrorAction SilentlyContinue
}
$jobTimer = [Diagnostics.Stopwatch]::StartNew()

$JobSource = if ($Source) { $Source } else { 'desktop' }
$result = [ordered]@{
    id = $JobId; kind = $Kind; title = $Title; status = 'failed'; exit_code = 1; mode = $JobMode; model = $Model
    cwd = $WorkDir; project = $(if ($Project) { $Project.slug } else { '' }); run_dir = $RunDir; source = $JobSource
    codex_exe = $CodexExe; codex_version = [string]$CodexVersion; queued_at = (Get-Date).ToString('o')
}
Add-JobEvent @{ event = 'queued'; id = $JobId; kind = $Kind; title = $Title; mode = $JobMode; model = $Model; cwd = $WorkDir; run_dir = $RunDir; pid = $PID; source = $JobSource }
Write-BridgeLog ("queued {0} mode={1} model={2} source={3} cwd={4} run_dir={5}" -f $Kind, $JobMode, $Model, $JobSource, $WorkDir, $RunDir)

$script:Outcome = $null
function Complete-Job([int]$Code, [string]$Status, [string]$ErrorText) {
    # Records the outcome and unwinds to the finally block (unload, unlock); the result is
    # reported after that, so the notification never arrives while the lock is still held.
    $script:Outcome = @{ code = $Code; status = $Status; error = $ErrorText }
    throw 'QWEN_CODEX_JOB_DONE'
}

$lock = $null
$codexProc = $null
try {
    if (-not (Test-OllamaUp)) { Complete-Job 2 'failed' "Ollama ไม่ตอบที่ $OllamaUrl - เปิด Qwen Suite ก่อนแล้วสั่งงานใหม่" }
    # One job at a time, then an idle Ollama. The lock comes first so two queued jobs never
    # both see "idle" and start together. Both waits share the -IdleWaitMinutes budget.
    $lock = Enter-BridgeLock $IdleWaitMinutes @{ id = $JobId; kind = $Kind; cwd = $WorkDir }
    if (-not $lock) { Complete-Job 4 'failed' ("มีงาน Codex อื่นทำอยู่นานเกิน {0} นาที ({1})" -f $IdleWaitMinutes, (Get-LockHolder)) }
    $lastReason = ''
    while ($true) {
        $reason = Get-OllamaBusyReason $Model ([int]$Settings.recent_activity_minutes)
        if (-not $reason) { break }
        if ($jobTimer.Elapsed.TotalMinutes -ge $IdleWaitMinutes) { Complete-Job 3 'failed' ("Ollama ไม่ว่างนานเกิน {0} นาที: {1}" -f $IdleWaitMinutes, $reason) }
        if ($reason -ne $lastReason) { Say ('รอให้ว่าง: ' + $reason); Write-BridgeLog ('waiting: ' + $reason); $lastReason = $reason }
        Start-Sleep -Seconds $PollSeconds
    }
    $result.wait_minutes = [Math]::Round($jobTimer.Elapsed.TotalMinutes, 1)

    # Policy hook: the job folders are marked untrusted in config.toml (no repo .codex config,
    # hooks or rules), and Codex must see our hook as trusted - else a write job is refused.
    $untrusted = Get-UntrustedDirs (@($WorkDir, $GitTop) + $AddDirs)
    Initialize-CodexHome $untrusted
    $hookProblem = Confirm-PolicyHook $WorkDir $untrusted
    if ($hookProblem) {
        Write-BridgeLog ('policy hook: ' + $hookProblem)
        if ($JobMode -ne 'read-only') { Complete-Job 2 'failed' ('ไม่เริ่มงานแก้ไขไฟล์เพราะ hook นโยบายคำสั่งไม่พร้อม: ' + $hookProblem) }
        $result.policy_hook = 'unverified: ' + $hookProblem
    } else { $result.policy_hook = 'trusted' }

    if ($Kind -eq 'review') {
        $lines = Get-ReviewDiffLines $WorkDir
        $result.diff_lines = $lines
        if ($lines -eq 0) { Complete-Job 2 'failed' 'ไม่มีการเปลี่ยนแปลงให้รีวิว' }
        if ($lines -gt [int]$Settings.max_review_diff_lines) {
            Complete-Job 8 'failed' ("diff ใหญ่เกิน ({0} บรรทัด เกิน {1}) แบ่งเป็นหลาย commit แล้วรีวิวทีละส่วน" -f $lines, $Settings.max_review_diff_lines)
        }
    }

    # ------------------------------------------------------------- the Codex command line
    $common = @('--strict-config', '--enable', 'hooks') + $sandboxArgs + @('-C', $WorkDir) + $providerOverride + $extraConfig
    if ($JobMode -ne 'read-only') {
        $writable = @($AddDirs)
        if ($Project) { New-Item -ItemType Directory -Force -Path $Project.outputs | Out-Null; $writable += $Project.outputs }
        foreach ($d in ($writable | Select-Object -Unique)) { if ($d -ne $WorkDir) { $common += @('--add-dir', $d) } }
    }
    $io = @('--json', '-o', $lastFile)
    if ($Ephemeral) { $io += '--ephemeral' }
    if ($OutputSchema) { $io += @('--output-schema', $OutputSchema) }

    if ($Kind -eq 'exec') {
        if ($JobMode -eq 'read-only') {
            $note = '[Job mode: read-only. Only basic read commands (Get-ChildItem, Get-Content, Select-String, git status/diff/log) run; anything that writes files or starts programs is rejected. Answer in text.]'
        } else {
            $note = "[Job mode: write. Work inside $WorkDir."
            if ($Project) { $note += " Put produced files in $($Project.outputs)." }
            $note += ' A safety policy checks every command; a blocked one returns its reason - do not retry it another way.]'
        }
        if ($OutputSchema) { $note += ' [The final answer must be only JSON that matches the given schema.]' }
        [System.IO.File]::WriteAllText($promptCopy, ($note + "`n`n" + $promptText.Trim() + "`n"), $Utf8)
        $codexArgs = @('exec') + $common
        if ($ResumeLast) { $codexArgs += @('resume', '--last') } elseif ($ResumeId) { $codexArgs += @('resume', $ResumeId) }
        $codexArgs += $io + @('--skip-git-repo-check', '-m', $Model, '-')
    } else {
        $scope = if ($Commit) { @('--commit', $Commit) } elseif ($Base) { @('--base', $Base) } else { @('--uncommitted') }
        [System.IO.File]::WriteAllText($promptCopy, $promptText, $Utf8)
        $codexArgs = @('exec') + $common + @('review') + $io + @('-m', $Model) + $scope
        # Custom review instructions come on stdin; without them Codex uses its own.
        if ($promptText.Trim()) { $codexArgs += '-' }
    }
    $result.codex_args = ($codexArgs | ForEach-Object { Format-NativeArg $_ }) -join ' '
    # Write jobs change files through shell commands (no apply_patch on 0.146, so no
    # file_change events): a size+mtime listing before and after tells what changed.
    $snapFile = Join-Path $RunDir 'files-before.json'
    if ($JobMode -ne 'read-only') {
        $snapRoots = @($WorkDir) + @($AddDirs)
        if ($Project -and -not (Test-Under $Project.outputs $WorkDir)) { $snapRoots += $Project.outputs }
        $snap = Invoke-Helper @('snapshot', $snapFile, (($snapRoots | Where-Object { $_ } | Select-Object -Unique) -join ';'))
        if (-not $snap -or (Get-Prop $snap 'ok') -ne $true) { Write-BridgeLog ('snapshot failed: ' + (Get-Prop $snap 'error')) }
    }
    Add-JobEvent @{ event = 'started'; id = $JobId; pid = $PID }
    Write-BridgeLog ('start codex: ' + $result.codex_args)
    Say ("เริ่มงาน {0} ({1}, {2}) ในโฟลเดอร์ {3}" -f $JobId, $Kind, $JobMode, $WorkDir)

    $script:HookRunDir = $RunDir
    $codexProc = Invoke-WithCodexEnv {
        Start-Process -FilePath $CodexExe -ArgumentList $result.codex_args -RedirectStandardInput $promptCopy -RedirectStandardOutput $eventsFile -RedirectStandardError $stderrFile -WorkingDirectory $WorkDir -NoNewWindow -PassThru
    }
    $script:HookRunDir = ''
    $null = $codexProc.Handle  # keeps ExitCode readable after exit (Windows PowerShell quirk)
    $result.codex_pid = $codexProc.Id
    $timeoutMs = [int][Math]::Min([double]$MaxMinutes * 60000, [double][int]::MaxValue)
    $timedOut = -not $codexProc.WaitForExit($timeoutMs)
    if ($timedOut) { Stop-ProcessTree $codexProc.Id }
    if (Test-Path -LiteralPath $snapFile) {
        $changed = Invoke-Helper @('changes', $snapFile)
        if ($changed -and (Get-Prop $changed 'ok') -eq $true) { $result.changed_files = $changed }
        Remove-Item -LiteralPath $snapFile -Force -ErrorAction SilentlyContinue
    }
    if ($timedOut) {
        $result.summary = Invoke-Helper @('summarize', $eventsFile)
        Complete-Job 7 'failed' ("เกินเวลาที่กำหนด {0} นาที หยุดงานแล้ว" -f $MaxMinutes)
    }
    $codexExit = $codexProc.ExitCode
    $result.codex_exit_code = $codexExit
    $result.summary = Invoke-Helper @('summarize', $eventsFile)

    if ($codexExit -ne 0) {
        $tail = (@(Get-Content -LiteralPath $stderrFile -Tail 5 -ErrorAction SilentlyContinue) | Where-Object { $_ -notmatch 'could not create PATH aliases' }) -join ' '
        if ($tail -match 'unknown configuration field') {
            # A Codex update renamed or dropped a key: --strict-config stops the job rather
            # than run with a safety setting silently ignored.
            Complete-Job 1 'failed' ("Codex รุ่น {0} ไม่รู้จักค่าบางตัวใน config.template.toml ต้องปรับแม่แบบก่อน: {1}" -f $CodexVersion, $tail.Trim())
        }
        Complete-Job 1 'failed' ("Codex จบด้วยรหัส {0}: {1}" -f $codexExit, $tail.Trim())
    }
    $errors = @(Get-Prop $result.summary 'errors' | Where-Object { $_ })
    if ($errors.Count -gt 0 -and -not (Test-Path -LiteralPath $lastFile)) {
        Complete-Job 1 'failed' ('Codex รายงานข้อผิดพลาด: ' + ($errors -join ' | '))
    }

    if ($OutputSchema) {
        if (-not (Test-Path -LiteralPath $lastFile)) { Complete-Job 6 'failed' 'ไม่มีคำตอบสุดท้ายให้ตรวจกับ schema' }
        # Codex 0.146 only forwards the schema to the server; it never checks the answer.
        $schemaCheck = Invoke-Helper @('validate', $OutputSchema, $lastFile)
        $result.schema_check = $schemaCheck
        if (-not $schemaCheck -or (Get-Prop $schemaCheck 'ok') -ne $true) {
            $why = if ($schemaCheck) { [string](Get-Prop $schemaCheck 'error') } else { 'ตรวจ schema ไม่ได้ (ไม่มี python)' }
            Complete-Job 6 'failed' ('คำตอบไม่ตรง schema: ' + $why)
        }
    }

    if ($Kind -eq 'review') {
        # Never inside the reviewed repo: the file would show up in the next review.
        $outDir = if ($Project -and -not (Test-Under $Project.outputs $GitTop)) { $Project.outputs } else { $RunDir }
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
        $reviewPath = Get-UniquePath $outDir ('review-' + (Get-Date).ToString('yyyyMMdd-HHmm')) '.md'
        $scopeText = if ($Commit) { "commit $Commit" } elseif ($Base) { "เทียบกับ branch $Base" } else { 'การแก้ไขที่ยังไม่ commit' }
        $body = if (Test-Path -LiteralPath $lastFile) { [System.IO.File]::ReadAllText($lastFile, $Utf8) } else { '(ไม่มีผลรีวิว)' }
        $md = @(
            '# รีวิวโค้ดโดย Qwen Codex', '',
            '> ความเห็นจากโมเดลในเครื่อง ต้องตรวจซ้ำ', '',
            "- โฟลเดอร์: $WorkDir", "- ขอบเขต: $scopeText", "- จำนวนบรรทัดที่เปลี่ยน: $($result.diff_lines)",
            "- โมเดล: $Model", ('- วันที่: ' + (Get-Date).ToString('yyyy-MM-dd HH:mm')), "- รหัสงาน: $JobId", '',
            '## ผลรีวิว', '', $body.Trim(), ''
        ) -join "`r`n"
        [System.IO.File]::WriteAllText($reviewPath, $md, $Utf8)
        $result.review_file = $reviewPath
        if ($Docx) {
            $doc = Invoke-Helper @('docx', $HelperToolsDir, $reviewPath, $outDir, ('รีวิวโค้ด ' + (Split-Path -Leaf $WorkDir)))
            $docPath = Get-Prop $doc 'path'
            if ($docPath) { $result.review_docx = $docPath } else { Write-BridgeLog ('docx for the review was not created: ' + (Get-Prop $doc 'error')) }
        }
    }
    Complete-Job 0 'completed' ''
} catch {
    if (-not $script:Outcome) {
        $script:Outcome = @{ code = 1; status = 'failed'; error = ('ข้อผิดพลาดภายใน bridge: ' + $_.Exception.Message) }
    }
} finally {
    if ($codexProc -and -not $codexProc.HasExited) { Stop-ProcessTree $codexProc.Id }
    # Only a run that started Codex unloads: after a lock timeout (4) or a busy Ollama (3)
    # the loaded qwen3-coder belongs to another bridge job, a TUI or a coder chat.
    if ($codexProc -and -not $KeepModel -and $Settings.unload_after) { Invoke-ModelUnload $Model }
    if ($lock) { $lock.Dispose() }
}

# ------------------------------------------------------------------------- report
$code = [int]$script:Outcome.code
$status = [string]$script:Outcome.status
$errorText = [string]$script:Outcome.error
$result.exit_code = $code
$result.status = $status
if ($errorText) { $result.error = $errorText }
$result.finished_at = (Get-Date).ToString('o')
$result.minutes = [Math]::Round($jobTimer.Elapsed.TotalMinutes, 1)
try { [System.IO.File]::WriteAllText((Join-Path $RunDir 'result.json'), ($result | ConvertTo-Json -Depth 8), $Utf8) } catch { }
$final = [string](Get-Prop $result.summary 'final_message')
$finalShort = if ($final.Length -gt 1000) { $final.Substring(0, 1000) + ' ...' } else { $final }
# touched_sensitive_paths: list_codex_jobs then shows no part of this answer in chat.
$finishedEvent = @{ event = 'finished'; id = $JobId; status = $status; exit_code = $code; minutes = $result.minutes; final_message = $finalShort; error = $errorText; touched_sensitive_paths = [bool](Get-Prop $result.summary 'touched_sensitive_paths') }
if ($result.Contains('changed_files')) { $finishedEvent['files_changed_count'] = [int](Get-Prop $result.changed_files 'total') }
Add-JobEvent $finishedEvent
Write-BridgeLog ("finished status={0} exit={1} minutes={2} {3}" -f $status, $code, $result.minutes, $errorText)
if (-not $NoNotify -and $Settings.notify) {
    $spec = Join-Path $RunDir 'notify.json'
    $notifyStatus = if ($status -eq 'completed') { 'completed' } else { 'failed' }
    [System.IO.File]::WriteAllText($spec, (@{ tools_dir = $HelperToolsDir; title = ('Qwen Codex: ' + $Title); status = $notifyStatus; minutes = $result.minutes; dry_run = [bool]$NotifyDryRun; source = $Source; job_id = $JobId } | ConvertTo-Json -Compress), $Utf8)
    $sent = Invoke-Helper @('notify', $spec)
    if ($sent) { Write-BridgeLog ('notify: ' + ($sent | ConvertTo-Json -Compress)) }
    Remove-Item -LiteralPath $spec -Force -ErrorAction SilentlyContinue
}
if ($JsonOut) {
    Write-Output ($result | ConvertTo-Json -Depth 8 -Compress)
} else {
    $color = if ($status -eq 'completed') { 'Green' } else { 'Yellow' }
    Write-Host ("งาน {0}: {1} ({2} นาที) - {3}" -f $JobId, $(if ($status -eq 'completed') { 'เสร็จแล้ว' } else { 'ไม่สำเร็จ' }), $result.minutes, $RunDir) -ForegroundColor $color
    if ($errorText) { Write-Host $errorText -ForegroundColor Yellow }
    if ($result.Contains('review_file')) { Write-Host ('ไฟล์รีวิว: ' + $result.review_file) }
    if ($finalShort) { Write-Host $finalShort }
}
exit $code
