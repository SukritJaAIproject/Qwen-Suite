$ErrorActionPreference = 'Stop'

$GatewayRoot = 'E:\localllm\internet-gateway'
$startScript = Join-Path $GatewayRoot 'start-internet-watchdog.ps1'

Write-Host 'Starting Qwen Internet Gateway...' -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript
if ($LASTEXITCODE -ne 0) { throw 'Qwen Internet Gateway could not start' }

$setupUrl = 'http://127.0.0.1:8780/local/setup'
Start-Process $setupUrl

Write-Host ''
Write-Host 'The local setup page is open.' -ForegroundColor Green
Write-Host 'It contains the private API key, current Internet URL, and LINE pairing code.'
Write-Host 'You may close this window.' -ForegroundColor DarkGray
