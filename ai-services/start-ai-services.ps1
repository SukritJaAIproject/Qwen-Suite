$ErrorActionPreference = 'Stop'

$Root = 'E:\localllm'
$ServiceRoot = Join-Path $Root 'ai-services'
$Python = Join-Path $Root 'webui-venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$ModelPath = Join-Path $Root 'model-cache\Qwen3-Reranker-0.6B'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-LocalListener([int]$Port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Start-QwenService {
    param([string]$Script,[int]$Port,[string]$Name,[string]$LogStem)
    if(Test-LocalListener $Port){Write-Host "$Name already ready on $Port" -ForegroundColor Green;return}
    $env:QWEN_SUITE_ROOT=$Root
    $env:QWEN_OLLAMA_URL='http://127.0.0.1:11434'
    $env:QWEN_ROUTER_LOG=Join-Path $LogDir 'model-router.jsonl'
    $env:QWEN_RERANKER_MODEL_PATH=$ModelPath
    $process=Start-Process -FilePath $Python `
      -ArgumentList @((Join-Path $ServiceRoot $Script)) `
      -WorkingDirectory $ServiceRoot -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $LogDir "$LogStem.log") `
      -RedirectStandardError (Join-Path $LogDir "$LogStem-error.log") -PassThru
    for($attempt=0;$attempt-lt 120;$attempt++){
        Start-Sleep -Milliseconds 500
        if($process.HasExited){throw "$Name exited early. See $LogDir\$LogStem-error.log"}
        if(Test-LocalListener $Port){Write-Host "$Name ready on 127.0.0.1:$Port" -ForegroundColor Green;return}
    }
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "$Name did not open port $Port."
}

Start-QwenService -Script 'router.py' -Port 8770 -Name 'Smart Model Router' -LogStem 'model-router'
Start-QwenService -Script 'reranker.py' -Port 8771 -Name 'Qwen3 Reranker' -LogStem 'reranker'

