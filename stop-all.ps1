# Stop Ollama and Open WebUI cleanly and give the RAM back.
#
# Why this exists: killing ollama.exe on its own leaves its llama-server child
# running, still holding the full model in RAM (about 20 GB for qwen3.8:27b) with
# nothing pointing at it. This stops the children too.

Write-Host 'Stopping Open WebUI...' -ForegroundColor Yellow
$webUiProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like '*E:\localllm\webui-venv\Scripts\open-webui.exe*serve*'
    }
foreach ($process in $webUiProcesses) {
    Stop-Process -Id $process.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host 'Stopping local computer tools...' -ForegroundColor Yellow
$toolProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like '*E:\localllm\local-tools\server.py*' -or
        $_.CommandLine -like '*E:\localllm\local-tools\fast_server.py*'
    }
foreach ($process in $toolProcesses) {
    Stop-Process -Id $process.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host 'Stopping Internet Gateway and tunnel...' -ForegroundColor Yellow
$internetProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like '*E:\localllm\internet-gateway\server.py*' -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*E:\localllm\internet-gateway\watch-internet-gateway.ps1*') -or
        ($_.Name -eq 'cloudflared.exe' -and $_.CommandLine -like '*tunnel*--url*127.0.0.1:8780*')
    }
foreach ($process in $internetProcesses) {
    Stop-Process -Id $process.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host 'Stopping AI services (router, reranker)...' -ForegroundColor Yellow
# Added 2026-09-22: these were missed, so "stop everything" left the Smart Model Router
# on 8770 and the reranker on 8771 running - and the reranker keeps its model in RAM.
$aiServiceProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like '*E:\localllm\ai-services\router.py*' -or
        $_.CommandLine -like '*E:\localllm\ai-services\reranker.py*'
    }
foreach ($process in $aiServiceProcesses) {
    Stop-Process -Id $process.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue
}

Write-Host 'Stopping Ollama...' -ForegroundColor Yellow
# First the eGPU watcher, a restart in progress and any launcher it already started
# (added 2026-09-29) - otherwise a launcher that is still starting up could bring Ollama
# back after this script has stopped it. Then the "restart interrupted" marker, so the
# next watcher does not finish a restart the user has just cancelled.
function Stop-OllamaStarters {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and $_.CommandLine -and (
                $_.CommandLine -like '*watch-ollama-gpu.ps1*' -or
                $_.CommandLine -like '*Restart-QwenOllama.ps1*' -or
                $_.CommandLine -like '*start-ollama-server.ps1*' -or
                $_.CommandLine -like '*-File*Start-Ollama.ps1*')
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
}
Stop-OllamaStarters
Remove-Item -LiteralPath 'E:\localllm\data\ollama-restart.json' -Force -ErrorAction SilentlyContinue
Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue |
    Stop-Process -Force -Confirm:$false
Start-Sleep -Seconds 3
# second pass: anything that slipped through the first one
Stop-OllamaStarters
Get-Process -Name 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false

# the part everyone forgets
$orphans = Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue
foreach ($p in $orphans) {
    $gb = [math]::Round($p.WorkingSet64 / 1GB, 2)
    Write-Host "  releasing llama-server (pid $($p.Id), $gb GB)" -ForegroundColor DarkYellow
    Stop-Process -Id $p.Id -Force -Confirm:$false
}

Start-Sleep -Seconds 2
$free = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 1)
Write-Host ''
Write-Host "Done. RAM free: $free GB" -ForegroundColor Green

# Deliberately NOT stopped here: the OpenClaw gateway on 18789 (Telegram/WhatsApp).
# It is started by the scheduled task at logon and would stay down until the next logon,
# which is not what "give the RAM back" should cost. Use Stop-Qwen-Suite.ps1 to stop it too.
$openClawStillUp = Get-NetTCPConnection -State Listen -LocalPort 18789 -ErrorAction SilentlyContinue
if ($openClawStillUp) {
    Write-Host 'Note: OpenClaw gateway (18789) is still running - use Stop-Qwen-Suite.ps1 to stop it as well.' -ForegroundColor DarkGray
}
