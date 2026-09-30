# Make sure the Ollama API server is running on 127.0.0.1:11434, with the tuned
# settings actually applied.
#
# Why this exists:
#  1. The Ollama tray app (0.32.9) is unreliable at spawning its own server on this
#     machine - it writes an empty server.log and gives up with "ollama server not
#     ready after retries". It attaches fine to a server that is already running.
#  2. A process only sees User-level environment variables if its parent was started
#     after they were set. Rather than hope, this reads them out of the registry and
#     applies them explicitly, so the server is configured correctly no matter who
#     launched this script.
#
# Safe to run twice: if the port is already listening it exits immediately.

# The eGPU watcher (Resolve-OllamaGpu.ps1, added 2026-09-29) must run whenever the server
# does - also when this script finds the server already up and exits early.
function Start-GpuWatchIfPresent {
    try {
        $helper = Join-Path $PSScriptRoot 'Resolve-OllamaGpu.ps1'
        if (Test-Path -LiteralPath $helper) { . $helper; Start-QwenGpuWatch -Root $PSScriptRoot }
    } catch { }
}

if (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue) {
    Start-GpuWatchIfPresent
    exit 0
}

# Shared with Start-Ollama.ps1, which start-webui.ps1 runs at the same logon. Without
# this both scripts could pass the port check and start a server each, and whichever won
# decided the session's settings (measured: OLLAMA_MAX_LOADED_MODELS was 1 one day and 2
# the next). Held across the check and the start, released in the finally below.
$ollamaMutex = [System.Threading.Mutex]::new($false, 'Local\QwenSuite-Ollama')
$ownsMutex = $false
try {
    try {
        $ownsMutex = $ollamaMutex.WaitOne(120000)
    } catch [System.Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        $waitUntil = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $waitUntil) {
            if (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue) { exit 0 }
            Start-Sleep -Seconds 2
        }
        exit 1
    }
    if (Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue) { Start-GpuWatchIfPresent; exit 0 }

$exe ="$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path $exe)) { exit 1 }

# pull the tuned settings straight from the User environment in the registry
foreach ($name in @(
    'OLLAMA_MODELS', 'OLLAMA_KEEP_ALIVE', 'OLLAMA_CONTEXT_LENGTH', 'OLLAMA_FLASH_ATTENTION',
    'OLLAMA_MAX_LOADED_MODELS', 'OLLAMA_NUM_PARALLEL', 'OLLAMA_GPU_OVERHEAD', 'OLLAMA_LLM_LIBRARY',
    'OLLAMA_KV_CACHE_TYPE'
)) {
    # Clear inherited values first. Otherwise deleting a User-level override can
    # leave a stale value (especially OLLAMA_LLM_LIBRARY) in the launcher process.
    Remove-Item -Path "env:$name" -ErrorAction SilentlyContinue
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($value) { Set-Item -Path "env:$name" -Value $value }
}

$logDir = 'E:\localllm\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Which GPUs the server may use (added 2026-09-29 for the Thunderbolt eGPUs): an attached
# eGPU on its own, never together with the Quadro P620; with no eGPU, Ollama's own choice
# (on path A the P620 - 3.5x faster prompt reading than CPU-only here; on path B the P620 is
# disabled, so CPU only). Decided per start because
# Ollama only looks for GPUs when it starts. If the helper is missing or fails, the server
# starts with Ollama's defaults.
$gpuHelper = Join-Path $PSScriptRoot 'Resolve-OllamaGpu.ps1'
$gpuReady = $false
$gpuSaved = $null
if (Test-Path -LiteralPath $gpuHelper) {
    try {
        . $gpuHelper
        $gpuSaved = Resolve-QwenGpuProfile -Root $PSScriptRoot -OllamaExe $exe -Launcher 'start-ollama-server.ps1' -ServerLog "$logDir\ollama-server.log"
        $gpuReady = $true
    } catch { }
}

$server = Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
    -RedirectStandardError "$logDir\ollama-server.log" `
    -RedirectStandardOutput "$logDir\ollama-server.out"
if ($gpuReady) {
    try {
        # The GPU variables were for the server only.
        Restore-QwenGpuEnvironment $gpuSaved
        # The watcher compares this pid with the server that owns 11434 to tell whether the
        # running server was started with the recorded profile. Only stamped when this
        # start wrote the record, so a stale record never looks current.
        if ($server -and $script:QwenGpuRecordWritten) { [void](Set-QwenGpuProfileServerPid -Root $PSScriptRoot -ServerPid $server.Id) }
        Start-QwenGpuWatch -Root $PSScriptRoot
    } catch { }
}

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try { Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 3 | Out-Null; exit 0 }
        catch { Start-Sleep -Seconds 2 }
    }
    exit 1
} finally {
    if ($ownsMutex) { [void]$ollamaMutex.ReleaseMutex() }
    $ollamaMutex.Dispose()
}
