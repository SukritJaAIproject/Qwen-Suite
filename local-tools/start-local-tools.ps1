$ErrorActionPreference = 'Stop'

$ToolRoot = 'E:\localllm\local-tools'
$Python = 'E:\localllm\webui-venv\Scripts\python.exe'
$LogDir = 'E:\localllm\logs'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Seed the user-editable command-policy rules once. The shipped defaults live next to the
# tools; the live copy under data\ is what the owner edits and what command_policy.py re-reads
# on mtime change. Never overwrite an existing copy.
$CommandRulesDir = 'E:\localllm\data\permission-center'
$CommandRulesFile = Join-Path $CommandRulesDir 'command-rules.json'
$CommandRulesSeed = Join-Path $ToolRoot 'default-command-rules.json'
if ((Test-Path -LiteralPath $CommandRulesSeed) -and -not (Test-Path -LiteralPath $CommandRulesFile)) {
    New-Item -ItemType Directory -Force -Path $CommandRulesDir | Out-Null
    Copy-Item -LiteralPath $CommandRulesSeed -Destination $CommandRulesFile
    Write-Host "Seeded command-rules.json from defaults." -ForegroundColor Green
}

function Test-LocalListener {
    param([int]$Port)
    try {
        return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
    } catch {
        return $false
    }
}

function Start-LocalToolService {
    param(
        [string]$ScriptName,
        [int]$Port,
        [string]$LogStem,
        [string]$DisplayName,
        [string[]]$ExtraArguments = @()
    )
    if (Test-LocalListener -Port $Port) {
        Write-Host "$DisplayName already listening on 127.0.0.1:$Port" -ForegroundColor Green
        return
    }

    $stdout = Join-Path $LogDir "$LogStem.log"
    $stderr = Join-Path $LogDir "$LogStem-error.log"
    $argumentList = @((Join-Path $ToolRoot $ScriptName)) + $ExtraArguments
    $process = Start-Process -FilePath $Python `
        -ArgumentList $argumentList `
        -WorkingDirectory $ToolRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "$DisplayName exited early. See $stderr"
        }
        if (Test-LocalListener -Port $Port) {
            Write-Host "$DisplayName ready on 127.0.0.1:$Port" -ForegroundColor Green
            return
        }
    }

    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "$DisplayName did not open port $Port. See $stderr"
}

Start-LocalToolService -ScriptName 'server.py' -Port 8765 -LogStem 'local-tools' -DisplayName 'Local Computer full tools'
Start-LocalToolService -ScriptName 'fast_server.py' -Port 8766 -LogStem 'local-tools-fast' -DisplayName 'Open WebUI permission gateway' -ExtraArguments @('--port','8766','--source','webui')
Start-LocalToolService -ScriptName 'fast_server.py' -Port 8767 -LogStem 'local-tools-openclaw' -DisplayName 'OpenClaw permission gateway' -ExtraArguments @('--port','8767','--source','openclaw')

# Long video tasks must keep their originating Open WebUI chat alive even when
# its streaming response disconnects or the browser is refreshed.
$Supervisor = Join-Path $ToolRoot 'chat_task_supervisor.py'
$SupervisorProcess = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*chat_task_supervisor.py*daemon*' } |
    Select-Object -First 1
if (-not $SupervisorProcess -and (Test-Path -LiteralPath $Supervisor)) {
    Start-Process -FilePath $Python `
        -ArgumentList @($Supervisor, 'daemon') `
        -WorkingDirectory $ToolRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'chat-task-supervisor.log') `
        -RedirectStandardError (Join-Path $LogDir 'chat-task-supervisor-error.log') | Out-Null
    Write-Host 'Chat Task Supervisor started.' -ForegroundColor Green
} elseif ($SupervisorProcess) {
    Write-Host 'Chat Task Supervisor already running.' -ForegroundColor Green
}
