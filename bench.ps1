# Non-disruptive benchmark for the production Qwen configuration.
#
#   powershell -ExecutionPolicy Bypass -File E:\localllm\bench.ps1
#   powershell -ExecutionPolicy Bypass -File E:\localllm\bench.ps1 -Runs 5 -NumPredict 96
#
# The script does not stop or unload models. It warms the selected model first,
# keeps the runner options identical to production (the server's 32768 context, no
# num_thread, the default shift - see README "runner options"), so it neither reloads the
# model nor makes the next production request reload it, and reports median/p95.

param(
    [string]$Model = 'qwen3.8:27b',
    [ValidateRange(1, 20)][int]$Runs = 3,
    [ValidateRange(8, 4096)][int]$NumPredict = 64,
    [ValidateRange(1024, 262144)][int]$NumCtx = 32768,
    [ValidateRange(0, 64)][int]$NumThread = 0
)

$ErrorActionPreference = 'Stop'
$Api = 'http://127.0.0.1:11434'
$Prompt = 'Explain in about 150 words how a pumped-storage hydro plant works.'

function Invoke-Generate {
    param([int]$Predict)

    $body = @{
        model      = $Model
        prompt     = $Prompt
        stream     = $false
        think      = $false
        keep_alive = '2h'
        options    = @{
            num_predict = $Predict
            num_ctx     = $NumCtx
            temperature = 0.7
        }
    }
    # 0 = not sent (production sends none); any other value reloads the production model
    if ($NumThread -gt 0) { $body.options.num_thread = $NumThread }
    $body = $body | ConvertTo-Json -Depth 5

    Invoke-RestMethod -Uri "$Api/api/generate" -Method Post -Body $body `
        -ContentType 'application/json' -TimeoutSec 900
}

function Get-Percentile {
    param([double[]]$Values, [double]$Percentile)
    $sorted = @($Values | Sort-Object)
    if ($sorted.Count -eq 1) { return $sorted[0] }
    $index = [math]::Ceiling(($Percentile / 100) * $sorted.Count) - 1
    return $sorted[[math]::Max(0, [math]::Min($index, $sorted.Count - 1))]
}

Write-Host "Warming $Model (ctx=$NumCtx, threads=$NumThread) ..." -ForegroundColor Cyan
$null = Invoke-Generate -Predict 1

$results = @()
for ($run = 1; $run -le $Runs; $run++) {
    Write-Host "Run $run/$Runs ..." -ForegroundColor DarkCyan
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-Generate -Predict $NumPredict
    $stopwatch.Stop()
    $promptTps = if ($response.prompt_eval_duration -gt 0) {
        $response.prompt_eval_count / ($response.prompt_eval_duration / 1e9)
    } else { 0 }
    $outputTps = if ($response.eval_duration -gt 0) {
        $response.eval_count / ($response.eval_duration / 1e9)
    } else { 0 }
    $results += [pscustomobject]@{
        Run            = $run
        'Wall (s)'     = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
        'Load (s)'     = [math]::Round($response.load_duration / 1e9, 2)
        'Prompt tok/s' = [math]::Round($promptTps, 2)
        'Output tok/s' = [math]::Round($outputTps, 2)
        'Tokens out'   = $response.eval_count
    }
}

$promptRates = [double[]]@($results | ForEach-Object { $_.'Prompt tok/s' })
$outputRates = [double[]]@($results | ForEach-Object { $_.'Output tok/s' })
$wallTimes = [double[]]@($results | ForEach-Object { $_.'Wall (s)' })

Write-Host ''
Write-Host '=== Individual runs ===' -ForegroundColor Green
$results | Format-Table -AutoSize
Write-Host '=== Summary ===' -ForegroundColor Green
[pscustomobject]@{
    Model                 = $Model
    Context               = $NumCtx
    Threads               = $NumThread
    Runs                  = $Runs
    'Prompt median tok/s' = [math]::Round((Get-Percentile $promptRates 50), 2)
    'Output median tok/s' = [math]::Round((Get-Percentile $outputRates 50), 2)
    'Wall median (s)'     = [math]::Round((Get-Percentile $wallTimes 50), 2)
    'Wall p95 (s)'        = [math]::Round((Get-Percentile $wallTimes 95), 2)
} | Format-List
