$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$LogDir = Join-Path $Root 'logs'
# The models folder comes from the User OLLAMA_MODELS that the installer writes
# (E:\ollama-models on the machine this was built on), so this file is identical in the
# installer payload and here. A models folder that cannot be created must not stop the
# whole suite: this script runs inside start-webui.ps1 under ErrorAction Stop.
$ModelDir = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User')
if (-not $ModelDir) { $ModelDir = Join-Path $Root 'models' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
try { New-Item -ItemType Directory -Force -Path $ModelDir -ErrorAction Stop | Out-Null } catch { }

function Test-LocalPort([int]$Port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

# The eGPU watcher (Resolve-OllamaGpu.ps1, added 2026-09-29) must run whenever the server
# does - also when the server is already up and this script exits early.
function Start-GpuWatchIfPresent {
    try {
        $helper = Join-Path $Root 'Resolve-OllamaGpu.ps1'
        if (Test-Path -LiteralPath $helper) { . $helper; Start-QwenGpuWatch -Root $Root }
    } catch { }
}

if (Test-LocalPort 11434) { Start-GpuWatchIfPresent; exit 0 }

# Two launchers run at every logon - this one (via start-webui.ps1) and
# start-ollama-server.ps1 (via the "Ollama Server" Startup shortcut). Both used to do a
# bare "is the port listening?" check and then start a server, which is a check-then-act
# race: on 2026-09-21 and 2026-09-22 the winner differed and the session ended up with
# different settings. The mutex makes the check and the start one atomic step; the port
# is re-checked inside it because the other launcher may have finished while we waited.
$OllamaMutex = [System.Threading.Mutex]::new($false, 'Local\QwenSuite-Ollama')
$OwnsMutex = $false
try {
    try {
        $OwnsMutex = $OllamaMutex.WaitOne(120000)
    } catch [System.Threading.AbandonedMutexException] {
        $OwnsMutex = $true
    }
    if (-not $OwnsMutex) {
        # Someone else is starting it and is taking too long; fall back to waiting on the port.
        $Deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $Deadline) {
            if (Test-LocalPort 11434) { exit 0 }
            Start-Sleep -Milliseconds 750
        }
        throw 'Another launcher holds the Ollama start lock and port 11434 never opened.'
    }
    if (Test-LocalPort 11434) { Start-GpuWatchIfPresent; exit 0 }

$Candidates = @(
    (Join-Path $Root 'runtime\ollama\ollama.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
    'C:\Program Files\Ollama\ollama.exe'
)
$Command = Get-Command ollama.exe -ErrorAction SilentlyContinue
if ($Command) { $Candidates += $Command.Source }
$Ollama = $Candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $Ollama) { throw 'Ollama executable was not found.' }

# Configuration comes from one place: the User-scope environment in the registry -
# the same source start-ollama-server.ps1 reads. Until 2026-09-22 this script kept its
# own hardcoded block, so whichever of the two logon launchers won the race decided the
# server's configuration for the whole session. They disagreed on
# OLLAMA_MAX_LOADED_MODELS (1 here vs 2 in the registry, measurably different on
# 2026-09-21 vs 2026-09-22) and this script never set OLLAMA_CONTEXT_LENGTH,
# OLLAMA_FLASH_ATTENTION or OLLAMA_GPU_OVERHEAD at all.
$Fallbacks = @{
    'OLLAMA_MODELS'            = $ModelDir
    'OLLAMA_HOST'              = '127.0.0.1:11434'
    'OLLAMA_KEEP_ALIVE'        = '2h'
    'OLLAMA_NUM_PARALLEL'      = '1'
    'OLLAMA_MAX_LOADED_MODELS' = '1'
}
foreach ($name in @(
    'OLLAMA_MODELS', 'OLLAMA_HOST', 'OLLAMA_KEEP_ALIVE', 'OLLAMA_CONTEXT_LENGTH',
    'OLLAMA_FLASH_ATTENTION', 'OLLAMA_MAX_LOADED_MODELS', 'OLLAMA_NUM_PARALLEL',
    'OLLAMA_GPU_OVERHEAD', 'OLLAMA_LLM_LIBRARY', 'OLLAMA_KV_CACHE_TYPE'
)) {
    # Clear inherited values first: otherwise a User override that was deleted
    # (OLLAMA_LLM_LIBRARY is the one that bites) resurrects through inheritance.
    Remove-Item -Path "env:$name" -ErrorAction SilentlyContinue
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if (-not $value -and $Fallbacks.ContainsKey($name)) { $value = $Fallbacks[$name] }
    if ($value) { Set-Item -Path "env:$name" -Value $value }
}

# Which GPUs the server may use (added 2026-09-29 for the Thunderbolt eGPUs): an attached
# eGPU on its own, never together with a small or pre-Volta card such as the Quadro P620;
# with no eGPU, Ollama's own choice (CPU only once the P620 is disabled, path B). Same
# helper as start-ollama-server.ps1. This script
# runs inside start-webui.ps1 with ErrorAction Stop, so nothing here may throw: if the
# helper is missing or fails, the server starts with Ollama's defaults.
$GpuHelper = Join-Path $Root 'Resolve-OllamaGpu.ps1'
$GpuSaved = $null
$GpuReady = $false
if (Test-Path -LiteralPath $GpuHelper) {
    try {
        . $GpuHelper
        $GpuSaved = Resolve-QwenGpuProfile -Root $Root -OllamaExe $Ollama -Launcher 'Start-Ollama.ps1' -ServerLog (Join-Path $LogDir 'ollama-error.log')
        $GpuReady = $true
    } catch { }
}

$Server = Start-Process -FilePath $Ollama `
    -ArgumentList @('serve') `
    -WorkingDirectory (Split-Path -Parent $Ollama) `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $LogDir 'ollama.log') `
    -RedirectStandardError (Join-Path $LogDir 'ollama-error.log')
if ($GpuReady) {
    try {
        # The GPU variables were for the server only; everything start-webui.ps1 starts
        # after this (tools, router, WebUI) inherits this process's environment.
        Restore-QwenGpuEnvironment $GpuSaved
        # Only stamped when this start wrote the record, so a stale record never looks current.
        if ($Server -and $script:QwenGpuRecordWritten) { [void](Set-QwenGpuProfileServerPid -Root $Root -ServerPid $Server.Id) }
        Start-QwenGpuWatch -Root $Root
    } catch { }
}

    $Deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $Deadline) {
        if (Test-LocalPort 11434) { exit 0 }
        Start-Sleep -Milliseconds 750
    }
    throw 'Ollama did not open port 11434 within 120 seconds.'
} finally {
    if ($OwnsMutex) { [void]$OllamaMutex.ReleaseMutex() }
    $OllamaMutex.Dispose()
}
