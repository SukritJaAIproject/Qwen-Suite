$ErrorActionPreference = 'Stop'

$GatewayRoot = 'E:\localllm\internet-gateway'
$Python = 'E:\localllm\webui-venv\Scripts\python.exe'
$LogDir = 'E:\localllm\logs'
$PublicUrlFile = Join-Path $GatewayRoot 'data\public-url.txt'
# Base URL that LINE last accepted as webhook (written only after a successful
# register-line), so a failed registration is retried on the next start.
$LineWebhookFile = Join-Path $GatewayRoot 'data\line-webhook-url.txt'
$previousPublicUrl = ''
if (Test-Path -LiteralPath $PublicUrlFile) {
    $previousPublicUrl = [System.IO.File]::ReadAllText($PublicUrlFile).Trim()
}

$startMutex = [System.Threading.Mutex]::new($false, 'Local\QwenInternetTunnelStart')
$ownsMutex = $false
$startedTunnelProcess = $null
$completed = $false
try {
    try {
        $ownsMutex = $startMutex.WaitOne(180000)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) { throw 'Timed out waiting for another Internet Tunnel startup' }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $GatewayRoot 'start-internet-gateway.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Internet Gateway could not start' }

function Resolve-Cloudflared {
    $command = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $packageRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $candidate = Get-ChildItem -LiteralPath $packageRoot -Filter 'cloudflared.exe' -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($candidate) { return $candidate.FullName }
    throw 'cloudflared is not installed'
}

function Get-TunnelUrl {
    param([string[]]$Paths)
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        # Start-Process keeps redirected log files open on Windows. Read with
        # FileShare.ReadWrite so the URL can be discovered while cloudflared runs.
        $stream = [System.IO.File]::Open(
            $path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::ReadWrite
        )
        try {
            $reader = [System.IO.StreamReader]::new($stream)
            try { $content = $reader.ReadToEnd() } finally { $reader.Dispose() }
        } finally {
            $stream.Dispose()
        }
        $matches = [regex]::Matches($content, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($matches.Count -gt 0) { return $matches[$matches.Count - 1].Value }
    }
    return $null
}

function Get-TunnelFailure {
    # Last cloudflared log line that explains a failure, e.g.
    # "failed to request quick Tunnel: ... lookup api.trycloudflare.com: no such host"
    param([string]$Path)
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $reader = [System.IO.StreamReader]::new($stream)
            try { $lines = $reader.ReadToEnd() -split "`r?`n" } finally { $reader.Dispose() }
        } finally { $stream.Dispose() }
        $line = $lines | Where-Object { $_ -match '(?i)\b(ERR|failed|error)\b' } | Select-Object -Last 1
        if ($line) { return ($line.Trim() -replace '^\S+Z\s+', '') }
    } catch {}
    return ''
}

function Send-TunnelUrlNotice {
    # Telegram to the owner + toast, through local-tools\notifier.py. Started detached and
    # without redirection so it never delays the tunnel or inherits this script's handles.
    param([string]$Url, [string]$LineState)
    $helper = Join-Path $GatewayRoot 'notify_tunnel_url.py'
    if (-not (Test-Path -LiteralPath $helper)) { return }
    try {
        Start-Process -FilePath $Python `
            -ArgumentList @($helper, '--url', $Url, '--line', $LineState) `
            -WorkingDirectory $GatewayRoot `
            -WindowStyle Hidden | Out-Null
    } catch {
        Write-Host "Could not start the URL-change notification: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
}

function Test-PublicGateway {
    param([string]$Url)
    if (-not $Url) { return $false }
    try {
        $health = Invoke-RestMethod -Uri ($Url + '/health') -TimeoutSec 5
        return $health.status -eq 'ok'
    } catch {}

    # Some corporate DNS resolvers temporarily cache NXDOMAIN for a brand-new
    # trycloudflare hostname. Resolve through 1.1.1.1 and make a TLS/SNI-safe
    # curl request without changing the machine DNS configuration.
    try {
        $uri = [System.Uri]$Url
        foreach ($dnsServer in @('1.1.1.1', '8.8.8.8')) {
            try {
                $addresses = Resolve-DnsName -Name $uri.DnsSafeHost -Type A -Server $dnsServer -ErrorAction Stop |
                    Where-Object { $_.IPAddress } |
                    Select-Object -ExpandProperty IPAddress -Unique
            } catch {
                continue
            }
            foreach ($address in $addresses) {
                $resolve = '{0}:443:{1}' -f $uri.DnsSafeHost, $address
                # Ignore a corporate HTTP proxy here: --resolve must connect to
                # the chosen Cloudflare IP directly while retaining TLS/SNI.
                $body = & curl.exe --silent --fail --noproxy '*' --connect-timeout 5 --max-time 12 `
                    --resolve $resolve ($Url + '/health') 2>$null
                if ($LASTEXITCODE -eq 0 -and ($body -join '') -match '"status"\s*:\s*"ok"') {
                    return $true
                }
            }
        }
    } catch {}
    return $false
}

$cloudflared = Resolve-Cloudflared
$stdout = Join-Path $LogDir 'internet-tunnel.log'
$stderr = Join-Path $LogDir 'internet-tunnel-error.log'
$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq 'cloudflared.exe' -and
        $_.CommandLine -like '*tunnel*--url*127.0.0.1:8780*'
    } | Select-Object -First 1

$process = $null
$publicUrl = $null
if ($existing) {
    $process = Get-Process -Id $existing.ProcessId
    $publicUrl = Get-TunnelUrl -Paths @($stdout, $stderr)
    if (-not $publicUrl -and (Test-Path -LiteralPath $PublicUrlFile)) {
        $recordedUrl = [System.IO.File]::ReadAllText($PublicUrlFile).Trim()
        if ($recordedUrl -match '^https://[a-z0-9-]+\.trycloudflare\.com$') {
            $publicUrl = $recordedUrl
        }
    }

    # Account-less Quick Tunnels can expire while cloudflared itself keeps
    # retrying. Never reuse a process merely because it is still alive.
    if (-not (Test-PublicGateway -Url $publicUrl)) {
        Write-Host 'The previous Quick Tunnel expired; creating a fresh URL...' -ForegroundColor DarkYellow
        Stop-Process -Id $existing.ProcessId -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $existing.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        $existing = $null
        $process = $null
        $publicUrl = $null
    }
}

if (-not $existing) {
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $cloudflared `
        -ArgumentList @('tunnel', '--url', 'http://127.0.0.1:8780', '--no-autoupdate') `
        -WorkingDirectory $GatewayRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    $startedTunnelProcess = $process
}

for ($attempt = 0; $attempt -lt 90; $attempt++) {
    if ($publicUrl) { break }
    Start-Sleep -Seconds 1
    if ($process.HasExited) {
        $reason = Get-TunnelFailure -Path $stderr
        if ($reason) { throw "Cloudflare Tunnel exited early: $reason (see $stderr)" }
        throw "Cloudflare Tunnel exited early. See $stderr"
    }
    $publicUrl = Get-TunnelUrl -Paths @($stdout, $stderr)
    if ($publicUrl) { break }
}
if (-not $publicUrl) { throw "Cloudflare Tunnel URL was not found. See $stderr" }

# cloudflared prints the hostname a moment before its DNS record exists. Probing it at
# once gets an NXDOMAIN that resolvers (the home router, 1.1.1.1, 8.8.8.8) then cache,
# so phones on this LAN could not open the new URL for minutes (seen 2026-09-30 22:31).
# For a new tunnel, wait for the first registered edge connection plus a few seconds.
if ($startedTunnelProcess) {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($process.HasExited) { break }
        $registered = $false
        foreach ($path in @($stderr, $stdout)) {
            try {
                $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                try {
                    $reader = [System.IO.StreamReader]::new($stream)
                    try { if ($reader.ReadToEnd() -match 'Registered tunnel connection') { $registered = $true } } finally { $reader.Dispose() }
                } finally { $stream.Dispose() }
            } catch {}
        }
        if ($registered) { break }
        Start-Sleep -Seconds 1
    }
    Start-Sleep -Seconds 8
}

$publicReady = Test-PublicGateway -Url $publicUrl
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    if ($publicReady) { break }
    if ($process.HasExited) { throw "Cloudflare Tunnel exited before health verification. See $stderr" }
    $publicReady = Test-PublicGateway -Url $publicUrl
    Start-Sleep -Seconds 1
}
if (-not $publicReady) {
    throw "Cloudflare Tunnel URL failed its public health check: $publicUrl"
}

& $Python (Join-Path $GatewayRoot 'configure.py') set-public-url $publicUrl | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not record the public tunnel URL' }

Write-Host "Qwen Internet URL: $publicUrl" -ForegroundColor Green

$status = & $Python (Join-Path $GatewayRoot 'configure.py') status | ConvertFrom-Json
$lineState = 'not_configured'
if ($status.line_configured) {
    $registeredUrl = ''
    if (Test-Path -LiteralPath $LineWebhookFile) {
        $registeredUrl = [System.IO.File]::ReadAllText($LineWebhookFile).Trim()
    }
    if ($registeredUrl -eq $publicUrl) {
        $lineState = 'unchanged'
    } else {
        & $Python (Join-Path $GatewayRoot 'configure.py') register-line $publicUrl
        if ($LASTEXITCODE -eq 0) {
            [System.IO.File]::WriteAllText($LineWebhookFile, $publicUrl + "`n")
            $lineState = 'registered'
        } else {
            $lineState = 'failed'
            Write-Host 'Tunnel is online, but LINE webhook registration failed. Check credentials/logs.' -ForegroundColor DarkYellow
        }
    }
}

# A quick tunnel gets a new random hostname on every restart; tell the owner.
if ($previousPublicUrl -ne $publicUrl) {
    Send-TunnelUrlNotice -Url $publicUrl -LineState $lineState
}

    $completed = $true
} finally {
    if (-not $completed -and $startedTunnelProcess -and -not $startedTunnelProcess.HasExited) {
        Stop-Process -Id $startedTunnelProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($ownsMutex) { [void]$startMutex.ReleaseMutex() }
    $startMutex.Dispose()
}
