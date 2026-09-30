$ErrorActionPreference = 'Stop'

$GatewayRoot = 'E:\localllm\internet-gateway'
$watchScript = Join-Path $GatewayRoot 'watch-internet-gateway.ps1'

# Bring the service online synchronously so callers know whether startup worked.
# A failure here must NOT prevent the watchdog from starting: at logon the Wi-Fi is
# often not connected yet (2026-09-30 21:28: "lookup api.trycloudflare.com: no such
# host"), and the old version threw before launching the watchdog, so nothing retried
# when the network came back and the tunnel stayed down until someone noticed.
$tunnelError = $null
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $GatewayRoot 'start-quick-tunnel.ps1')
    if ($LASTEXITCODE -ne 0) { $tunnelError = "start-quick-tunnel.ps1 exited with code $LASTEXITCODE" }
} catch {
    $tunnelError = $_.Exception.Message
}

$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq 'powershell.exe' -and
        $_.CommandLine -like '*E:\localllm\internet-gateway\watch-internet-gateway.ps1*'
    } |
    Select-Object -First 1

if (-not $existing) {
    # No output redirection: the watchdog must not inherit this script's handles
    # (a caller that pipes our output would otherwise wait for the watchdog forever).
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', $watchScript
        ) `
        -WorkingDirectory $GatewayRoot `
        -WindowStyle Hidden | Out-Null
    Write-Host 'Internet tunnel watchdog started.' -ForegroundColor Green
} else {
    Write-Host 'Internet tunnel watchdog already running.' -ForegroundColor Green
}

if ($tunnelError) {
    Write-Host 'The watchdog will retry the tunnel every few minutes once the network is back.' -ForegroundColor DarkYellow
    throw "Internet tunnel startup failed: $tunnelError"
}
