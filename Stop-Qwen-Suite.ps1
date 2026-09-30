$ErrorActionPreference = 'SilentlyContinue'
$Root = $PSScriptRoot

$OpenClaw = @(
    (Join-Path $Root 'openclaw-runtime\openclaw.cmd'),
    (Join-Path $Root 'openclaw-runtime\node_modules\.bin\openclaw.cmd')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if($OpenClaw){& $OpenClaw gateway stop --force | Out-Null}

# Case-insensitive: the logon shortcut runs "start-webui.ps1" while this used to look for
# "Start-WebUI.ps1", so it never matched on the machine it was written on.
function Test-Has([string]$Text, [string]$Part) {
    return $Text -and $Text.IndexOf($Part, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

# The eGPU watcher, a restart in progress and any Ollama launcher still starting up
# (added 2026-09-29) go first, so none of them can bring Ollama back after this.
function Stop-OllamaStarters {
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -eq 'powershell.exe' -and (Test-Has $_.CommandLine $Root) -and (
            (Test-Has $_.CommandLine 'watch-ollama-gpu.ps1') -or
            (Test-Has $_.CommandLine 'Restart-QwenOllama.ps1') -or
            (Test-Has $_.CommandLine 'start-ollama-server.ps1') -or
            (Test-Has $_.CommandLine 'Start-Ollama.ps1')
        )
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
Stop-OllamaStarters
Remove-Item -LiteralPath (Join-Path $Root 'data\ollama-restart.json') -Force -ErrorAction SilentlyContinue

$Ports=3000,8765,8766,8767,8770,8771,8780,18789
foreach($Port in $Ports){
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique |
      ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}

Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and (Test-Has $_.CommandLine $Root) -and (
        $_.Name -in @('python.exe','cloudflared.exe') -or
        ($_.Name -eq 'powershell.exe' -and (
            (Test-Has $_.CommandLine 'Start-WebUI.ps1') -or
            (Test-Has $_.CommandLine 'start-internet-watchdog.ps1') -or
            (Test-Has $_.CommandLine 'watch-internet-gateway.ps1')
        ))
    )
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Added 2026-09-22: Ollama was never stopped here, so "stop the suite" left the server on
# 11434 and - worse - its llama-server child holding the whole model (about 18-20 GB) in RAM.
Get-Process -Name 'ollama app','ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Stop-OllamaStarters
Get-Process -Name 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host ("  releasing llama-server (pid {0}, {1} GB)" -f $_.Id, [math]::Round($_.WorkingSet64 / 1GB, 2))
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
