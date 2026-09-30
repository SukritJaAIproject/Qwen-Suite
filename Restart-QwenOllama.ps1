# Restart only the Ollama server, through the normal launcher, so a new GPU profile
# (eGPU plugged in or pulled out) takes effect. Added 2026-09-29.
#
# Why a separate script: Ollama picks its GPUs once, when the server starts, and both
# launchers exit straight away when port 11434 is already listening. stop-all.ps1 and
# Stop-Qwen-Suite.ps1 stop everything (WebUI, tools, router), which is far more than a GPU
# change needs.
#
# What it does:
#  1. Only one restart at a time (mutex Local\QwenSuite-OllamaRestart); a second caller
#     logs "already in progress" and returns.
#  2. With -MaxIdleWaitSeconds (the watcher uses 600): waits for 60 s of real quiet - no
#     runner slot processing a request (llama-server /slots), no runner burning CPU or
#     appearing/disappearing, no request finishing, no model download in progress. If that
#     never comes it gives up with exit code 3 and restarts nothing; the watcher tries
#     again later. Without it (the default, e.g. when run by hand or from chat) it
#     restarts at once - and if the watcher's restart is already waiting for idle, it tells
#     that one to go ahead now instead.
#  3. Leaves data\ollama-restart.json while the server is down, so if this script is
#     killed between stop and start the watcher finishes the job. stop-all.ps1 and
#     Stop-Qwen-Suite.ps1 delete it.
#  4. Stops the ollama.exe that owns 11434 and the llama-server processes it started - by
#     process id, not by name, so an "ollama pull" or a test server on another port is
#     left alone. Killing ollama.exe alone would orphan llama-server with the model in RAM.
#  5. Keeps the previous logs as logs\*.prev.log (the launchers truncate theirs).
#  6. Starts the server with start-ollama-server.ps1 (or Start-Ollama.ps1 on an installed
#     machine), which runs Resolve-OllamaGpu.ps1 inside the shared start mutex.
#
# Safe to run at any time, from anywhere (it is reachable from the chat channels): with no
# server running it does nothing unless -StartIfStopped is given.
# Exit codes: 0 done / nothing to do, 1 server did not come back, 3 busy - not restarted.

param(
    [string]$Reason = 'manual',
    [int]$MaxIdleWaitSeconds = 0,
    [switch]$StartIfStopped
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$helper = Join-Path $Root 'Resolve-OllamaGpu.ps1'
if (Test-Path -LiteralPath $helper) { . $helper }

function Write-RestartLog([string]$Message) {
    if (Get-Command Write-QwenGpuLog -ErrorAction SilentlyContinue) {
        Write-QwenGpuLog -Root $Root -Message ("restart: " + $Message)
    }
    Write-Host $Message
}

function Get-OllamaServeProcess {
    $owner = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -First 1
    if (-not $owner) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
    if ($proc -and $proc.Name -ieq 'ollama.exe') { return $proc }
    return $null
}

function Get-RunnerProcesses([int]$ServePid) {
    # Win32_Process keeps ParentProcessId after the parent has exited, so this also finds
    # runners of a server that was just killed.
    return @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $ServePid })
}

function Get-RunnerCpu([int]$ServePid) {
    $sample = @{}
    foreach ($runner in (Get-RunnerProcesses $ServePid)) {
        $p = Get-Process -Id $runner.ProcessId -ErrorAction SilentlyContinue
        if ($p) { $sample[[int]$runner.ProcessId] = $p.TotalProcessorTime.TotalSeconds }
    }
    return $sample
}

function Get-LastRequestTime {
    # Ollama writes one [GIN] line per request when it finishes, to the launcher's stdout log.
    $latest = $null
    foreach ($name in 'ollama-server.out', 'ollama.log') {
        $path = Join-Path $LogDir $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $line = Get-Content -LiteralPath $path -Tail 40 -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\[GIN\] (\d{4}/\d{2}/\d{2} - \d{2}:\d{2}:\d{2}) \|' -and $_ -notmatch '"/api/(ps|version|tags)"' } |
            Select-Object -Last 1
        if ($line -and $line -match '^\[GIN\] (\d{4}/\d{2}/\d{2} - \d{2}:\d{2}:\d{2})') {
            try {
                $t = [datetime]::ParseExact($Matches[1], 'yyyy/MM/dd - HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
                if (-not $latest -or $t -gt $latest) { $latest = $t }
            } catch { }
        }
    }
    return $latest
}

function Test-PullInProgress {
    # A download holds its "-partial" file open for its whole run (and NTFS does not update
    # the listed timestamp while it does), so test whether any of them is open; leftovers
    # of an old, aborted pull are not.
    $models = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
    if (-not $models) { return $false }
    $blobs = Join-Path $models 'blobs'
    if (-not (Test-Path -LiteralPath $blobs)) { return $false }
    foreach ($f in @(Get-ChildItem -LiteralPath $blobs -Filter '*-partial*' -File -ErrorAction SilentlyContinue)) {
        try { [System.IO.File]::Open($f.FullName, 'Open', 'Read', 'None').Dispose() } catch { return $true }
    }
    return $false
}

function Test-RunnerProcessing([int]$ServePid) {
    # llama-server reports per slot whether it is working on a request - the one signal
    # that also sees a long prompt being read or a GPU generation that uses little CPU.
    foreach ($runner in (Get-RunnerProcesses $ServePid)) {
        if ([string]$runner.CommandLine -match '--port\s+(\d+)') {
            try {
                $slots = Invoke-RestMethod ("http://127.0.0.1:{0}/slots" -f $Matches[1]) -TimeoutSec 3
                if (@($slots | Where-Object { $_.is_processing -eq $true }).Count -gt 0) { return $true }
            } catch { }
        }
    }
    return $false
}

function Get-RestartNowFlagPath { return (Join-Path (Join-Path $Root 'data') 'ollama-restart-now') }

# Intervals on the tick count (ms since boot), not the wall clock: a clock correction
# during a wait must not stretch it to hours.
function Get-Tick { return [long][Environment]::TickCount }
function Get-TickSeconds([long]$From) {
    $d = (Get-Tick) - $From
    if ($d -lt 0) { $d += 4294967296 }
    return $d / 1000.0
}

function Test-UsedGpuGone([int]$ServePid) {
    # A device the server can be using has disappeared or stopped working: the server is
    # broken already, so a restart that is politely waiting for idle should go ahead. The
    # rule is shared with the watcher (Test-QwenServerDeviceLost in Resolve-OllamaGpu.ps1).
    # Only a record written for THIS server says which devices it uses; a stale one (a server
    # started by hand) must not cut off its answer.
    try {
        $record = Read-QwenGpuProfile -Root $Root
        if (-not $record -or [string]$record.server_pid -ne [string]$ServePid) { return $false }
        $now = Get-QwenNvidiaPnpSignature
        if ($null -eq $now) { return $false }
        return [bool](Test-QwenServerDeviceLost -Record $record -Signature $now)
    } catch { return $false }
}

function Wait-OllamaIdle([int]$ServePid, [int]$MaxSeconds) {
    # True after 60 s in which nothing happened (or when waiting no longer makes sense);
    # false if that never came before the deadline.
    $start = Get-Tick
    $quietSince = $null
    while ((Get-TickSeconds $start) -lt $MaxSeconds) {
        $before = Get-RunnerCpu $ServePid
        Start-Sleep -Seconds 5
        $after = Get-RunnerCpu $ServePid
        $busy = $false
        # A runner that appeared or went away is a model being loaded or swapped.
        if (@($before.Keys | Where-Object { -not $after.ContainsKey($_) }).Count -gt 0) { $busy = $true }
        if (@($after.Keys | Where-Object { -not $before.ContainsKey($_) }).Count -gt 0) { $busy = $true }
        $cpu = 0.0
        foreach ($id in $after.Keys) { if ($before.ContainsKey($id)) { $cpu += $after[$id] - $before[$id] } }
        # Generation on a GPU still keeps one core busy feeding it.
        if ($cpu -ge 1.5) { $busy = $true }
        if (-not $busy -and (Test-RunnerProcessing $ServePid)) { $busy = $true }
        # Someone ran Restart by hand (or from chat) while this was waiting: go ahead now.
        $flag = Get-RestartNowFlagPath
        if (Test-Path -LiteralPath $flag) {
            Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
            Write-RestartLog 'restart requested now by a manual call; not waiting for idle any longer'
            return $true
        }
        $last = Get-LastRequestTime
        if ($last -and $last -gt (Get-Date).AddSeconds(-20)) { $busy = $true }
        if (Test-PullInProgress) { $busy = $true }
        if (Test-UsedGpuGone $ServePid) {
            Write-RestartLog 'a GPU the server uses has disappeared; not waiting for idle any longer'
            return $true
        }
        if ($busy) { $quietSince = $null }
        elseif ($null -eq $quietSince) { $quietSince = Get-Tick }
        elseif ((Get-TickSeconds $quietSince) -ge 60) { return $true }
    }
    return $false
}

$restartMutex = New-Object System.Threading.Mutex($false, 'Local\QwenSuite-OllamaRestart')
$ownsRestart = $false
try { $ownsRestart = $restartMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $ownsRestart = $true }
if (-not $ownsRestart) {
    $restartMutex.Dispose()
    if ($MaxIdleWaitSeconds -gt 0) {
        Write-RestartLog "a restart is already in progress; not starting another ($Reason)"
        exit 0
    }
    # A restart "now" while the watcher's restart is waiting for idle: tell it to go ahead
    # instead of doing nothing, and wait until it has taken the request.
    $flag = Get-RestartNowFlagPath
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $flag) | Out-Null
        [System.IO.File]::WriteAllText($flag, ((Get-Date).ToString('o') + ' ' + $Reason))
    } catch { }
    Write-RestartLog "a restart is already waiting for idle; asked it to restart now ($Reason)"
    $t0 = Get-Tick
    while ((Get-TickSeconds $t0) -lt 60 -and (Test-Path -LiteralPath $flag)) { Start-Sleep -Seconds 2 }
    if (Test-Path -LiteralPath $flag) {
        Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
        Write-RestartLog 'the restart in progress did not take the request within 60 s'
        exit 1
    }
    Write-RestartLog 'the restart in progress is going ahead now'
    exit 0
}
# A leftover request from a caller that gave up must not cut a later idle wait short.
Remove-Item -LiteralPath (Get-RestartNowFlagPath) -Force -ErrorAction SilentlyContinue

$exitCode = 0
try {
    $serve = Get-OllamaServeProcess
    if (-not $serve -and -not $StartIfStopped) {
        Write-RestartLog "nothing to restart ($Reason): no Ollama server is listening on 11434"
        exit 0
    }
    if ($serve -and $MaxIdleWaitSeconds -gt 0) {
        if (-not (Wait-OllamaIdle -ServePid $serve.ProcessId -MaxSeconds $MaxIdleWaitSeconds)) {
            Write-RestartLog "still busy after $MaxIdleWaitSeconds s; not restarting now ($Reason)"
            exit 3
        }
        $now = Get-OllamaServeProcess
        if (-not $now) {
            if (-not $StartIfStopped) {
                Write-RestartLog "the server stopped while waiting; leaving it stopped ($Reason)"
                exit 0
            }
        } elseif ($now.ProcessId -ne $serve.ProcessId) {
            Write-RestartLog "someone else restarted the server while waiting (pid $($serve.ProcessId) -> $($now.ProcessId)); nothing to do"
            exit 0
        }
        $serve = $now
    }

    $markerPath = $null
    if (Get-Command Get-QwenRestartMarkerPath -ErrorAction SilentlyContinue) { $markerPath = Get-QwenRestartMarkerPath -Root $Root }
    if ($serve) {
        if ($markerPath) {
            try {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $markerPath) | Out-Null
                $marker = @{ at = (Get-Date).ToString('o'); reason = $Reason; pid = $PID } | ConvertTo-Json -Compress
                [System.IO.File]::WriteAllText($markerPath, $marker, (New-Object System.Text.UTF8Encoding($false)))
            } catch { }
        }
        foreach ($name in 'ollama-server.log', 'ollama-server.out', 'ollama-error.log', 'ollama.log') {
            $live = Join-Path $LogDir $name
            if (Test-Path -LiteralPath $live) {
                Copy-Item -LiteralPath $live -Destination (Join-Path $LogDir ($name -replace '\.(log|out)$', '.prev.$1')) -Force -ErrorAction SilentlyContinue
            }
        }
        $runners = Get-RunnerProcesses $serve.ProcessId
        Write-RestartLog ("stopping ollama pid {0} and {1} runner(s) ({2})" -f $serve.ProcessId, $runners.Count, $Reason)
        Stop-Process -Id $serve.ProcessId -Force -ErrorAction SilentlyContinue
        foreach ($runner in $runners) { Stop-Process -Id $runner.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
        # A runner started between the listing and the kill.
        foreach ($runner in (Get-RunnerProcesses $serve.ProcessId)) { Stop-Process -Id $runner.ProcessId -Force -ErrorAction SilentlyContinue }
        $t0 = Get-Tick
        while ((Get-TickSeconds $t0) -lt 30 -and (Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 500
        }
    }

    $launcher = Join-Path $Root 'start-ollama-server.ps1'
    if (-not (Test-Path -LiteralPath $launcher)) { $launcher = Join-Path $Root 'Start-Ollama.ps1' }
    if (-not (Test-Path -LiteralPath $launcher)) {
        Write-RestartLog "no launcher found next to $PSCommandPath"
        exit 1
    }
    $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    # Not Start-Process -Wait: in Windows PowerShell 5.1 that waits for the whole process
    # tree, and the server the launcher starts never exits.
    $started = Start-Process -FilePath $shell -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"{0}"' -f $launcher)
    )
    # The launcher is its own process now and survives this script; the stop-to-start gap
    # the marker covers is closed.
    if ($markerPath) { Remove-Item -LiteralPath $markerPath -Force -ErrorAction SilentlyContinue }
    [void]$started.WaitForExit(200000)
    $launcherExit = if ($started.HasExited) { $started.ExitCode } else { 'still running' }
    $ok = $false
    $t0 = Get-Tick
    while ((Get-TickSeconds $t0) -lt 90) {
        try { Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 3 | Out-Null; $ok = $true; break }
        catch { Start-Sleep -Seconds 2 }
    }
    $profileText = ''
    if (Get-Command Read-QwenGpuProfile -ErrorAction SilentlyContinue) {
        $record = Read-QwenGpuProfile -Root $Root
        if ($record) { $profileText = " profile=$($record.profile) vram=$($record.vram_expected)" }
    }
    if ($ok) {
        Write-RestartLog ("server is back via {0} (launcher exit {1}){2}" -f (Split-Path -Leaf $launcher), $launcherExit, $profileText)
    } else {
        Write-RestartLog ("server did NOT come back via {0} (launcher exit {1})" -f (Split-Path -Leaf $launcher), $launcherExit)
        $exitCode = 1
    }
} finally {
    try { $restartMutex.ReleaseMutex() } catch { }
    $restartMutex.Dispose()
}
exit $exitCode
