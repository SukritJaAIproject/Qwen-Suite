$ErrorActionPreference = 'Stop'

$openClaw = 'E:\localllm\openclaw-runtime\openclaw.cmd'
$ollamaStart = 'E:\localllm\start-ollama-server.ps1'

function Test-LocalPort([int]$Port) {
    return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

try {
    if (-not (Test-Path -LiteralPath $openClaw)) {
        throw "OpenClaw was not found at $openClaw"
    }

    if (-not (Test-LocalPort 11434)) {
        $startArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $ollamaStart)
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $startArgs -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw 'Ollama could not be started.'
        }
    }

    if (-not (Test-LocalPort 18789)) {
        Start-ScheduledTask -TaskName 'OpenClaw Gateway'
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-LocalPort 18789) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
        }
        if (-not (Test-LocalPort 18789)) {
            throw 'OpenClaw Gateway did not become ready within 90 seconds.'
        }
    }

    & $openClaw dashboard --yes
    if ($LASTEXITCODE -ne 0) {
        throw 'OpenClaw could not open its dashboard.'
    }
}
catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Qwen OpenClaw', 'OK', 'Error') | Out-Null
    exit 1
}
