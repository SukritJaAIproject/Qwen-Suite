# Choose which processing unit the local LLM (Ollama) runs on: automatic, the CPU, or a
# specific GPU. Added 2026-09-29.
#
#   Select-QwenCompute.ps1                  menu (the "Qwen Compute Device" shortcut)
#   Select-QwenCompute.ps1 -List            show the devices and the current choice
#   Select-QwenCompute.ps1 -Device auto     let the suite decide (eGPU if one is attached)
#   Select-QwenCompute.ps1 -Device cpu      CPU only
#   Select-QwenCompute.ps1 -Device 4090     a GPU by list number, part of its name, or UUID
#                                           (several: -Device 4090,4060)
#   -Apply now   restart Ollama right away (default for the interactive menu)
#   -Apply idle  let the watcher restart Ollama once nothing is running (default with -Device,
#                so a command from chat never cuts off an answer that is being written)
#   -Hours 4     the choice falls back to auto after that many hours
#   -Json        machine-readable output (used by the web page and the chat tool)
#
# The choice is saved in data\compute-selection.json and survives restarts and reboots.
# Resolve-OllamaGpu.ps1 applies it every time the server starts; if a chosen GPU is not
# attached, the automatic choice is used until it is back. A card Windows shows but cannot
# use (disabled in Device Manager - the P620 on path B - or without a working driver) is
# listed once, greyed out, and cannot be chosen. A User OLLAMA_LLM_LIBRARY or
# CUDA_VISIBLE_DEVICES still overrides everything (shown as "manual").

param(
    [string[]]$Device,
    [ValidateSet('', 'now', 'idle', 'none')][string]$Apply = '',
    [double]$Hours = 0,
    [switch]$List,
    [switch]$Json,
    [string]$By = 'Select-QwenCompute.ps1'
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
. (Join-Path $Root 'Resolve-OllamaGpu.ps1')

function Get-ComputeDevices {
    # CPU first, then every NVIDIA GPU nvidia-smi can describe, then NVIDIA devices that
    # Windows sees but cannot use (no driver, disabled, error).
    $devices = New-Object System.Collections.Generic.List[object]
    $cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
    $ram = (Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).TotalPhysicalMemory
    $devices.Add([pscustomobject]@{
        Number = 0; Kind = 'cpu'; Id = 'cpu'
        Name = if ($cpu) { ([string]$cpu.Name).Trim() } else { 'CPU' }
        Detail = ('{0} cores / {1} threads, {2:N0} GB RAM' -f $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors, ($ram / 1GB))
        Usable = $true; Note = ''
    })
    $inventory = Get-QwenGpuInventory
    $n = 1
    foreach ($g in @($inventory.Gpus)) {
        $note = ''
        if ($g.ComputeCap -lt $script:QwenGpuMinComputeCap -or $g.MemoryMiB -lt $script:QwenGpuMinMemoryMiB) { $note = 'small or old card: auto does not use it next to a bigger one (it can still be chosen)' }
        $devices.Add([pscustomobject]@{
            Number = $n; Kind = 'gpu'; Id = $g.Uuid; Name = $g.Name
            Detail = ('{0:N1} GB VRAM, compute {1}, driver {2}, PCI {3}' -f ($g.MemoryMiB / 1024), $g.ComputeCap, $g.Driver, $g.PciBus)
            Usable = $true; Note = $note
        })
        $n++
    }
    # Windows says "NVIDIA Quadro P620" where nvidia-smi says "Quadro P620".
    $listed = @($inventory.Gpus | ForEach-Object { $_.Name })
    # (assigned first: the function returns its list with a leading comma, and @() around the
    # call itself would make that list one element - every device merged into one entry)
    $pnpDevices = Get-QwenNvidiaPnpDevices

    # Cards seen before are remembered in data\compute-devices-seen.json with their PCI
    # device id (found through the PCI bus), because Windows renames a card without a driver
    # "Microsoft Basic Display Adapter" and only that id still tells which card it is.
    $seenPath = Join-Path $Root 'data\compute-devices-seen.json'
    $seen = Read-QwenSeenDevices -Root $Root
    $working = @(@($pnpDevices) | Where-Object { $_ -and $_.ErrorCode -eq 0 -and $_.Service -ieq 'nvlddmkm' })
    $byBus = if ($working.Count -gt 0 -and @($inventory.Gpus).Count -gt 0) { Get-QwenGpuExternalFlags -Devices $working } else { @{} }
    $changed = $false
    foreach ($g in @($inventory.Gpus)) {
        $bus = -1
        try { $bus = [Convert]::ToInt32((([string]$g.PciBus) -split ':')[-2], 16) } catch { }
        $old = if ($seen.Contains($g.Uuid)) { $seen[$g.Uuid] } else { $null }
        $dev = if ($byBus.ContainsKey($bus)) { Get-QwenPciDeviceId ([string]$byBus[$bus].PnpId) } else { $null }
        if (-not $dev -and $old) { $dev = [string]$old.pci_dev }
        if (-not $old -or [string]$old.name -ne $g.Name -or [string]$old.pci_dev -ne [string]$dev) { $changed = $true }
        $seen[$g.Uuid] = [ordered]@{ name = $g.Name; memory_mib = $g.MemoryMiB; pci_dev = $dev; last_seen = (Get-Date).ToString('yyyy-MM-dd') }
    }
    if ($changed -and $inventory.SmiExitCode -eq 0) {
        try {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $seenPath) | Out-Null
            [void](Write-QwenGpuProfileText -Path $seenPath -Text (ConvertTo-Json -InputObject $seen -Depth 4) -KeepOnFailure)
        } catch { }
    }
    $present = @($inventory.Gpus | ForEach-Object { $_.Uuid })
    $notListed = @($seen.Keys | Where-Object { $present -notcontains $_ })
    # Path B = a kernel driver of the 590 branch or later, which cannot run the P620. Only
    # then is the P620 "unsupported"; on path A it is an ordinary card.
    $pathB = (Get-QwenNvidiaKernelDriver).Major -ge 590
    # uuid -> $null: Windows shows that card but it cannot run (listed once, as unusable);
    # uuid -> text: attached and working, nvidia-smi just has not listed it yet.
    $seenState = @{}
    foreach ($p in @($pnpDevices)) {
        if (-not $p) { continue }
        $known = @($listed | Where-Object { $p.Name -like "*$_*" -or $_ -like "*$($p.Name)*" }).Count -gt 0
        if ($p.ErrorCode -eq 0 -and $p.Service -ieq 'nvlddmkm' -and $known) { continue }
        $same = @($notListed | Where-Object { Test-QwenSameCard -Name ([string]$seen[$_].name) -PciDev ([string]$seen[$_].pci_dev) -Device $p })
        # The Quadro P620 (DEV_1CBD) needs the R580 driver branch, which the RTX cards' current
        # driver replaces: on path B it stays disabled, and its old driver must not come back.
        $p620 = $pathB -and ((Get-QwenPciDeviceId ([string]$p.PnpId)) -eq '1CBD')
        if ($p.ErrorCode -eq 0 -and $p.Service -ieq 'nvlddmkm' -and -not $p620) {
            # Working in Windows, but nvidia-smi does not list it (yet): usually a card still
            # initialising after a plug-in. A remembered card stays choosable.
            if ($same.Count -gt 0) { foreach ($u in $same) { $seenState[$u] = 'attached; nvidia-smi does not list it yet - can be chosen' }; continue }
            $devices.Add([pscustomobject]@{ Number = $null; Kind = 'unavailable'; Id = [string]$p.PnpId; Uuid = $null; Name = [string]$p.Name; Detail = 'nvidia-smi does not list it'; Usable = $false; Note = '' })
            continue
        }
        $why = if ($p.ErrorCode -eq 22) { 'disabled in Device Manager' + $(if ($p620) { ' (path B: the current NVIDIA driver does not support it)' } else { '' }) }
            elseif ($p620) { 'not supported by the installed NVIDIA driver (R580 was the last for it) - disable it in Device Manager; do not install the Dell driver, it would break the RTX' }
            elseif ($p.Service -ieq 'BasicDisplay') { 'no NVIDIA driver for this card yet' }
            else { "Windows error code $($p.ErrorCode)" }
        foreach ($u in $same) { $seenState[$u] = $null }
        $uuid = if ($same.Count -gt 0) { [string]$same[0] } else { $null }
        # "Microsoft Basic Display Adapter" alone does not say which card it is.
        $name = if ($uuid -and $p.Service -ieq 'BasicDisplay') { '{0} ({1})' -f $p.Name, $seen[$uuid].name } else { [string]$p.Name }
        $devices.Add([pscustomobject]@{ Number = $null; Kind = 'unavailable'; Id = [string]$p.PnpId; Uuid = $uuid; Name = $name; Detail = $why; Usable = $false; Note = '' })
    }
    # Cards seen before but not attached now (an eGPU in its box) can still be chosen: the
    # choice waits for them.
    foreach ($uuid in $notListed) {
        if ($seenState.ContainsKey($uuid) -and $null -eq $seenState[$uuid]) { continue }
        $s = $seen[$uuid]
        $detail = if ($seenState[$uuid]) { $seenState[$uuid] } else { 'not attached now ({0:N1} GB, last seen {1}) - can be chosen; used once plugged in' -f ([double]$s.memory_mib / 1024), $s.last_seen }
        $devices.Add([pscustomobject]@{ Number = $null; Kind = 'absent'; Id = $uuid; Name = [string]$s.name; Detail = $detail; Usable = $true; Note = '' })
    }
    # A plain array (@() on a List[object] throws "Argument types do not match" in Windows
    # PowerShell 5.1), returned with a comma so a one-row list - the CPU alone - stays a list
    # for ConvertTo-Json.
    return ,($devices.ToArray())
}

function Test-ChoiceInEffect($record, [string]$wantKey, $devices) {
    # Is the running server really doing what was chosen? Keys alone are not enough: a
    # chosen card can be missing at start (then auto runs) and come back later.
    if (-not $record) { return $false }
    $haveKey = if ($record.selection_key) { [string]$record.selection_key } else { 'auto' }
    if ($haveKey -ne $wantKey) { return $false }
    if ($wantKey -eq 'cpu') { return ($record.mode -eq 'cpu') }
    if ($wantKey -like 'gpu:*') {
        $present = @($devices | Where-Object { $_.Kind -eq 'gpu' } | ForEach-Object { $_.Id })
        $chosen = @($wantKey.Substring(4) -split ',')
        if ($record.mode -ne 'selected') {
            # None of the chosen cards can run now (in its box, disabled, no driver): the
            # automatic choice runs on purpose and nothing is waiting to be applied. Once one
            # is attached and usable, the watcher's device check restarts onto it.
            return (@($chosen | Where-Object { $present -contains $_ }).Count -eq 0)
        }
        $used = @($record.gpus_used | Where-Object { $_ } | ForEach-Object { [string]$_.uuid })
        foreach ($c in $chosen) { if ($present -contains $c -and $used -notcontains $c) { return $false } }
    }
    return $true
}

function Get-ComputeState($devices) {
    $sel = Read-QwenComputeSelection -Root $Root
    $record = Read-QwenGpuProfile -Root $Root
    $manual = @('OLLAMA_LLM_LIBRARY', 'CUDA_VISIBLE_DEVICES') | Where-Object { Get-QwenConfiguredValue -Name $_ }
    $legacy = [string][Environment]::GetEnvironmentVariable('QWEN_GPU_MODE', 'User')
    $choice = if (-not $sel -or $sel.device -eq 'auto' -or (Test-QwenSelectionExpired $sel)) { 'auto' } elseif ($sel.device -eq 'cpu') { 'cpu' } else {
        $seenNames = Read-QwenSeenDevices -Root $Root
        $names = @($sel.gpus | ForEach-Object {
            $u = [string]$_
            $d = $devices | Where-Object { $_.Id -eq $u -or ($_.Uuid -and $_.Uuid -eq $u) } | Select-Object -First 1
            if ($d -and $d.Kind -eq 'gpu') { $d.Name }
            elseif ($d -and $d.Kind -eq 'unavailable') { "$($d.Name) (cannot be used now)" }
            elseif ($d -and $d.Detail -like 'attached;*') { "$($d.Name) (attached, not ready yet)" }
            elseif ($d) { "$($d.Name) (not attached)" }
            elseif ($seenNames.Contains($u)) { "$($seenNames[$u].name) (not attached)" }
            else { "$u (not attached)" }
        })
        'gpu: ' + ($names -join ', ')
    }
    $running = 'unknown'
    if ($record) {
        $running = switch ([string]$record.profile) {
            'gpu' { 'GPU: ' + ((@($record.gpus_used) | Where-Object { $_ } | ForEach-Object { $_.name }) -join ', ') }
            'cpu' { 'CPU only' }
            'internal' { if ($record.cpu_only -eq $true) { 'CPU only (automatic: no working NVIDIA GPU)' } else { 'automatic (no eGPU in use: Ollama uses what it finds - on path A the internal P620 helps read prompts)' } }
            default { [string]$record.profile }
        }
    }
    $owner = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -First 1
    $wantKey = Get-QwenSelectionKey $sel
    $applied = $record -and $owner -and ([string]$record.server_pid -eq [string]$owner) -and (@($manual).Count -eq 0) -and (Test-ChoiceInEffect $record $wantKey $devices)
    # A GPU was chosen but none of the chosen cards can run now: automatic runs instead
    # (only when that is what the running server really does - never next to 'pending').
    $fallback = [bool]$applied -and [bool]($wantKey -like 'gpu:*' -and $record.mode -ne 'selected')
    [pscustomobject]@{
        choice = $choice
        selection = $sel
        running = $running
        applied = [bool]$applied
        fallback = $fallback
        server_running = [bool]$owner
        manual_override = @($manual)
        expires_at = $(if ($sel -and $sel.expires_at -and -not (Test-QwenSelectionExpired $sel)) { [string]$sel.expires_at } else { $null })
        legacy_mode = $(if ($legacy -and $legacy -ne 'auto') { $legacy } else { $null })
        note = $(if ($record) { [string]$record.note } else { '' })
    }
}

function Resolve-DeviceArgs($devices, [string[]]$wanted) {
    # auto / cpu / list numbers / name fragments / UUIDs -> @{ device; gpus; labels }
    # Rules: auto/cpu alone; 1-2 digit tokens are list numbers only (0 = CPU, alone); anything
    # else is a GPU id or part of a GPU name (4090, RTX 4060, P620), attached or seen before.
    $wanted = @($wanted | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $lower = @($wanted | ForEach-Object { $_.ToLowerInvariant() })
    if ($wanted.Count -eq 1 -and $lower[0] -in @('auto', 'cpu', '0')) { return @{ device = $(if ($lower[0] -eq 'auto') { 'auto' } else { 'cpu' }); gpus = @(); labels = @(); absent = @(); attached = @() } }
    if (@($lower | Where-Object { $_ -in @('auto', 'cpu', '0') }).Count -gt 0) { throw 'auto and cpu (0) cannot be combined with GPUs' }
    $gpus = @($devices | Where-Object { $_.Kind -eq 'gpu' })
    $absent = @($devices | Where-Object { $_.Kind -eq 'absent' })
    $unavailable = @($devices | Where-Object { $_.Kind -eq 'unavailable' })
    # A card Windows shows but that cannot run (the P620 on path B, a card without a driver):
    # refused with the reason instead of a choice that could never apply.
    $picked = @()
    foreach ($w in $wanted) {
        if ($w -match '^\d{1,2}$') {
            $hit = @($gpus | Where-Object { [string]$_.Number -eq $w })
            if ($hit.Count -ne 1) { throw ("there is no GPU number {0} in the list" -f $w) }
        } else {
            $hit = @($gpus | Where-Object { $_.Id -ieq $w })
            if ($hit.Count -eq 0) { $hit = @($gpus | Where-Object { $_.Name -like "*$w*" }) }
            if ($hit.Count -eq 0) { $hit = @($absent | Where-Object { $_.Id -ieq $w }) }
            if ($hit.Count -eq 0) { $hit = @($absent | Where-Object { $_.Name -like "*$w*" }) }
            if ($hit.Count -eq 0) {
                # Windows shows it but it cannot run (the P620 on path B): say why.
                $bad = @($unavailable | Where-Object { $_.Id -ieq $w -or ($_.Uuid -and $_.Uuid -ieq $w) -or $_.Name -like "*$w*" })
                if ($bad.Count -gt 0) { throw ("{0} cannot be used: {1}" -f $bad[0].Name, $bad[0].Detail) }
            }
            if ($hit.Count -eq 0) { throw ("no attached or previously seen GPU matches '{0}' (plug it in once so it is known, or use its full name)" -f $w) }
            if ($hit.Count -gt 1) { throw ("'{0}' matches {1} GPUs ({2}); be more specific" -f $w, $hit.Count, (($hit | ForEach-Object { $_.Name }) -join ', ')) }
        }
        $picked += $hit[0]
    }
    return @{ device = 'gpu'; gpus = @($picked | ForEach-Object { $_.Id } | Select-Object -Unique); labels = @($picked | ForEach-Object { $_.Name } | Select-Object -Unique)
              absent = @($picked | Where-Object { $_.Kind -eq 'absent' -and $_.Detail -notlike 'attached;*' } | ForEach-Object { $_.Name })
              attached = @($picked | Where-Object { $_.Kind -eq 'absent' -and $_.Detail -like 'attached;*' } | ForEach-Object { $_.Name }) }
}

function Get-WaitText($pick) {
    # Why a chosen card is not running yet: in its box, or attached but not ready.
    $parts = @()
    if (@($pick.absent).Count -gt 0) { $parts += (($pick.absent -join ', ') + ' is not attached now: the automatic choice runs until it is plugged in, then it switches by itself.') }
    if (@($pick.attached).Count -gt 0) { $parts += (($pick.attached -join ', ') + ' is attached, but nvidia-smi does not list it yet: the automatic choice runs until it does (the watcher checks once more when idle; if it stays like this, run Restart-QwenOllama.ps1).') }
    return ($parts -join ' ')
}

function Invoke-Apply([string]$how, [string]$reason) {
    $restart = Join-Path $Root 'Restart-QwenOllama.ps1'
    switch ($how) {
        'now' {
            & $restart -Reason $reason *> $null
            return "Ollama restarted with the new choice (exit $LASTEXITCODE)"
        }
        'idle' {
            # The watcher sees the changed selection and restarts once nothing is running.
            # Start-QwenGpuWatch starts one if none runs and replaces one from older code.
            Start-QwenGpuWatch -Root $Root
            return 'saved; the watcher applies it when Ollama is idle (usually within 2 minutes after the current answer)'
        }
        default { return 'saved; the watcher applies it when Ollama is idle, or at the next Ollama start' }
    }
}

function Write-Report($devices, $state, [string]$message) {
    if ($Json) {
        [pscustomobject]@{ status = $script:Status; devices = $devices; state = $state; message = $message } | ConvertTo-Json -Depth 6 -Compress
        return
    }
    Write-Host ''
    Write-Host 'Processing units for the local LLM' -ForegroundColor Cyan
    foreach ($d in $devices) {
        $num = if ($null -ne $d.Number) { '[{0}]' -f $d.Number } else { '[-]' }
        $color = if ($d.Usable) { 'White' } else { 'DarkGray' }
        Write-Host ('  {0,-4} {1}  -  {2}' -f $num, $d.Name, $d.Detail) -ForegroundColor $color
        if ($d.Kind -eq 'gpu') { Write-Host ('        ' + $d.Id) -ForegroundColor DarkGray }
        if ($d.Note) { Write-Host ('        ' + $d.Note) -ForegroundColor DarkGray }
    }
    Write-Host ''
    Write-Host ('  Chosen : {0}{1}' -f $state.choice, $(if ($state.expires_at) { "  (until $($state.expires_at), then auto)" } else { '' }))
    Write-Host ('  Running: {0}{1}' -f $state.running, $(if ($state.applied -and $state.fallback) { '  (the chosen GPU cannot run now - automatic choice instead)' } elseif ($state.applied) { '' } elseif ($state.server_running) { '  (not applied yet - restart pending)' } else { '  (Ollama is not running)' }))
    if ($state.note) { Write-Host ('  Note   : ' + $state.note) -ForegroundColor Yellow }
    if (@($state.manual_override).Count -gt 0) { Write-Host ('  Manual override in the User environment: {0} - it wins over this choice; remove it to use this menu' -f ($state.manual_override -join ', ')) -ForegroundColor Yellow }
    if ($state.legacy_mode) { Write-Host ("  User QWEN_GPU_MODE={0} applies while the choice is 'auto'" -f $state.legacy_mode) -ForegroundColor DarkGray }
    if ($message) { Write-Host ''; Write-Host ('  ' + $message) -ForegroundColor Green }
    Write-Host ''
}

$devices = Get-ComputeDevices
$message = ''
$script:Status = 'listed'
try {
    if (-not $Device -and -not $List -and -not $Json -and [Environment]::UserInteractive) {
        Write-Report $devices (Get-ComputeState $devices) ''
        Write-Host '  Type a number (0 = CPU), several numbers separated by commas, "auto", or Enter to keep:' -NoNewline
        $answer = Read-Host ' '
        if ($answer) {
            $Device = @($answer)
            if (-not $Apply) { $Apply = 'now' }   # someone at the keyboard wants it now
        }
    }
    if ($Device) {
        if (-not $Apply) { $Apply = 'idle' }
        $pick = Resolve-DeviceArgs $devices $Device
        $saved = Save-QwenComputeSelection -Root $Root -Device $pick.device -Gpus $pick.gpus -Labels $pick.labels -Hours $Hours -By $By
        $what = if ($pick.device -eq 'gpu') { 'GPU: ' + ($pick.labels -join ', ') } else { $pick.device }
        $state = Get-ComputeState $devices
        $until = if ($Hours -gt 0) { " for $Hours h (then auto)" } else { '' }
        if (@($state.manual_override).Count -gt 0) {
            $script:Status = 'saved_but_overridden'
            $message = "Chose $what, but the User setting " + ($state.manual_override -join ', ') + ' overrides it; remove that for this choice to take effect.'
        } elseif ($state.applied -and $state.fallback) {
            # Chosen again while it still cannot run: nothing to restart, and not "in use".
            $script:Status = 'waiting'
            $wait = Get-WaitText $pick
            if (-not $wait) { $wait = 'It cannot run now; the automatic choice runs instead.' }
            $message = "Chose $what$until. $wait"
        } elseif ($state.applied) {
            # The running server already does exactly this: nothing to restart.
            if ($saved.changed -and $Hours -gt 0) { $script:Status = 'extended'; $message = "$what is already in use; it now stays$until." }
            else { $script:Status = 'unchanged'; $message = "$what is already the processing unit in use." }
        } else {
            $script:Status = 'saved'
            $wait = Get-WaitText $pick
            if ($wait) { $wait = ' ' + $wait }
            $message = "Chose $what$until.$wait " + (Invoke-Apply $Apply "processing unit set to $what by $By")
        }
        if ($pick.device -eq 'auto' -and $state.legacy_mode) { $message += " Note: User QWEN_GPU_MODE=$($state.legacy_mode) applies while the choice is auto." }
    }
} catch {
    $script:Status = 'error'
    $message = 'ERROR: ' + $_.Exception.Message
    if (-not $Json) { Write-Host "  $message" -ForegroundColor Red }
}
Write-Report $devices (Get-ComputeState $devices) $message
if ($message -like 'ERROR:*') { exit 1 }
exit 0
