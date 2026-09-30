$ErrorActionPreference = 'Stop'

$GatewayRoot = 'E:\localllm\internet-gateway'
$Python = 'E:\localllm\webui-venv\Scripts\python.exe'
$LogDir = 'E:\localllm\logs'
$DataDir = Join-Path $GatewayRoot 'data'
$Port = 8780

function Set-PrivateItemAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$IsContainer,
        [Parameter(Mandatory = $true)][string]$Account
    )

    if ($IsContainer) {
        $grant = "${Account}:(OI)(CI)F"
    } else {
        $grant = "${Account}:F"
    }

    & icacls.exe $Path /inheritance:r /grant:r $grant | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not protect ACL on $Path" }

    # /grant:r replaces this user's rule, but does not remove unrelated
    # explicit ACEs. Enumerate and remove every other allow/deny principal.
    $otherPrincipals = Get-Acl -LiteralPath $Path -ErrorAction Stop |
        Select-Object -ExpandProperty Access |
        Where-Object { $_.IdentityReference.Value -ine $Account } |
        ForEach-Object { $_.IdentityReference.Value } |
        Sort-Object -Unique
    foreach ($principal in $otherPrincipals) {
        $icaclsPrincipal = if ($principal -like 'S-1-*') { "*$principal" } else { $principal }
        & icacls.exe $Path /remove:g $icaclsPrincipal /remove:d $icaclsPrincipal | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not remove ACL entry $principal from $Path" }
    }

    $unexpected = Get-Acl -LiteralPath $Path -ErrorAction Stop |
        Select-Object -ExpandProperty Access |
        Where-Object { $_.IdentityReference.Value -ine $Account }
    if ($unexpected) { throw "Unexpected ACL entries remain on $Path" }
}

function Protect-GatewayData {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

    # Protect the directory before credentials are generated, then remove any
    # pre-existing explicit/inherited ACEs from descendants as well.
    Set-PrivateItemAcl -Path $DataDir -IsContainer $true -Account $account
    $items = Get-ChildItem -LiteralPath $DataDir -Force -Recurse -ErrorAction Stop |
        Where-Object { -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) } |
        Sort-Object -Property FullName
    foreach ($item in $items) {
        Set-PrivateItemAcl -Path $item.FullName -IsContainer $item.PSIsContainer -Account $account
    }
}

function Invoke-GatewayStart {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Protect-GatewayData

    & $Python (Join-Path $GatewayRoot 'configure.py') init | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize Internet Gateway configuration' }

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if (-not $process -or $process.CommandLine -notlike '*E:\localllm\internet-gateway\server.py*') {
            throw "Port $Port is occupied by an unexpected process"
        }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
        if ($health.status -ne 'ok') { throw 'Internet Gateway health check failed' }
        Write-Host "Qwen Internet Gateway already ready on 127.0.0.1:$Port" -ForegroundColor Green
        return
    }

    $stdout = Join-Path $LogDir 'internet-gateway.log'
    $stderr = Join-Path $LogDir 'internet-gateway-error.log'
    $process = Start-Process -FilePath $Python `
        -ArgumentList @((Join-Path $GatewayRoot 'server.py')) `
        -WorkingDirectory $GatewayRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) { throw "Internet Gateway exited early. See $stderr" }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($health.status -eq 'ok') {
                Write-Host "Qwen Internet Gateway ready on 127.0.0.1:$Port" -ForegroundColor Green
                return
            }
        } catch {}
    }

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "Internet Gateway did not open port $Port. See $stderr"
}

$startMutex = [System.Threading.Mutex]::new($false, 'Local\QwenInternetGatewayStart')
$ownsMutex = $false
try {
    try {
        $ownsMutex = $startMutex.WaitOne(120000)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) { throw 'Timed out waiting for another Internet Gateway startup' }
    Invoke-GatewayStart
} finally {
    if ($ownsMutex) { [void]$startMutex.ReleaseMutex() }
    $startMutex.Dispose()
}
