# Restart the Ollama server when an eGPU is plugged in or pulled out, so it uses the card
# that is actually there. Added 2026-09-29.
#
# Ollama looks for GPUs once, at server start (0.32.x has a "TODO hotplug" in its
# discovery code). Without this, a card plugged in after logon is ignored until the next
# restart, and a card pulled out leaves the server pointing at a device that is gone.
#
# Every $IntervalSeconds:
#  - Nothing listening on 11434: do nothing - it never starts a server someone stopped
#    on purpose (stop-all.ps1) - unless Restart-QwenOllama.ps1 left its marker
#    (data\ollama-restart.json), i.e. a restart was killed between stop and start.
#  - The record says vram_class "pending": look in the server log for the card and
#    promote it to large/small (the router's big-GPU rule waits for this), or to "none"
#    if the server did not find it.
#  - Reasons to restart Ollama:
#      the running server was not started through Resolve-OllamaGpu.ps1 (pid mismatch);
#      the NVIDIA devices changed (PnP instance, driver, problem code) - a device the
#        server can be using disappeared or stopped working = urgent (the server is broken
#        already): a card it was given, or on 'internal' any NVIDIA card that worked at its
#        start (the P620 on path A). Anything else waits for idle, and a change that would
#        not alter the setup (a disabled or driverless card changing state, e.g. during a
#        driver install) only updates the record;
#      the machine slept while a removable (Thunderbolt) GPU was in use - the link resets
#        over Modern Standby - remembered until one restart has been tried;
#      the processing-unit selection (data\compute-selection.json) changed - applied once
#        the server is idle, so a chat that asked for it can finish its answer first;
#      nvidia-smi could not describe the cards at start - tried once more per device state
#        and error (not per start: a failure that persists would otherwise restart again
#        after every retry); the device list could not be read - tried once more per start.
#  - A server the watcher did not start is adopted, not restarted, when a fresh start
#    would decide exactly what it already runs with (the usual no-eGPU case); a server
#    owned by the Ollama tray app is left alone and logged.
# A reason has to be seen on two checks in a row (Thunderbolt enumerates in steps).
# Rate limit: at most three restarts per 30 minutes; an urgent restart may skip it once
# per device change per 30 minutes. A restart that found the server busy (exit 3) is not
# counted and is simply tried again. After two restarts in a row that did not clear their
# reason, that reason is ignored until it goes away.
#
# Reading PnP state does not wake the GPU; nvidia-smi only runs inside the launcher.
# Started by Start-QwenGpuWatch (Resolve-OllamaGpu.ps1) whenever a launcher runs;
# stopped by stop-all.ps1 and Stop-Qwen-Suite.ps1. Log: logs\ollama-gpu-watch.log.

param(
    [int]$IntervalSeconds = 45,
    [int]$IdleWaitSeconds = 600
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
. (Join-Path $Root 'Resolve-OllamaGpu.ps1')

$mutex = New-Object System.Threading.Mutex($false, $script:QwenGpuWatchMutexName)
$owns = $false
try { $owns = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owns = $true }
if (-not $owns) { $mutex.Dispose(); exit 0 }

$logPath = Join-Path $Root 'logs\ollama-gpu-watch.log'
function Write-WatchLog([string]$Message) {
    try {
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
            Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
        }
        Add-Content -LiteralPath $logPath -Encoding UTF8 -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    } catch { }
}

function Get-ServePid {
    $owner = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -First 1
    if (-not $owner) { return $null }
    $proc = Get-Process -Id $owner -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -ieq 'ollama') { return [int]$owner }
    return $null
}

function Get-ServeParentName([int]$ServePid) {
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ServePid" -ErrorAction Stop
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction Stop
        return [string]$parent.Name
    } catch { return '' }
}

function Test-ExternalGpuInUse($record) {
    # The sleep rule is for Thunderbolt cards only; an internal RTX survives sleep fine.
    return @($record.gpus_used | Where-Object { $_ -and $_.external -eq $true }).Count -gt 0
}

function Test-AdoptRunningServer([int]$ServePid) {
    # The running server was not started through the GPU step (first run after this was
    # installed, or a start by hand). If a fresh start would decide exactly what that server
    # already runs with - no eGPU, profile 'internal', no GPU variables - record it instead
    # of restarting it and unloading its models for nothing.
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ServePid" -ErrorAction Stop
        $logs = @('ollama-server.log', 'ollama-error.log') | ForEach-Object { Join-Path $Root "logs\$_" } |
            Where-Object { Test-Path -LiteralPath $_ } | Sort-Object { (Get-Item -LiteralPath $_).LastWriteTime } -Descending
        $log = $logs | Select-Object -First 1
        if (-not $log) { return $false }
        $facts = Get-QwenOllamaLogFacts -LogPath $log
        if (-not $facts.Config) { return $false }
        # Decide into a scratch folder: if the answer is not "same as running", the real
        # record must not describe a server that does not exist.
        $scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('qwen-adopt-' + [guid]::NewGuid().ToString('N'))
        try {
            $saved = Resolve-QwenGpuProfile -Root $Root -OutRoot $scratch -OllamaExe ([string]$proc.ExecutablePath) -Launcher 'adopted by watch-ollama-gpu.ps1' -ServerLog $log
            Restore-QwenGpuEnvironment $saved
            $record = Read-QwenGpuProfile -Root $scratch
            $scratchText = if ($record) { [System.IO.File]::ReadAllText((Get-QwenGpuProfilePath -Root $scratch)) } else { $null }
        } finally {
            Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (-not $record -or $record.profile -ne 'internal') { return $false }
        foreach ($name in 'CUDA_VISIBLE_DEVICES', 'GGML_VK_VISIBLE_DEVICES', 'OLLAMA_LLM_LIBRARY') {
            $want = [string]$record.env.$name
            $have = if ($facts.Config -match "$name`:([^ \]]*)") { $Matches[1] } else { $null }
            if ($null -eq $have -or $have -ne $want) { return $false }
        }
        $vulkanWant = switch ([string]$record.env.OLLAMA_VULKAN) { '0' { 'false' } 'false' { 'false' } default { 'true' } }
        if ($facts.Config -match 'OLLAMA_VULKAN:(\w+)' -and $Matches[1] -ne $vulkanWant) { return $false }
        $real = Get-QwenGpuProfilePath -Root $Root
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $real) | Out-Null
        if (-not (Write-QwenGpuProfileText -Path $real -Text $scratchText)) { return $false }
        if (-not (Set-QwenGpuProfileServerPid -Root $Root -ServerPid $ServePid)) { return $false }
        Write-WatchLog "adopted the running server (pid $ServePid): a restart would give the same setup (profile internal, log $log)"
        return $true
    } catch { return $false }
}

function Update-PendingVram($record) {
    # Promote "pending" only when the server log shows the card with real memory.
    $facts = Get-QwenOllamaLogFacts -LogPath ([string]$record.server_log)
    if (-not $facts.Discovered) { return }
    $names = @($record.gpus_used | Where-Object { $_ } | ForEach-Object { [string]$_.name })
    $found = @($facts.GpuLines | Where-Object { $names -contains $_.Description -and $_.Total -ne '0 B' })
    if ($found.Count -gt 0 -and -not $facts.ZeroVram) {
        if (Set-QwenGpuProfileValue -Root $Root -Name 'vram_class' -Value ([string]$record.vram_expected)) {
            Write-WatchLog ("server found {0} ({1}); vram_class -> {2}" -f $found[0].Description, $found[0].Total, $record.vram_expected)
        }
    } else {
        $seen = (@($facts.GpuLines | ForEach-Object { $_.Description }) -join ', ')
        if (-not $seen) { $seen = 'CPU only' }
        if (Set-QwenGpuProfileValue -Root $Root -Name 'vram_class' -Value 'none') {
            Write-WatchLog ("WARNING: the server did not get the eGPU (it found: {0}{1}). Driver problem or Ollama issue #18581 - run Test-QwenGpu.ps1" -f $seen, $(if ($facts.ZeroVram) { '; 0 B VRAM' } else { '' }))
        }
    }
}

# Intervals come from the tick count (ms since boot): it keeps counting through sleep,
# so a resume shows up as a gap, and it ignores wall-clock corrections (this laptop's clock
# once ran 8.5 h fast) that would otherwise freeze or fake the timing below.
function Get-Tick { return [long][Environment]::TickCount }
function Get-TickSeconds([long]$From) {
    $d = (Get-Tick) - $From
    if ($d -lt 0) { $d += 4294967296 }  # TickCount wraps every 49.7 days
    return $d / 1000.0
}

$restartScript = Join-Path $Root 'Restart-QwenOllama.ps1'
$markerPath = Get-QwenRestartMarkerPath -Root $Root
$pendingKey = $null
$restarts = New-Object System.Collections.Generic.List[long]
# An urgent restart may skip the rate limit once per device change per 30 minutes.
$urgentTransitions = @{}
$inventoryRetried = New-Object System.Collections.Generic.HashSet[string]
$unknownRetried = New-Object System.Collections.Generic.HashSet[string]
$adoptTried = New-Object System.Collections.Generic.HashSet[string]
$selectionDryRun = New-Object System.Collections.Generic.HashSet[string]
$deviceDryRun = New-Object System.Collections.Generic.HashSet[string]
$trayLogged = $false
$resumePending = $false
$backoffLogged = $false
# A restart that does not clear its own reason (say the record cannot be written because
# the disk is full) must not repeat forever: after two in a row for the same kind of
# reason, that reason is ignored until it goes away by itself.
$lastKind = $null
$kindRepeats = 0
$suppressedLogged = $false
$lastTick = Get-Tick
Write-WatchLog "watcher started (pid $PID, every $IntervalSeconds s, version $script:QwenGpuWatchVersion)"
# Lets Start-QwenGpuWatch tell whether the watcher that holds the mutex runs current code.
try {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'data') | Out-Null
    [void](Write-QwenGpuProfileText -Path (Join-Path $Root 'data\gpu-watch.json') -KeepOnFailure -Text (ConvertTo-Json -Compress -InputObject ([ordered]@{
        pid = $PID; version = $script:QwenGpuWatchVersion; started = (Get-Date).ToString('o') })))
} catch { }

function Get-InventoryRetryKey($record) {
    return ('{0}|{1}|{2}' -f $record.pnp_signature, $record.nvidia_smi.exit_code, $record.nvidia_smi.error)
}

function Test-ChoiceComplete($record, [string]$recordKey) {
    # The running server really does what was chosen: CPU on the CPU, or every chosen GPU in use.
    if ($recordKey -eq 'cpu') { return ($record.mode -eq 'cpu') }
    if ($recordKey -like 'gpu:*' -and $record.mode -eq 'selected') {
        $used = @($record.gpus_used | Where-Object { $_ } | ForEach-Object { [string]$_.uuid })
        return (@($recordKey.Substring(4) -split ',' | Where-Object { $used -notcontains $_ }).Count -eq 0)
    }
    return $false
}

function Get-WorkingFromSignature([string]$Signature) {
    # The PnP ids of the NVIDIA devices that were working (nvlddmkm, code 0).
    return @(($Signature -split ';') | Where-Object { $f = @($_ -split '\|'); $f.Count -ge 3 -and $f[1] -ieq 'nvlddmkm' -and $f[2] -eq '0' } |
        ForEach-Object { ($_ -split '\|')[0] } | Sort-Object)
}

function Test-SameSetup($record) {
    # Would a fresh start give the running server exactly its current setup - the same GPU
    # variables, the same working NVIDIA devices, the same cards? Then only the record needs
    # updating: a new choice that changes nothing (auto already on the 4090 alone and the
    # 4090 is chosen), or a disabled or driverless card changing state (path B: the RTX on
    # Microsoft Basic Display before the driver install). Decided with a dry run.
    $scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('qwen-sel-' + [guid]::NewGuid().ToString('N'))
    try {
        $exe = [string](Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$record.server_pid)" -ErrorAction SilentlyContinue).ExecutablePath
        $saved = Resolve-QwenGpuProfile -Root $Root -OutRoot $scratch -OllamaExe $exe -Launcher ([string]$record.launcher) -ServerLog ([string]$record.server_log)
        Restore-QwenGpuEnvironment $saved
        $dry = Read-QwenGpuProfile -Root $scratch
        if (-not $dry -or $dry.inventory_ok -eq $false -or [string]$dry.pnp_signature -eq 'unknown') { return $false }
        foreach ($n in 'CUDA_VISIBLE_DEVICES', 'OLLAMA_LLM_LIBRARY', 'OLLAMA_VULKAN', 'GGML_VK_VISIBLE_DEVICES') {
            if ([string]$dry.env.$n -ne [string]$record.env.$n) { return $false }
        }
        if (((Get-WorkingFromSignature ([string]$dry.pnp_signature)) -join ';') -ne ((Get-WorkingFromSignature ([string]$record.pnp_signature)) -join ';')) { return $false }
        $usedNow = (@($record.gpus_used | Where-Object { $_ } | ForEach-Object { [string]$_.uuid } | Sort-Object) -join ',')
        $usedDry = (@($dry.gpus_used | Where-Object { $_ } | ForEach-Object { [string]$_.uuid } | Sort-Object) -join ',')
        if ($usedNow -ne $usedDry) { return $false }
        $text = [System.IO.File]::ReadAllText((Get-QwenGpuProfilePath -Root $scratch))
        # keep what the running server already established
        $text = [regex]::Replace($text, '"vram_class"\s*:\s*"[^"]*"', ('"vram_class":  "{0}"' -f [string]$record.vram_class))
        if (-not (Write-QwenGpuProfileText -Path (Get-QwenGpuProfilePath -Root $Root) -Text $text)) { return $false }
        return (Set-QwenGpuProfileServerPid -Root $Root -ServerPid ([int]$record.server_pid))
    } catch { return $false }
    finally { Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue }
}

try {
    while ($true) {
        Start-Sleep -Seconds $IntervalSeconds
        $gap = Get-TickSeconds $lastTick
        $lastTick = Get-Tick
        if ($gap -gt ($IntervalSeconds * 3 + 60)) { $resumePending = $true }
        try {
            $servePid = Get-ServePid
            if (-not $servePid) {
                $pendingKey = $null
                $resumePending = $false
                if (Test-Path -LiteralPath $markerPath) {
                    $age = ((Get-Date) - (Get-Item -LiteralPath $markerPath).LastWriteTime).TotalMinutes
                    Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue
                    if ($age -lt 15) {
                        Write-WatchLog 'a restart was interrupted between stop and start; starting the server'
                        & $restartScript -Reason 'finishing an interrupted restart' -StartIfStopped | Out-Null
                        Write-WatchLog ("start finished with exit code {0}" -f $LASTEXITCODE)
                        $lastTick = Get-Tick
                    }
                }
                continue
            }

            $record = Read-QwenGpuProfile -Root $Root
            # The nvidia-smi retry is once per failure episode: once a start describes the
            # cards again, a later plug-in that hits the same short failure is retried again.
            if ($record -and [string]$record.server_pid -eq [string]$servePid -and $record.inventory_ok -ne $false) { $inventoryRetried.Clear() }
            if ($record -and [string]$record.server_pid -eq [string]$servePid -and $record.vram_class -eq 'pending') {
                Update-PendingVram $record
            }
            if (-not $record -or $record.profile -notin @('gpu', 'vulkan') -or -not (Test-ExternalGpuInUse $record)) { $resumePending = $false }

            $signature = Get-QwenNvidiaPnpSignature
            $selectionKey = Get-QwenSelectionKey (Read-QwenComputeSelection -Root $Root)
            # Records written before the selection existed carry no key: they ran 'auto'.
            $recordKey = if ($record -and $record.selection_key) { [string]$record.selection_key } else { 'auto' }
            $reason = $null
            $kind = $null
            $urgent = $false
            $transition = $null
            if (-not $record) {
                $reason = 'no GPU profile recorded for the running server'; $kind = 'no-record'
            } elseif ([string]$record.server_pid -ne [string]$servePid) {
                $reason = "the running server (pid $servePid) was not started through the GPU profile step"; $kind = 'pid'
            } elseif ($null -ne $signature -and [string]$record.pnp_signature -ne 'unknown' -and [string]$record.pnp_signature -ne $signature) {
                $reason = 'NVIDIA devices changed (was: {0}; now: {1})' -f $record.pnp_signature, $signature
                $transition = '{0} -> {1}' -f $record.pnp_signature, $signature
                # Keyed on the change itself: a new plug/unplug is not a "failed" repeat.
                $kind = "devices:$transition"
                # Urgent when a device the server can be using went away or stopped working
                # (see Test-QwenServerDeviceLost: the cards it was given, or on 'internal' every
                # card that worked at its start - disabling the P620 while it is in use).
                $urgent = [bool](Test-QwenServerDeviceLost -Record $record -Signature $signature)
                # With the processing unit fixed to the CPU, or to GPUs that are all in use, a
                # card coming or going changes nothing: note it instead of restarting Ollama and
                # unloading its models. Never for a manual override.
                # (only when every card in use is known by its PnP id: otherwise the change
                # may be one of them, and it must go through the normal path)
                $fixed = (Test-ChoiceComplete $record $recordKey) -and
                    (@($record.gpus_used | Where-Object { $_ -and -not $_.pnp_id }).Count -eq 0)
                if ($fixed -and -not $urgent) {
                    if (Set-QwenGpuProfileValue -Root $Root -Name 'pnp_signature' -Value $signature) {
                        Write-WatchLog "device change noted, no restart: the processing unit is fixed ($recordKey)"
                    }
                    $pendingKey = $null
                    continue
                }
            } elseif ($recordKey -ne $selectionKey) {
                # The processing unit was changed (menu, web page or chat), or a timed choice
                # ran out: apply it once the server is idle, so the chat turn that asked for it
                # can finish first.
                $reason = "the processing unit choice changed ($recordKey -> $selectionKey)"; $kind = "selection:$selectionKey"
                # (dry run once per choice and server, not on every check while waiting for idle)
                # (keyed on the record too: once Test-SameSetup rewrote it, the same change
                # coming back later gets a fresh dry run instead of a restart)
                if ($selectionDryRun.Add("$selectionKey|$servePid|$($record.written_at)") -and (Test-SameSetup $record)) {
                    Write-WatchLog "choice changed ($recordKey -> $selectionKey) but the server setup stays the same: record updated, no restart"
                    $pendingKey = $null
                    continue
                }
            } elseif ($resumePending) {
                $reason = 'the machine resumed from sleep with an eGPU in use'; $kind = 'resume'
            } elseif ($record.inventory_ok -eq $false -and -not $inventoryRetried.Contains((Get-InventoryRetryKey $record))) {
                $reason = 'nvidia-smi could not describe the NVIDIA cards when the server started; trying once more'; $kind = 'inventory'
            } elseif ([string]$record.pnp_signature -eq 'unknown' -and $null -ne $signature -and -not $unknownRetried.Contains([string]$record.written_at)) {
                # Without a baseline no plug or unplug can be noticed; get one.
                $reason = 'the device list could not be read when the server started; restarting once to record it'; $kind = 'unknown'
            }
            if ($kind -in @('no-record', 'pid')) {
                # A server the Ollama tray app started and keeps alive cannot be replaced
                # from here: say so once instead of restarting it over and over.
                if ((Get-ServeParentName $servePid) -ieq 'ollama app.exe') {
                    if (-not $trayLogged) {
                        Write-WatchLog 'the running server belongs to the Ollama tray app; eGPU handling is off until the tray app is removed from Startup (see README)'
                        $trayLogged = $true
                    }
                    continue
                }
                if ($adoptTried.Add([string]$servePid) -and (Test-AdoptRunningServer $servePid)) { $pendingKey = $null; continue }
            }
            if (-not $reason) { $pendingKey = $null; $lastKind = $null; $kindRepeats = 0; $suppressedLogged = $false; continue }
            if (-not $urgent -and $kind -eq $lastKind -and $kindRepeats -ge 2) {
                if (-not $suppressedLogged) {
                    Write-WatchLog "giving up on '$kind' until it clears: two restarts in a row did not fix it. Last reason: $reason. Run Test-QwenGpu.ps1"
                    $suppressedLogged = $true
                }
                continue
            }

            $key = "$reason|$signature|$servePid"
            if ($pendingKey -ne $key) {
                $pendingKey = $key
                Write-WatchLog "change seen, confirming on the next check: $reason"
                continue
            }
            $pendingKey = $null
            # A device change that would not alter the setup (the RTX still on Microsoft Basic
            # Display during a driver install, the disabled P620's package replaced) is only
            # noted: no restart, no unloaded models, and nothing used up from the rate limit
            # that the restart onto the RTX may need a minute later. Once per change and server.
            if ($kind -like 'devices:*' -and -not $urgent -and $deviceDryRun.Add("$transition|$servePid|$($record.written_at)") -and (Test-SameSetup $record)) {
                Write-WatchLog "NVIDIA devices changed but the server setup stays the same: record updated, no restart ($transition)"
                continue
            }

            $recent = @($restarts | Where-Object { (Get-TickSeconds $_) -lt 1800 })
            $allowedUrgent = $urgent -and $transition -and (-not $urgentTransitions.ContainsKey($transition) -or (Get-TickSeconds $urgentTransitions[$transition]) -ge 1800)
            if ($recent.Count -ge 3 -and -not $allowedUrgent) {
                if (-not $backoffLogged) {
                    Write-WatchLog "NOT restarting yet: already restarted $($recent.Count) times in 30 min (rate limit). Reason: $reason. See logs\ollama-gpu-profile.log"
                    $backoffLogged = $true
                }
                continue
            }
            $backoffLogged = $false
            if ($urgent -and $transition) { $urgentTransitions[$transition] = Get-Tick }
            $wait = if ($urgent) { 0 } else { $IdleWaitSeconds }
            Write-WatchLog ("restarting Ollama (idle wait up to {0} s): {1}" -f $wait, $reason)
            & $restartScript -Reason $reason -MaxIdleWaitSeconds $wait | Out-Null
            $code = $LASTEXITCODE
            Write-WatchLog ("restart finished with exit code {0}" -f $code)
            if ($code -ne 3) {
                # A deliberate choice clears its own reason; it must not delay a later plug-in.
                if ($kind -notlike 'selection:*') { $restarts.Add((Get-Tick)) }
                $resumePending = $false
                if ($kind -eq 'inventory') { [void]$inventoryRetried.Add((Get-InventoryRetryKey $record)) }
                if ($kind -eq 'unknown') { [void]$unknownRetried.Add([string]$record.written_at) }
                if ($kind -eq $lastKind) { $kindRepeats++ } else { $lastKind = $kind; $kindRepeats = 1 }
            }
            $lastTick = Get-Tick
        } catch {
            Write-WatchLog ('check failed: ' + $_.Exception.Message)
        }
    }
} finally {
    try { $mutex.ReleaseMutex() } catch { }
    $mutex.Dispose()
}
