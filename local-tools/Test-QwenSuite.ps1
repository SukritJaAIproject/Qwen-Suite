# Qwen Suite health check for the console (added 2026-09-30).
#
# Prints the same report as http://127.0.0.1:8765/status and the chat action suite_status.
# It runs local-tools\suite_health.py directly instead of asking :8765, so it still works
# when the local-tools server itself is down - which is when you need it most.
# Everything it does is read-only: no restarts, no model calls, nothing is changed or killed.
#
#   .\Test-QwenSuite.ps1                 # full report (about 3-13 s)
#   .\Test-QwenSuite.ps1 -Only light     # quick part only: ports, Ollama, disk, processes...
#   .\Test-QwenSuite.ps1 -Only ports,ollama,channels
#   .\Test-QwenSuite.ps1 -Json           # the whole report as JSON
#
# Exit code: 0 = all normal (or not checkable), 1 = something to look at, 2 = a real problem,
# 3 = the check itself could not run.
[CmdletBinding()]
param(
    [string]$Only = '',
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'suite_health.py'
$python = Join-Path (Split-Path -Parent $here) 'webui-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    $python = if ($cmd) { $cmd.Source } else { $null }
}
if (-not $python -or -not (Test-Path -LiteralPath $script)) {
    Write-Host 'ไม่พบ Python ของชุด (webui-venv) หรือ suite_health.py - ตรวจสถานะไม่ได้' -ForegroundColor Red
    exit 3
}

$pyArgs = @($script, '--json')
if ($Only) { $pyArgs += @('--only', $Only) }

# Read the child's UTF-8 output as UTF-8 (PowerShell 5.1 would otherwise decode it with the
# console code page and mangle the Thai text); restored afterwards.
$savedEncoding = [Console]::OutputEncoding
$savedPyEnc = $env:PYTHONIOENCODING
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
    # PS 5.1 turns any stderr line of a native command into a terminating error under
    # 'Stop', even with 2>$null - a Python warning must not abort the report.
    $ErrorActionPreference = 'Continue'
    $raw = & $python @pyArgs 2>$null
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = $savedEncoding
    $env:PYTHONIOENCODING = $savedPyEnc
}

$text = ($raw | Out-String).Trim()
if ($Json) {
    Write-Output $text
    exit $code
}
try {
    if (-not $text) { throw 'empty output' }
    $report = $text | ConvertFrom-Json
    if (-not $report.checks) { throw 'no checks in output' }
} catch {
    Write-Host 'suite_health.py ไม่ได้ส่งผลกลับมาเป็น JSON:' -ForegroundColor Red
    Write-Host $text
    exit 3
}

$colors = @{ ok = 'Green'; warn = 'Yellow'; bad = 'Red'; unknown = 'DarkGray' }
Write-Host ''
Write-Host ('Qwen Suite - {0}  ({1}, {2} s)' -f $report.overall_th, $report.checked_at, $report.took_s) -ForegroundColor $colors[[string]$report.overall]
foreach ($prop in $report.checks.PSObject.Properties) {
    $c = $prop.Value
    Write-Host ('  [{0}] ' -f $c.status_th) -ForegroundColor $colors[[string]$c.status] -NoNewline
    Write-Host ('{0}: ' -f $c.title) -NoNewline
    Write-Host $c.summary
}
Write-Host ''
Write-Host ('รายละเอียด: {0}' -f $report.page) -ForegroundColor DarkGray
exit $code
