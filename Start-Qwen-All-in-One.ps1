param([switch]$NoOpen, [switch]$NoDialogs)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$LogDir = Join-Path $Root 'logs'
$Log = Join-Path $LogDir 'qwen-all-in-one.log'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log([string]$Message) {
    Add-Content -LiteralPath $Log -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),$Message) -Encoding UTF8
}

function Test-LocalPort([int]$Port) {
    $Client = [Net.Sockets.TcpClient]::new()
    try {
        $Async = $Client.BeginConnect('127.0.0.1',$Port,$null,$null)
        if (-not $Async.AsyncWaitHandle.WaitOne(400)) { return $false }
        $Client.EndConnect($Async)
        return $true
    } catch { return $false } finally { $Client.Dispose() }
}

function Wait-LocalPort([int]$Port,[int]$Seconds) {
    $Deadline=(Get-Date).AddSeconds($Seconds)
    while((Get-Date)-lt $Deadline){if(Test-LocalPort $Port){return $true};Start-Sleep -Milliseconds 750}
    return $false
}

function Show-Message([string]$Text,[string]$Icon) {
    if($NoDialogs){return}
    try{Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show($Text,'Qwen All-in-One','OK',$Icon)|Out-Null}catch{}
}

function Find-OpenClaw {
    @(
        (Join-Path $Root 'openclaw-runtime\openclaw.cmd'),
        (Join-Path $Root 'openclaw-runtime\node_modules\.bin\openclaw.cmd')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

try {
    Write-Log 'Startup requested.'
    $CoreScript = Join-Path $Root 'Start-WebUI.ps1'
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $CoreScript) `
        -WorkingDirectory $Root -WindowStyle Hidden | Out-Null

    if(-not (Wait-LocalPort 11434 180)){throw 'Ollama did not start within 3 minutes.'}

    $Warnings=[Collections.Generic.List[string]]::new()
    $OpenClaw=Find-OpenClaw
    if($OpenClaw -and -not (Test-LocalPort 18789)){
        try{
            & $OpenClaw gateway start | Out-Null
            if(-not (Wait-LocalPort 18789 120)){throw 'OpenClaw Gateway did not open port 18789.'}
        }catch{$Warnings.Add($_.Exception.Message)}
    }

    if(-not (Wait-LocalPort 3000 420)){throw 'Open WebUI did not start within 7 minutes.'}
    foreach($Check in @(
        @{Name='Local Computer Tools';Port=8765},
        @{Name='Local Computer Fast';Port=8766},
        @{Name='OpenClaw Permission Gateway';Port=8767},
        @{Name='Smart Model Router';Port=8770},
        @{Name='Qwen3 Reranker';Port=8771},
        @{Name='Internet Gateway';Port=8780},
        @{Name='OpenClaw messaging';Port=18789}
    )){
        if(-not (Wait-LocalPort $Check.Port 15)){$Warnings.Add("$($Check.Name) is not ready on port $($Check.Port).")}
    }

    if($Warnings.Count){Show-Message ("Qwen is open, but some optional services need attention:`n`n- "+($Warnings -join "`n- ")+"`n`nLog: $Log") 'Warning'}
    if(-not $NoOpen){Start-Process 'http://127.0.0.1:3000'}
    Write-Log 'Startup completed.'
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    Show-Message ("Qwen could not start completely.`n`n$($_.Exception.Message)`n`nLog: $Log") 'Error'
    exit 1
}
