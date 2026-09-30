<#
Qwen Dictate: speech-to-text anywhere in Windows.

Press Ctrl+Alt+D (the "Qwen Dictate" Start Menu shortcut) to start recording the microphone,
press it again to stop. The recording is transcribed on this machine with faster-whisper
(ai-services\transcribe.py), the text is put on the clipboard and a toast says so; paste it
with Ctrl+V. Nothing leaves the machine. The audio lives in %TEMP% only until it has been
transcribed and is always deleted, also when something fails.

  dictate.ps1                    toggle (what the hotkey runs)
  dictate.ps1 -Mode status       idle / recording / finished
  dictate.ps1 -Mode devices      detect the microphone again and remember it
  dictate.ps1 -Device "<name>"   record from this DirectShow audio device and remember it

Settings in <suite>\data\dictate.json (written on first use, edit freely):
  device, device_id   the remembered microphone (from ffmpeg -list_devices)
  language            "" detects the language (like Telegram voice notes); "th" or "en" forces one
  max_seconds         recording stops by itself after this many seconds (default 300)

-StateDir, -ConfigPath, -FfmpegPath, -PythonPath, -TranscribeScript, -InputArgs, -OutputFile and
-NoToast are for tests: a test records a generated tone instead of the microphone and writes the
text to a file so the clipboard is left alone.
#>
[CmdletBinding()]
param(
    [ValidateSet('toggle', 'start', 'stop', 'status', 'devices')]
    [string]$Mode = 'toggle',
    [string]$Device = '',
    [string]$Language = '',
    [int]$MaxSeconds = 0,
    [string]$StateDir = '',
    [string]$ConfigPath = '',
    [string]$FfmpegPath = '',
    [string]$PythonPath = '',
    [string]$TranscribeScript = '',
    [string[]]$InputArgs = @(),
    [string]$OutputFile = '',
    [switch]$NoToast
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $StateDir) { $StateDir = [IO.Path]::GetTempPath() }
if (-not $ConfigPath) { $ConfigPath = Join-Path $Root 'data\dictate.json' }
if (-not $PythonPath) { $PythonPath = Join-Path $Root 'webui-venv\Scripts\python.exe' }
if (-not $TranscribeScript) { $TranscribeScript = Join-Path $PSScriptRoot 'transcribe.py' }
$StatePath = Join-Path $StateDir 'qwen-dictate.json'
$LanguageGiven = $PSBoundParameters.ContainsKey('Language')
$ToastAppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$Hotkey = 'Ctrl+Alt+D'
$SampleRate = 16000
$MinAudioBytes = $SampleRate    # half a second of 16-bit mono
$TranscribeTimeoutSeconds = 900
# Stereo Mix and virtual cables record what the speakers play, not the user's voice.
$LoopbackPattern = 'Stereo Mix|สเตอริโอ|What U Hear|Wave Out|Loopback|CABLE Output|VB-Audio|Voicemeeter|Virtual'
$MicrophonePattern = 'Microphone|\bMic\b|ไมโครโฟน|ไมค์|Headset|Array|Webcam|USB Audio'


function ConvertTo-CommandLine([string[]]$Arguments) {
    # Windows argv quoting, so device names and paths with spaces survive CreateProcess.
    ($Arguments | ForEach-Object {
        if ($_ -ne '' -and $_ -notmatch '[\s"]') { $_ }
        else { '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
    }) -join ' '
}

function Invoke-Captured([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = ConvertTo-CommandLine $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    # Thai text must reach us as UTF-8, not the console code page.
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process = [Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        [void]$process.WaitForExit(5000)
        throw "หมดเวลา ($TimeoutSeconds วินาที): $([IO.Path]::GetFileName($FilePath))"
    }
    $process.WaitForExit()
    [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout.Result; Stderr = $stderr.Result }
}

function Show-Toast([string]$Title, [string]$Body) {
    Write-Output "toast: $Title | $Body"
    if ($NoToast -or $env:QWEN_TOAST_DRYRUN -eq '1') { return }
    try {
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
        $heading = [Security.SecurityElement]::Escape($Title)
        $text = [Security.SecurityElement]::Escape($Body)
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$heading</text><text>$text</text></binding></visual></toast>")
        $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
        # Same tag: "transcribing" is replaced by the result instead of piling up.
        $toast.Tag = 'qwen-dictate'
        $toast.Group = 'qwen-dictate'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($ToastAppId).Show($toast)
    } catch {
        # The toast is only feedback; never fail dictation over it.
    }
}

function Find-Ffmpeg {
    $candidates = @(
        $FfmpegPath,
        $env:QWEN_FFMPEG,
        (Join-Path $Root 'runtime\ffmpeg\bin\ffmpeg.exe'),
        (Get-Command ffmpeg.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ffmpeg.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return [string]$candidate }
    }
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path -LiteralPath $packages) {
        $match = Get-ChildItem -LiteralPath $packages -Directory -Filter 'Gyan.FFmpeg*' -ErrorAction SilentlyContinue |
            ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Recurse -Filter 'ffmpeg.exe' -ErrorAction SilentlyContinue } |
            Where-Object { $_.Directory.Name -eq 'bin' } | Select-Object -First 1
        if ($match) { return $match.FullName }
    }
    throw 'ไม่พบ FFmpeg (runtime\ffmpeg หรือ PATH) ใช้ Repair Qwen Suite เพื่อติดตั้งใหม่'
}

function ConvertFrom-DshowList([string]$Text) {
    # ffmpeg 5+ prints '"Name" (audio)'; older builds print a "DirectShow audio devices" header.
    $devices = New-Object System.Collections.Generic.List[object]
    $section = ''
    $last = $null
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match 'DirectShow (video|audio) devices') { $section = $Matches[1]; continue }
        if ($line -match 'Alternative name\s+"([^"]+)"') {
            if ($last) { $last.Id = $Matches[1] }
            continue
        }
        if ($line -match '\]\s+"([^"]+)"(?:\s+\((audio|video|none)\))?\s*$') {
            $kind = if ($Matches[2]) { $Matches[2] } else { $section }
            $last = [pscustomobject]@{ Name = $Matches[1]; Kind = $kind; Id = '' }
            $devices.Add($last)
        }
    }
    , $devices.ToArray()
}

function Select-Microphone([object[]]$Devices) {
    $audio = @($Devices | Where-Object { $_.Kind -eq 'audio' -and $_.Name -notmatch $LoopbackPattern })
    if (-not $audio) { return $null }
    $preferred = $audio | Where-Object { $_.Name -match $MicrophonePattern } | Select-Object -First 1
    if ($preferred) { return $preferred }
    $audio[0]
}

function Read-Config {
    $config = [ordered]@{ device = ''; device_id = ''; language = ''; max_seconds = 300 }
    if (Test-Path -LiteralPath $ConfigPath) {
        try {
            $saved = [IO.File]::ReadAllText($ConfigPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
            foreach ($property in $saved.PSObject.Properties) { $config[$property.Name] = $property.Value }
        } catch {
            # A damaged file is rewritten on the next detection.
        }
    }
    $config
}

function Save-Config($Config) {
    $folder = Split-Path -Parent $ConfigPath
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
    $temporary = "$ConfigPath.tmp"
    [IO.File]::WriteAllText($temporary, ([pscustomobject]$Config | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
    Move-Item -LiteralPath $temporary -Destination $ConfigPath -Force
}

function Update-Microphone($Config, [string]$Ffmpeg) {
    $listing = Invoke-Captured $Ffmpeg @('-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy') 30
    $pick = Select-Microphone (ConvertFrom-DshowList $listing.Stderr)
    if (-not $pick) { throw 'ไม่พบไมโครโฟน ตรวจว่าเสียบไมค์แล้ว และเปิดสิทธิ์ไมโครโฟนให้แอปเดสก์ท็อปใน Settings > Privacy > Microphone' }
    $Config.device = $pick.Name
    $Config.device_id = $pick.Id
    $Config.detected_at = (Get-Date).ToString('s')
    Save-Config $Config
}

function Get-MicrophoneInput($Config) {
    # The alternative name (@device_cm_...) is ASCII and stable, so it survives Thai device names.
    $name = if ($Config.device_id) { $Config.device_id } else { $Config.device }
    @('-f', 'dshow', '-audio_buffer_size', '50', '-i', "audio=$name")
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) { return $null }
    try { [IO.File]::ReadAllText($StatePath, [Text.Encoding]::UTF8) | ConvertFrom-Json } catch { $null }
}

function Get-Recorder($State) {
    if (-not $State -or -not $State.pid) { return $null }
    $process = Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    # PIDs are reused: only the process with the recorded name and start time is ours to stop.
    if ($process.ProcessName -ne [IO.Path]::GetFileNameWithoutExtension([string]$State.recorder)) { return $null }
    try { $ticks = [string]$process.StartTime.ToUniversalTime().Ticks } catch { return $null }
    if ($ticks -ne [string]$State.started_ticks) { return $null }
    $process
}

function Remove-DictationFiles($State) {
    if ($State) {
        foreach ($path in @($State.raw, $State.wav, $State.log)) {
            if ($path) { Remove-Item -LiteralPath ([string]$path) -Force -ErrorAction SilentlyContinue }
        }
    }
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
}

function Remove-Leftovers {
    # Audio from a crashed or abandoned session must not stay on disk.
    Get-ChildItem -LiteralPath $StateDir -File -Filter 'qwen-dictate-*' -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in '.pcm', '.wav', '.log' } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Write-Wav([string]$RawPath, [string]$WavPath) {
    $data = [IO.File]::ReadAllBytes($RawPath)
    $length = $data.Length - ($data.Length % 2)
    $ascii = [Text.Encoding]::ASCII
    $stream = [IO.File]::Create($WavPath)
    try {
        $writer = New-Object IO.BinaryWriter $stream
        $writer.Write($ascii.GetBytes('RIFF')); $writer.Write([int](36 + $length)); $writer.Write($ascii.GetBytes('WAVE'))
        $writer.Write($ascii.GetBytes('fmt ')); $writer.Write([int]16); $writer.Write([int16]1); $writer.Write([int16]1)
        $writer.Write([int]$SampleRate); $writer.Write([int]($SampleRate * 2)); $writer.Write([int16]2); $writer.Write([int16]16)
        $writer.Write($ascii.GetBytes('data')); $writer.Write([int]$length)
        $writer.Write($data, 0, $length)
        $writer.Flush()
    } finally {
        $stream.Dispose()
    }
}

function Invoke-Transcriber([string]$WavPath, [string]$Language) {
    if (-not (Test-Path -LiteralPath $PythonPath)) { throw "ไม่พบ Python ของ Qwen Suite: $PythonPath" }
    $arguments = @($TranscribeScript, $WavPath)
    if ($Language) { $arguments += @('--language', $Language) }
    $result = Invoke-Captured $PythonPath $arguments $TranscribeTimeoutSeconds
    if ($result.ExitCode -ne 0) {
        $reason = (($result.Stderr -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
        throw "ถอดเสียงไม่สำเร็จ: $reason"
    }
    $result.Stdout.Trim()
}

function Start-Recording {
    Remove-Leftovers
    $config = Read-Config
    $limit = if ($MaxSeconds -gt 0) { $MaxSeconds } else { [int]$config.max_seconds }
    $limit = [Math]::Max(5, [Math]::Min(1800, $limit))
    $ffmpeg = Find-Ffmpeg
    if ($Device) {
        $config.device = $Device
        $config.device_id = ''
        Save-Config $config
    } elseif (-not $InputArgs -and -not $config.device_id -and -not $config.device) {
        Update-Microphone $config $ffmpeg
    }
    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $raw = Join-Path $StateDir "qwen-dictate-$stamp.pcm"
    $log = Join-Path $StateDir "qwen-dictate-$stamp.log"
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $source = if ($InputArgs) { $InputArgs } else { Get-MicrophoneInput $config }
        # Raw PCM has no header to finalise, so stopping ffmpeg abruptly loses nothing but the last packet.
        $arguments = @('-hide_banner', '-nostdin', '-loglevel', 'error', '-y') + $source +
            @('-t', [string]$limit, '-ac', '1', '-ar', [string]$SampleRate, '-f', 's16le', '-flush_packets', '1', $raw)
        # Started through ShellExecute (no redirection) so ffmpeg does not inherit this script's
        # stdout/stderr: a caller reading our output would otherwise wait until the recording ends.
        # Its errors go to a report file instead (FFREPORT; ':' and '\' need quoting there).
        $env:FFREPORT = "file='" + ($log.Replace('\', '/').Replace('%', '%%')) + "':level=16"
        try {
            $process = Start-Process -FilePath $ffmpeg -ArgumentList (ConvertTo-CommandLine $arguments) -WindowStyle Hidden -PassThru
        } finally {
            Remove-Item -LiteralPath Env:FFREPORT -ErrorAction SilentlyContinue
        }
        $state = [ordered]@{
            pid = $process.Id
            started_ticks = [string]$process.StartTime.ToUniversalTime().Ticks
            recorder = $ffmpeg
            raw = $raw
            wav = [IO.Path]::ChangeExtension($raw, '.wav')
            log = $log
            started_at = (Get-Date).ToString('o')
            max_seconds = $limit
        }
        # Written before anything else can fail, so the next press can always find and stop it.
        [IO.File]::WriteAllText($StatePath, ([pscustomobject]$state | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
        Start-Sleep -Milliseconds 700
        if (-not $process.HasExited) {
            Show-Toast 'Qwen ถอดเสียง: กำลังฟัง' "พูดได้เลย กด $Hotkey อีกครั้งเพื่อหยุดและถอดเสียง (สูงสุด $([Math]::Ceiling($limit / 60)) นาที)"
            return 'recording'
        }
        $reason = ''
        try { $reason = ([IO.File]::ReadAllText($log) -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1) } catch { }
        Remove-DictationFiles ([pscustomobject]$state)
        # The remembered microphone may have been unplugged: detect once more, then give up.
        if ($attempt -eq 2 -or $InputArgs -or $Device) { throw "เปิดไมโครโฟนไม่ได้: $reason" }
        Update-Microphone $config $ffmpeg
    }
}

function Stop-Recording($State) {
    $recorder = Get-Recorder $State
    if ($recorder) {
        Start-Sleep -Milliseconds 400    # keep the last syllable
        Stop-Process -Id $recorder.Id -Force -ErrorAction SilentlyContinue
        [void]$recorder.WaitForExit(5000)
    }
    try {
        $raw = [string]$State.raw
        $bytes = if ($raw -and (Test-Path -LiteralPath $raw)) { (Get-Item -LiteralPath $raw).Length } else { 0 }
        if ($bytes -lt $MinAudioBytes) {
            Show-Toast 'Qwen ถอดเสียง: ไม่ได้ยินเสียง' "ไม่มีเสียงที่บันทึกไว้ ลองกด $Hotkey แล้วพูดใหม่"
            return 'no-audio'
        }
        $seconds = [Math]::Round($bytes / ($SampleRate * 2), 1)
        Show-Toast 'Qwen ถอดเสียง: กำลังถอดเสียง' "เสียง $seconds วินาที รอสักครู่"
        $wav = [string]$State.wav
        Write-Wav $raw $wav
        Remove-Item -LiteralPath $raw -Force -ErrorAction SilentlyContinue
        $config = Read-Config
        $language = if ($LanguageGiven) { $Language } else { [string]$config.language }
        $text = Invoke-Transcriber $wav $language
        if (-not $text) {
            Show-Toast 'Qwen ถอดเสียง: ไม่พบคำพูด' 'ไม่ได้ยินคำพูดในเสียงที่บันทึก คลิปบอร์ดไม่ถูกเปลี่ยน'
            return 'empty'
        }
        if ($OutputFile) {
            [IO.File]::WriteAllText($OutputFile, $text, (New-Object Text.UTF8Encoding $false))
        } else {
            Set-Clipboard -Value $text
        }
        $preview = if ($text.Length -gt 120) { $text.Substring(0, 120) + '…' } else { $text }
        Show-Toast 'ถอดเสียงแล้ว — กด Ctrl+V เพื่อวาง' $preview
        return "transcribed: $($text.Length) chars"
    } finally {
        Remove-DictationFiles $State
    }
}

function Test-Fresh($State) {
    try {
        $started = [datetime]::Parse([string]$State.started_at, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
        $age = ((Get-Date) - $started).TotalSeconds
        return $age -lt ([int]$State.max_seconds + 600)
    } catch {
        return $false
    }
}

function Invoke-Dictate {
    $state = Read-State
    $recorder = Get-Recorder $state
    # ffmpeg stops on its own at max_seconds; a recent finished recording is still transcribed.
    $finished = (-not $recorder) -and $state -and $state.raw -and (Test-Path -LiteralPath ([string]$state.raw)) -and (Test-Fresh $state)
    switch ($Mode) {
        'status' {
            if ($recorder) { 'recording' } elseif ($finished) { 'finished' } else { 'idle' }
        }
        'devices' {
            if ($recorder) { throw 'กำลังบันทึกเสียงอยู่ กดหยุดก่อนแล้วค่อยตรวจไมโครโฟนใหม่' }
            $config = Read-Config
            Update-Microphone $config (Find-Ffmpeg)
            Show-Toast 'Qwen ถอดเสียง: ตั้งไมโครโฟนแล้ว' ([string]$config.device)
            'devices: saved'
        }
        'start' {
            if ($recorder) { 'already-recording' } else { Remove-DictationFiles $state; Start-Recording }
        }
        'stop' {
            if ($recorder -or $finished) { Stop-Recording $state }
            else {
                Remove-DictationFiles $state
                Show-Toast 'Qwen ถอดเสียง' "ยังไม่ได้เริ่มบันทึก กด $Hotkey เพื่อเริ่ม"
                'idle'
            }
        }
        default {
            if ($recorder -or $finished) { Stop-Recording $state }
            else { Remove-DictationFiles $state; Start-Recording }
        }
    }
}

function Invoke-Main {
    $hash = [BitConverter]::ToString((New-Object Security.Cryptography.SHA1Managed).ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($StateDir.TrimEnd('\').ToLowerInvariant()))).Replace('-', '').Substring(0, 12)
    $mutex = New-Object Threading.Mutex($false, "Local\QwenDictate-$hash")
    $owned = $false
    try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        # A press while the previous clip is still being transcribed.
        Show-Toast 'Qwen ถอดเสียง: กำลังทำงานอยู่' 'รอให้ถอดเสียงครั้งก่อนเสร็จก่อน แล้วค่อยกดอีกครั้ง'
        'busy'
        $mutex.Dispose()
        return 0
    }
    try {
        Invoke-Dictate
        return 0
    } catch {
        Show-Toast 'Qwen ถอดเสียง: ไม่สำเร็จ' $_.Exception.Message
        "error: $($_.Exception.Message)"
        return 1
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}

# Dot-sourcing (tests) loads the functions without recording anything.
if ($MyInvocation.InvocationName -ne '.') {
    $exitCode = 0
    Invoke-Main | ForEach-Object { if ($_ -is [int]) { $exitCode = $_ } else { $_ } }
    exit $exitCode
}
