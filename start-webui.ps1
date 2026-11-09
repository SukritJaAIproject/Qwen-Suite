$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$Venv = Join-Path $Root 'webui-venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$WebUI = Join-Path $Venv 'Scripts\open-webui.exe'
$LogDir = Join-Path $Root 'logs'
$DataDir = Join-Path $Root 'webui-data'
$CacheDir = Join-Path $Root 'hf-cache'

New-Item -ItemType Directory -Force -Path $LogDir,$DataDir,$CacheDir | Out-Null

function Test-LocalPort([int]$Port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

if (-not (Test-Path -LiteralPath $Python) -or -not (Test-Path -LiteralPath $WebUI)) {
    throw 'Open WebUI runtime is incomplete. Run Repair Qwen Suite.'
}

$RuntimePaths = @(
    (Join-Path $Root 'runtime\ffmpeg\bin'),
    (Join-Path $Root 'runtime\cloudflared'),
    (Join-Path $Root 'runtime\ollama')
) | Where-Object { Test-Path -LiteralPath $_ }
if ($RuntimePaths) { $env:Path = (($RuntimePaths -join ';') + ';' + $env:Path) }

& (Join-Path $Root 'Start-Ollama.ps1')

# Local Computer full and compact MCP servers are idempotent.
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'local-tools\start-local-tools.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Local Computer Tools could not start.' }

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'ai-services\start-ai-services.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Smart Model Router or Reranker could not start.' }

$env:DATA_DIR = $DataDir
$env:HF_HOME = $CacheDir
$env:SENTENCE_TRANSFORMERS_HOME = $CacheDir
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
$env:RAG_EMBEDDING_ENGINE = 'ollama'
$env:RAG_EMBEDDING_MODEL = 'qwen3-embedding:4b'
$env:RAG_OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
$env:ENABLE_RAG_HYBRID_SEARCH = 'true'
$env:RAG_RERANKING_ENGINE = 'external'
$env:RAG_RERANKING_MODEL = 'Qwen/Qwen3-Reranker-0.6B'
$env:RAG_EXTERNAL_RERANKER_URL = 'http://127.0.0.1:8771/v1/rerank'
$env:RAG_EXTERNAL_RERANKER_TIMEOUT = '300'
$env:RAG_TOP_K = '8'
$env:RAG_TOP_K_RERANKER = '4'
$env:RAG_HYBRID_BM25_WEIGHT = '0.4'
$env:CHUNK_SIZE = '800'
$env:CHUNK_OVERLAP = '120'
$env:WEBUI_AUTH = 'True'
$env:ENABLE_PASSWORD_AUTH = 'True'
$env:ENABLE_LOGIN_FORM = 'True'
$env:ENABLE_SIGNUP = 'True'
$env:DEFAULT_USER_ROLE = 'pending'
$env:ENABLE_INITIAL_ADMIN_SIGNUP = 'False'
# Password login is required. Self-registration is on (9 Nov 2026) but a new account stays
# "pending" until the owner approves it in Admin Panel > Users, and only admins reach the local
# tools. Only this machine's own Open WebUI page may call the API from a browser.
$env:CORS_ALLOW_ORIGIN = 'http://127.0.0.1:3000;http://localhost:3000'
# Voice input: skip silence (local faster-whisper, model in model-cache). Thai is the owner's
# default in Settings > Audio (set by Configure-Qwen-WebUI.py); WHISPER_LANGUAGE is not used
# because it would force Thai even for English dictation.
$env:WHISPER_VAD_FILTER = 'true'
$env:ANONYMIZED_TELEMETRY = 'False'
$env:DO_NOT_TRACK = 'true'
$env:SCARF_NO_ANALYTICS = 'true'

# Keep the custom dark UI after package repairs or upgrades.
try {
    $PackageRoot = (& $Python -c "import pathlib,open_webui; print(pathlib.Path(open_webui.__file__).resolve().parent)").Trim()
    $Frontend = Join-Path $PackageRoot 'frontend'
    $FrontendStatic = Join-Path $Frontend 'static'
    $RuntimeStatic = Join-Path $PackageRoot 'static'
    New-Item -ItemType Directory -Force -Path $FrontendStatic,$RuntimeStatic | Out-Null
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-modern-dark.css') -Destination (Join-Path $FrontendStatic 'custom.css') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-modern-dark.css') -Destination (Join-Path $RuntimeStatic 'custom.css') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-project-paths.js') -Destination (Join-Path $FrontendStatic 'qwen-project-paths.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-project-paths.js') -Destination (Join-Path $RuntimeStatic 'qwen-project-paths.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-permission-center.js') -Destination (Join-Path $FrontendStatic 'qwen-permission-center.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-permission-center.js') -Destination (Join-Path $RuntimeStatic 'qwen-permission-center.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-auto-continue.js') -Destination (Join-Path $FrontendStatic 'qwen-auto-continue.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-auto-continue.js') -Destination (Join-Path $RuntimeStatic 'qwen-auto-continue.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-video-studio.js') -Destination (Join-Path $FrontendStatic 'qwen-video-studio.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-video-studio.js') -Destination (Join-Path $RuntimeStatic 'qwen-video-studio.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-account.js') -Destination (Join-Path $FrontendStatic 'qwen-account.js') -Force
    Copy-Item -LiteralPath (Join-Path $Root 'theme\qwen-account.js') -Destination (Join-Path $RuntimeStatic 'qwen-account.js') -Force
    $IndexPath = Join-Path $Frontend 'index.html'
    if (Test-Path -LiteralPath $IndexPath) {
        $Index = [IO.File]::ReadAllText($IndexPath)
        if (-not $Index.Contains('/static/custom.css')) {
            $Index = $Index.Replace('</head>', '<link rel="stylesheet" href="/static/custom.css?v=qwen-suite-v1"></head>')
        }
        # 10 Nov 2026: the add-ons' labels follow Open WebUI's language (Thai or English); new ?v= so browsers reload them.
        if ($Index -match '/static/qwen-project-paths\.js\?v=[^"'']+') {
            $Index = [regex]::Replace($Index, '/static/qwen-project-paths\.js\?v=[^"'']+', '/static/qwen-project-paths.js?v=qwen-suite-v3')
        } elseif (-not $Index.Contains('/static/qwen-project-paths.js')) {
            $Index = $Index.Replace('</body>', '<script src="/static/qwen-project-paths.js?v=qwen-suite-v3" defer></script></body>')
        }
        if ($Index -match '/static/qwen-permission-center\.js\?v=[^"'']+') {
            $Index = [regex]::Replace($Index, '/static/qwen-permission-center\.js\?v=[^"'']+', '/static/qwen-permission-center.js?v=qwen-suite-v4')
        } elseif (-not $Index.Contains('/static/qwen-permission-center.js')) {
            $Index = $Index.Replace('</body>', '<script src="/static/qwen-permission-center.js?v=qwen-suite-v4" defer></script></body>')
        }
        if ($Index -match '/static/qwen-auto-continue\.js\?v=[^"'']+') {
            $Index = [regex]::Replace($Index, '/static/qwen-auto-continue\.js\?v=[^"'']+', '/static/qwen-auto-continue.js?v=qwen-suite-v3')
        } elseif (-not $Index.Contains('/static/qwen-auto-continue.js')) {
            $Index = $Index.Replace('</body>', '<script src="/static/qwen-auto-continue.js?v=qwen-suite-v3" defer></script></body>')
        }
        if ($Index -match '/static/qwen-video-studio\.js\?v=[^"'']+') {
            $Index = [regex]::Replace($Index, '/static/qwen-video-studio\.js\?v=[^"'']+', '/static/qwen-video-studio.js?v=qwen-suite-v5')
        } elseif (-not $Index.Contains('/static/qwen-video-studio.js')) {
            $Index = $Index.Replace('</body>', '<script src="/static/qwen-video-studio.js?v=qwen-suite-v5" defer></script></body>')
        }
        # Sign-in page: "forgot password" and "register" buttons; suite buttons only for admins.
        if ($Index -match '/static/qwen-account\.js\?v=[^"'']+') {
            $Index = [regex]::Replace($Index, '/static/qwen-account\.js\?v=[^"'']+', '/static/qwen-account.js?v=qwen-suite-v2')
        } elseif (-not $Index.Contains('/static/qwen-account.js')) {
            $Index = $Index.Replace('</body>', '<script src="/static/qwen-account.js?v=qwen-suite-v2" defer></script></body>')
        }
        [IO.File]::WriteAllText($IndexPath, $Index, [Text.UTF8Encoding]::new($false))
    }
} catch {
    Add-Content -LiteralPath (Join-Path $LogDir 'launcher-warnings.log') -Value "Theme update warning: $($_.Exception.Message)"
}

if (Test-LocalPort 3000) {
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'internet-gateway\start-internet-watchdog.ps1')
    } catch { }
    exit 0
}

    Start-Process -FilePath 'powershell.exe' `
    -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $Root 'internet-gateway\start-internet-watchdog.ps1')) `
    -WorkingDirectory (Join-Path $Root 'internet-gateway') `
    -WindowStyle Hidden | Out-Null

& $WebUI serve --host 127.0.0.1 --port 3000
