$ErrorActionPreference = 'Continue'

# Tunnel watchdog.
#
# Until 2026-09-22 this script "checked" the tunnel by re-running the whole
# start-quick-tunnel.ps1 startup script every 120 s and treating its process exit
# code as a health verdict. That was wrong in both directions:
#   * powershell.exe -File returns 1 for ANY terminating error anywhere in a
#     ~200-line mutating script, so a healthy tunnel produced "Tunnel check exited
#     with code 1" (all 70 log lines were that one string, with the child's stderr
#     discarded, so the real reason was unrecoverable).
#   * if the tunnel was genuinely dead the startup script silently killed it, minted
#     a brand-new hostname and exited 0 - the one event worth recording logged nothing.
# It also re-ACLed the data directory and rewrote data\config.json and
# data\public-url.txt every two minutes, and a single DNS hiccup on the resolver it
# depends on was enough to kill a healthy tunnel and rotate the public hostname.
#
# Now: probe cloudflared's own metrics endpoint (local, no DNS, no internet), and run
# the repair script only when that probe says the tunnel is actually unhealthy.
#
# 2026-09-30 fixes (the tunnel was dead 09-29 07:15 -> 09-30 09:40 with no repair):
#   * Invoke-TunnelRepair used Start-Process -Wait. In Windows PowerShell 5.1 that
#     waits for the WHOLE process tree, including the cloudflared the repair has just
#     started, so after every successful repair this loop froze until that cloudflared
#     exited (09-24 20:49 -> 09-28 08:38, 09-29 06:26 -> 09-30 09:40: zero health checks
#     while the tunnel died during connected standby). Now: -PassThru + WaitForExit on
#     the repair process only, with a timeout.
#   * A cloudflared that is alive but not ready gets a grace period to reconnect by
#     itself (it keeps the same hostname when it does), instead of being killed on
#     the first failed probe, which rotated the URL and broke the LINE webhook.
#   * When api.trycloudflare.com does not resolve (laptop offline / between Wi-Fi
#     networks, or a resolver that blocks it) the repair is postponed instead of
#     killing the old tunnel and failing every 2 minutes.

$GatewayRoot = 'E:\localllm\internet-gateway'
$LogPath = 'E:\localllm\logs\internet-watchdog.log'
$RepairOut = 'E:\localllm\logs\internet-watchdog-repair.log'
$RepairErr = 'E:\localllm\logs\internet-watchdog-repair-error.log'
$TunnelTarget = '127.0.0.1:8780'
$CheckIntervalSeconds = 120
# consecutive failed probes of a still-running cloudflared before it is replaced (3 = ~6 min)
$ReconnectGraceChecks = 3
# start-quick-tunnel.ps1 normally finishes in < 2 min; never let one run block the loop longer
$RepairTimeoutSeconds = 900
# while unhealthy/offline, repeat the same log line only this often (in checks; 15 = ~30 min)
$RepeatLogEveryChecks = 15

$mutex = [System.Threading.Mutex]::new($false, 'Local\QwenInternetTunnelWatchdog')
$ownsMutex = $false

function Write-WatchdogLog {
    param([string]$Message)
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $LogPath -Encoding utf8 -Value "$timestamp $Message"
}

function Get-TunnelProcess {
    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*--url*$TunnelTarget*" } |
        Select-Object -First 1
}

function Test-TunnelHealth {
    # cloudflared chooses its metrics port at startup unless --metrics pins it, so
    # discover the port from the running process rather than hardcoding one.
    $proc = Get-TunnelProcess
    if (-not $proc) {
        return [pscustomobject]@{ Healthy = $false; Pid = $null; Reason = 'no cloudflared process for ' + $TunnelTarget }
    }
    $ports = @(
        Get-NetTCPConnection -State Listen -OwningProcess $proc.ProcessId -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq '127.0.0.1' } |
            Select-Object -ExpandProperty LocalPort -Unique
    )
    if ($ports.Count -eq 0) {
        return [pscustomobject]@{ Healthy = $false; Pid = $proc.ProcessId; Reason = "pid $($proc.ProcessId) has no local listener (metrics server not up)" }
    }
    foreach ($port in $ports) {
        try {
            $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$port/ready" -TimeoutSec 8 -ErrorAction Stop
            if ($ready.readyConnections -ge 1) {
                return [pscustomobject]@{ Healthy = $true; Pid = $proc.ProcessId; Reason = "ready on :$port, readyConnections=$($ready.readyConnections)" }
            }
            return [pscustomobject]@{ Healthy = $false; Pid = $proc.ProcessId; Reason = "ready on :$port reports readyConnections=$($ready.readyConnections)" }
        } catch {
            continue
        }
    }
    return [pscustomobject]@{ Healthy = $false; Pid = $proc.ProcessId; Reason = "pid $($proc.ProcessId) answered no /ready probe on ports $($ports -join ',')" }
}

function Test-TunnelDns {
    # cloudflared resolves api.trycloudflare.com through the system resolver, so ask the
    # same resolver. No answer means a new quick tunnel cannot be created right now.
    try {
        return (@([System.Net.Dns]::GetHostAddresses('api.trycloudflare.com')).Count -gt 0)
    } catch {
        return $false
    }
}

function Invoke-TunnelRepair {
    param([string]$Because)
    Write-WatchdogLog "repair: $Because - running start-quick-tunnel.ps1"
    try {
        # NOT -Wait: in Windows PowerShell 5.1 it also waits for every descendant,
        # i.e. for the cloudflared this repair starts, which froze the watchdog for days.
        $process = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $GatewayRoot 'start-quick-tunnel.ps1')) `
            -WindowStyle Hidden -RedirectStandardOutput $RepairOut -RedirectStandardError $RepairErr -PassThru
        # Touch Handle now: without it PS 5.1 reports an empty ExitCode after -PassThru.
        $null = $process.Handle
        if (-not $process.WaitForExit($RepairTimeoutSeconds * 1000)) {
            # Only the repair script is stopped; a cloudflared it already started is left
            # running and judged by the next probe.
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            Write-WatchdogLog "repair: start-quick-tunnel.ps1 still running after $RepairTimeoutSeconds s - stopped it"
            return $false
        }
        if ($process.ExitCode -eq 0) {
            $url = ''
            $urlFile = Join-Path $GatewayRoot 'data\public-url.txt'
            try { $url = [System.IO.File]::ReadAllText($urlFile).Trim() } catch {}
            Write-WatchdogLog "repair: start-quick-tunnel.ps1 finished (exit 0) url=$url"
            return $true
        } else {
            # The first non-empty stderr line is the thrown message (it includes the last
            # cloudflared error line); the rest is PowerShell's position/category noise.
            $message = @(Get-Content -LiteralPath $RepairErr -ErrorAction SilentlyContinue |
                Where-Object { $_.Trim() } | Select-Object -First 1) -join ''
            if (-not $message) { $message = '(no stderr captured)' }
            Write-WatchdogLog "repair: start-quick-tunnel.ps1 FAILED exit $($process.ExitCode) :: $($message.Trim())"
        }
    } catch {
        Write-WatchdogLog "repair: could not launch start-quick-tunnel.ps1 :: $($_.Exception.Message)"
    }
    return $false
}

try {
    try {
        $ownsMutex = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) { return }

    Write-WatchdogLog 'watchdog started (metrics-probe mode, non-blocking repair)'
    $healthyStreak = 0
    $unhealthyStreak = 0
    $offlineStreak = 0
    $repairFailures = 0
    $nextRepairAt = [datetime]::MinValue
    $lastPid = $null

    while ($true) {
        Start-Sleep -Seconds $CheckIntervalSeconds
        $status = Test-TunnelHealth

        if ($status.Healthy) {
            if ($unhealthyStreak -gt 0) {
                Write-WatchdogLog "recovered after $unhealthyStreak failed check(s): $($status.Reason)"
            }
            $unhealthyStreak = 0
            $offlineStreak = 0
            $repairFailures = 0
            $nextRepairAt = [datetime]::MinValue
            # A different pid means cloudflared was restarted by something else and the
            # public hostname has changed, so data\public-url.txt is stale: refresh it.
            if ($lastPid -and $status.Pid -ne $lastPid) {
                Write-WatchdogLog "cloudflared pid changed $lastPid -> $($status.Pid); hostname may have rotated"
                $null = Invoke-TunnelRepair -Because 'pid change, refreshing published URL'
            }
            $lastPid = $status.Pid
            $healthyStreak++
            # one line on the first healthy check, then roughly hourly, to keep the log useful
            if ($healthyStreak -eq 1 -or ($healthyStreak % 30) -eq 0) {
                Write-WatchdogLog "healthy: $($status.Reason) [$healthyStreak consecutive checks]"
            }
            continue
        }

        $healthyStreak = 0
        $unhealthyStreak++
        if ($unhealthyStreak -eq 1 -or ($unhealthyStreak % $RepeatLogEveryChecks) -eq 0) {
            Write-WatchdogLog "unhealthy: $($status.Reason) [$unhealthyStreak consecutive checks]"
        }

        # cloudflared reconnects by itself after a network blip and keeps its hostname;
        # replacing it would mint a new URL, so give it a few checks first.
        if ($status.Pid -and $unhealthyStreak -lt $ReconnectGraceChecks) {
            continue
        }

        if (-not (Test-TunnelDns)) {
            $offlineStreak++
            if ($offlineStreak -eq 1 -or ($offlineStreak % $RepeatLogEveryChecks) -eq 0) {
                Write-WatchdogLog 'offline: api.trycloudflare.com does not resolve (no network or DNS blocks it) - repair postponed'
            }
            continue
        }
        if ($offlineStreak -gt 0) {
            Write-WatchdogLog "network back after $offlineStreak offline check(s)"
            $offlineStreak = 0
        }

        # Back off after repeated failures (Cloudflare rate-limits quick tunnel creation):
        # retry after 2, 4, 8, 16, then every 30 minutes.
        if ((Get-Date) -lt $nextRepairAt) {
            continue
        }

        $lastPid = $null
        if (Invoke-TunnelRepair -Because $status.Reason) {
            $repairFailures = 0
            $nextRepairAt = [datetime]::MinValue
        } else {
            $repairFailures++
            $delayMinutes = [Math]::Min(30, 2 * [Math]::Pow(2, $repairFailures - 1))
            $nextRepairAt = (Get-Date).AddMinutes($delayMinutes).AddSeconds(-30)
            if ($repairFailures -ge 2) {
                Write-WatchdogLog "repair: $repairFailures failures in a row - next attempt in about $delayMinutes min"
            }
        }
    }
} finally {
    if ($ownsMutex) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
