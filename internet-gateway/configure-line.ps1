$ErrorActionPreference = 'Stop'

$GatewayRoot = 'E:\localllm\internet-gateway'
$Python = 'E:\localllm\webui-venv\Scripts\python.exe'

Write-Host 'Qwen Internet - LINE setup' -ForegroundColor Cyan
Write-Host 'Values are passed only to a local child process and stored with Windows DPAPI.' -ForegroundColor DarkGray
$secretSecure = Read-Host 'LINE Channel secret' -AsSecureString
$tokenSecure = Read-Host 'LINE Channel access token' -AsSecureString

$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
$tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($tokenSecure)
try {
    $env:QWEN_LINE_CHANNEL_SECRET = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    $env:QWEN_LINE_CHANNEL_ACCESS_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    & $Python (Join-Path $GatewayRoot 'configure.py') line-from-env
    if ($LASTEXITCODE -ne 0) { throw 'LINE credentials were not saved' }
} finally {
    Remove-Item Env:QWEN_LINE_CHANNEL_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:QWEN_LINE_CHANNEL_ACCESS_TOKEN -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
}

$publicUrlPath = Join-Path $GatewayRoot 'data\public-url.txt'
if (Test-Path -LiteralPath $publicUrlPath) {
    $publicUrl = [System.IO.File]::ReadAllText($publicUrlPath).Trim()
    if ($publicUrl -match '^https://') {
        Write-Host 'Registering and testing the current webhook URL with LINE...' -ForegroundColor Yellow
        & $Python (Join-Path $GatewayRoot 'configure.py') register-line $publicUrl
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Credentials are saved, but webhook registration failed.' -ForegroundColor DarkYellow
            Write-Host 'The tunnel may be down; it will be registered automatically when the tunnel next starts.' -ForegroundColor DarkYellow
        } else {
            # start-quick-tunnel.ps1 re-registers only when this differs from the live URL
            [System.IO.File]::WriteAllText((Join-Path $GatewayRoot 'data\line-webhook-url.txt'), $publicUrl + "`n")
        }
    }
}

Write-Host 'LINE configuration complete.' -ForegroundColor Green
Start-Process 'http://127.0.0.1:8780/local/setup'
