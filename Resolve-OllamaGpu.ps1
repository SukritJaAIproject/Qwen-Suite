# Decide which GPUs the Ollama server may use, immediately before it starts.
#
# Dot-sourced by start-ollama-server.ps1 and Start-Ollama.ps1 inside the
# Local\QwenSuite-Ollama mutex, so every way of starting the server gets the same
# answer. Added 2026-09-29 for the Thunderbolt eGPUs (RTX 4090 24 GB, RTX 4060 Ti 8 GB).
#
# Why this cannot be a static User environment variable:
#  - The eGPU comes and goes (one Thunderbolt port, two cards to swap), and Ollama only
#    looks for GPUs when the server starts. The choice has to be made per start.
#  - The internal Quadro P620 (Pascal, 4 GB) must stay out whenever an eGPU is there:
#    next to an RTX card it would pull every card onto the older cuda_v12 group, take
#    layers of any model that gets split, and its 4 GB would host small models.
#  - With a driver that still supports it (R580, "path A") it helps when there is no
#    eGPU. Measured 2026-09-29 on qwen3.8:27b with a 1,090-token prompt: P620 visible =
#    prompt 21.2 tok/s, answer 1.66 tok/s; CPU-only (num_gpu 0) = prompt 4.9-6.1 tok/s,
#    answer 1.9 tok/s. It gets 0 of 66 layers, but llama.cpp uses it for the prompt
#    batches, and this stack's prompts run to 20k tokens (OpenClaw), so reading the
#    prompt is most of the wait. The older note that "CPU-only is faster" measured answer
#    speed only.
#  - "Path B", chosen 2026-09-29: the latest GeForce driver for the RTX cards and the P620
#    disabled in Device Manager (R580 was the last branch for Pascal, and every NVIDIA
#    device shares one kernel driver). Then no eGPU means CPU only. A card disabled on
#    purpose (code 22) is recorded as disabled, not as a problem.
#  - CUDA_VISIBLE_DEVICES alone does not hide a card: Ollama 0.32 has Vulkan on by
#    default and would pick the same card up again as a Vulkan device.
#
# What "auto" (the default) sets - process scope only, nothing is written to the registry:
#  - an NVIDIA GPU usable for inference (compute capability >= 7.0, >= 6 GiB, driver
#    >= 550): CUDA_VISIBLE_DEVICES=<UUIDs, biggest first>  OLLAMA_LLM_LIBRARY=cuda_v12
#    OLLAMA_VULKAN=0. cuda_v12 carries native code for Volta..Blackwell (sm_70..sm_120);
#    cuda_v13 ships PTX only on Windows, so every first load would compile kernels.
#  - no such GPU: $QwenGpuNoEgpuProfile - 'internal' leaves Ollama's own discovery alone
#    (the P620 on path A; on path B no NVIDIA card works, the record says cpu_only and the
#    note says why); 'cpu' would hide every GPU.
#  - nvidia-smi failed or listed fewer cards than Windows shows working: asked again after
#    5 s; if it still fails, inventory_ok=false and the watcher tries once more when the
#    server is idle. With two or more working NVIDIA cards this start is 'cpu', so the
#    P620 is never mixed with an eGPU it could not see; with one, Ollama's own discovery
#    takes that card for this start.
#
# Override with the User environment variable QWEN_GPU_MODE:
#    auto (default) | cpu | all (Ollama's own discovery, P620 included - the behaviour
#    before 2026-09-29) | vulkan (only if CUDA cannot see the card, and only with the P620
#    disabled: Vulkan would otherwise use both).
# An OLLAMA_LLM_LIBRARY or CUDA_VISIBLE_DEVICES the user set (User or Machine scope) always
# wins: mode "manual" applies exactly the configured values and decides nothing. In the
# other modes the variables "auto" does not decide (CUDA_DEVICE_ORDER,
# OLLAMA_SCHED_SPREAD, and in 'internal'/'all' all of them) keep their configured values;
# values merely inherited from whichever process started the launcher (for example the
# watcher) are never passed on.
#
# The decision is written to data\gpu-profile.json (read by the model router, the video
# worker and the watcher) and appended to logs\ollama-gpu-profile.log. vram_class starts
# as "pending" and the watcher promotes it to large/small only after the server log shows
# the card was really found, so the router never acts on a card Ollama could not use.
# Nothing here may throw: if the decision fails, the server starts with Ollama's defaults.

$script:QwenGpuEnvNames = @(
    'CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'OLLAMA_VULKAN',
    'GGML_VK_VISIBLE_DEVICES', 'OLLAMA_SCHED_SPREAD', 'OLLAMA_LLM_LIBRARY'
)
# What "auto" does when no eGPU is attached (see above). QWEN_GPU_MODE=cpu gives CPU-only
# without editing this file.
$script:QwenGpuNoEgpuProfile = 'internal'
$script:QwenGpuMinComputeCap = 7.0
$script:QwenGpuMinMemoryMiB = 6144
$script:QwenGpuLargeMemoryMiB = 20480
$script:QwenGpuMinDriverMajor = 550
$script:QwenGpuWatchMutexName = 'Local\QwenSuite-OllamaGpuWatch'
# Bumped whenever watch-ollama-gpu.ps1 learns something new; an older watcher that is still
# running is replaced by Start-QwenGpuWatch (it records itself in data\gpu-watch.json).
$script:QwenGpuWatchVersion = 6
# Set by Resolve-QwenGpuProfile: did THIS start write data\gpu-profile.json? The launchers
# only stamp the server pid into the record when it did, so a stale record never looks
# current to the watcher.
$script:QwenGpuRecordWritten = $false

function Write-QwenGpuLog {
    param([string]$Root, [string]$Message)
    try {
        $dir = Join-Path $Root 'logs'
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null }
        $path = Join-Path $dir 'ollama-gpu-profile.log'
        if ((Test-Path -LiteralPath $path) -and (Get-Item -LiteralPath $path).Length -gt 2MB) {
            Move-Item -LiteralPath $path -Destination "$path.1" -Force -ErrorAction Stop
        }
        $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $PID, $Message
        Add-Content -LiteralPath $path -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch { }
}

function Get-QwenConfiguredValue {
    # What the user configured for a variable (User, else Machine scope) - as opposed to
    # what the current process happened to inherit.
    param([string]$Name)
    $value = [Environment]::GetEnvironmentVariable($Name, 'User')
    if ($null -eq $value -or $value -eq '') { $value = [Environment]::GetEnvironmentVariable($Name, 'Machine') }
    if ($value -eq '') { $value = $null }
    return $value
}

function Invoke-QwenNvidiaSmi {
    # nvidia-smi can hang while a Thunderbolt card is half initialised, and under
    # $ErrorActionPreference='Stop' a native command writing to stderr would abort the
    # caller. Run it as a plain process with a timeout instead.
    param([string]$Arguments, [int]$TimeoutMs = 15000)
    # DCH drivers put it in System32; older "Standard" drivers only in NVSMI.
    $smi = @(
        (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'),
        (Join-Path ${env:ProgramFiles} 'NVIDIA Corporation\NVSMI\nvidia-smi.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $smi) {
        $cmd = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if (-not $cmd) { return $null }
        $smi = $cmd.Source
    }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $smi
        $psi.Arguments = $Arguments
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $stdout = $proc.StandardOutput.ReadToEndAsync()
        $stderr = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit($TimeoutMs)) {
            try { $proc.Kill() } catch { }
            return [pscustomobject]@{ ExitCode = -1; Output = ''; Error = 'timed out'; Path = $smi }
        }
        $proc.WaitForExit()
        return [pscustomobject]@{ ExitCode = $proc.ExitCode; Output = $stdout.Result; Error = $stderr.Result; Path = $smi }
    } catch {
        return [pscustomobject]@{ ExitCode = -2; Output = ''; Error = $_.Exception.Message; Path = $smi }
    }
}

function Get-QwenNvidiaPnpDevices {
    # The PnP view of every NVIDIA display device, including an eGPU without an NVIDIA
    # driver (Microsoft Basic Display). Cheap and does not wake the GPU. Returns $null -
    # not an empty list - when the query itself failed, so a WMI hiccup is never read as
    # "all NVIDIA devices are gone".
    try {
        $devices = @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Display'" -OperationTimeoutSec 10 -ErrorAction Stop |
            Where-Object { $_.PNPDeviceID -like 'PCI\VEN_10DE*' } |
            ForEach-Object {
                [pscustomobject]@{
                    Name      = [string]$_.Name
                    PnpId     = [string]$_.PNPDeviceID
                    Service   = [string]$_.Service
                    ErrorCode = [int]$_.ConfigManagerErrorCode
                }
            })
        return ,$devices
    } catch {
        return $null
    }
}

function Get-QwenNvidiaPnpSignature {
    # One string for "which NVIDIA devices are present, with which driver, in which state".
    # $null when the PnP query failed.
    param($Devices)
    if (-not $PSBoundParameters.ContainsKey('Devices')) { $Devices = Get-QwenNvidiaPnpDevices }
    if ($null -eq $Devices) { return $null }
    # A card disabled in Device Manager counts only as "disabled": its service name can
    # change when driver packages are replaced (path B), and that alone must not restart
    # Ollama.
    $parts = @($Devices | ForEach-Object {
        if ($_.ErrorCode -eq 22) { '{0}|disabled|22' -f $_.PnpId } else { '{0}|{1}|{2}' -f $_.PnpId, $_.Service, $_.ErrorCode }
    } | Sort-Object)
    if ($parts.Count -eq 0) { return 'none' }
    return ($parts -join ';')
}

function Test-QwenServerDeviceLost {
    # Did a device the running server can be using disappear or stop working? Then the
    # server is broken already, and a restart must not wait for idle. Only a device that
    # WORKED when the server started counts (a disabled or driverless card changing state
    # was never in use - it must never cut off an answer). Then, by profile:
    #  gpu:          the cards it was given (by PnP id); if any of them has no known id,
    #                any such device
    #  vulkan:       any such device - Vulkan uses every NVIDIA card it can see (a working
    #                P620 too), and a GGML_VK_VISIBLE_DEVICES index cannot be matched to one
    #  internal/all: any such device - Ollama's own discovery may be using any of them
    #  manual:       any such device when some active library is not narrowed to a list
    #                (CUDA unless OLLAMA_LLM_LIBRARY picks another or CUDA_VISIBLE_DEVICES
    #                lists cards; Vulkan unless turned off or GGML_VK_VISIBLE_DEVICES lists
    #                cards); a list cannot be matched to devices here, so fully narrowed
    #                settings - or the cpu library - are not urgent
    #  cpu:          never
    param($Record, [string]$Signature)
    if (-not $Record -or -not $Signature -or [string]$Record.pnp_signature -eq 'unknown') { return $false }
    $before = @(([string]$Record.pnp_signature) -split ';' | Where-Object { $_ -and $_ -ne 'none' })
    $after = @($Signature -split ';' | Where-Object { $_ -and $_ -ne 'none' })
    $lost = @($before | Where-Object { $after -notcontains $_ } |
        Where-Object { $f = @($_ -split '\|'); $f.Count -ge 3 -and $f[1] -ieq 'nvlddmkm' -and $f[2] -eq '0' } |
        ForEach-Object { ($_ -split '\|')[0] })
    if ($lost.Count -eq 0) { return $false }
    switch ([string]$Record.profile) {
        'gpu' {
            $used = @($Record.gpus_used | Where-Object { $_ })
            if ($used.Count -eq 0 -or @($used | Where-Object { -not $_.pnp_id }).Count -gt 0) { return $true }
            $usedIds = @($used | ForEach-Object { [string]$_.pnp_id })
            return (@($lost | Where-Object { $usedIds -contains $_ }).Count -gt 0)
        }
        'vulkan' { return $true }
        'internal' { return $true }
        'all' { return $true }
        'manual' {
            $lib = [string]$Record.env.OLLAMA_LLM_LIBRARY
            $cvd = [string]$Record.env.CUDA_VISIBLE_DEVICES
            $vk = [string]$Record.env.OLLAMA_VULKAN
            if ($lib -ieq 'cpu') { return $false }
            $cudaActive = (-not $lib -or $lib -like 'cuda*') -and $cvd -ne '-1'
            $vulkanActive = ($lib -like 'vulkan*') -or (-not $lib -and $vk -notin @('0', 'false', 'False'))
            $cudaOpen = $cudaActive -and -not $cvd
            $vulkanOpen = $vulkanActive -and -not [string]$Record.env.GGML_VK_VISIBLE_DEVICES
            return ($cudaOpen -or $vulkanOpen)
        }
    }
    return $false
}

function ConvertFrom-QwenSmiCsv {
    param([string]$Output)
    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    $gpus = @()
    foreach ($line in ($Output -split "`r?`n")) {
        if (-not $line.Trim()) { continue }
        $f = @($line -split ',' | ForEach-Object { $_.Trim() })
        if ($f.Count -lt 7 -or $f[1] -notlike 'GPU-*') { continue }
        $memory = 0; [void][int]::TryParse($f[3], [ref]$memory)
        $cc = 0.0; [void][double]::TryParse($f[4], [System.Globalization.NumberStyles]::Float, $culture, [ref]$cc)
        $gpus += [pscustomobject]@{
            Index = $f[0]; Uuid = $f[1]; Name = $f[2]; MemoryMiB = $memory
            ComputeCap = $cc; PciBus = $f[5]; Driver = $f[6]
        }
    }
    return ,$gpus
}

function Get-QwenGpuInventory {
    $result = [ordered]@{ SmiPath = $null; SmiExitCode = $null; SmiError = $null; Gpus = @() }
    $query = Invoke-QwenNvidiaSmi -Arguments '--query-gpu=index,uuid,name,memory.total,compute_cap,pci.bus_id,driver_version --format=csv,noheader,nounits'
    if ($null -eq $query) { $result.SmiError = 'nvidia-smi not found'; return [pscustomobject]$result }
    $result.SmiPath = $query.Path
    $result.SmiExitCode = $query.ExitCode
    # Keep whatever complete lines it printed even when it exits non-zero.
    $result.Gpus = ConvertFrom-QwenSmiCsv -Output ([string]$query.Output)
    if ($query.ExitCode -ne 0) { $result.SmiError = ([string]$query.Error + ' ' + [string]$query.Output).Trim() }
    return [pscustomobject]$result
}

function Get-QwenGpuExternalFlags {
    # For each PCI bus number of a working NVIDIA device: is it removable (a Thunderbolt
    # eGPU) or built in? The internal P620 here reports RemovalPolicy 1 (no removal) and
    # InLocalMachineContainer True; a card behind a hot-plug port is expected to report
    # removable (2/3) or a separate container. Unknown -> not in the table.
    param($Devices)
    $flags = @{}
    foreach ($d in @($Devices)) {
        if (-not $d) { continue }
        try {
            $props = @(Get-PnpDeviceProperty -InstanceId $d.PnpId -KeyName 'DEVPKEY_Device_BusNumber', 'DEVPKEY_Device_RemovalPolicy', 'DEVPKEY_Device_InLocalMachineContainer' -ErrorAction Stop)
            $bus = ($props | Where-Object KeyName -eq 'DEVPKEY_Device_BusNumber').Data
            $policy = ($props | Where-Object KeyName -eq 'DEVPKEY_Device_RemovalPolicy').Data
            $local = ($props | Where-Object KeyName -eq 'DEVPKEY_Device_InLocalMachineContainer').Data
            if ($null -eq $bus) { continue }
            $flags[[int]$bus] = [pscustomobject]@{ External = (([int]$policy -ge 2) -or ($local -eq $false)); PnpId = [string]$d.PnpId }
        } catch { }
    }
    return $flags
}

function ConvertTo-QwenNvidiaVersion([string]$WindowsVersion) {
    # Windows driver version -> NVIDIA's: 32.0.15.8195 -> 581.95, 32.0.16.1714 -> 617.14
    # (the last digit of the third field, then the fourth field).
    if ($WindowsVersion -notmatch '(\d+)\.(\d+)\.(\d+)\.(\d+)') { return $null }
    $n = ([int]$Matches[3] % 10) * 10000 + [int]$Matches[4]
    return ('{0}.{1:D2}' -f [math]::Floor($n / 100), ($n % 100))
}

function Get-QwenNvidiaKernelDriver {
    # The ONE kernel driver every NVIDIA device runs on (the nvlddmkm service's file), as an
    # NVIDIA version. Major 590 and later = no Pascal: path B, the P620 cannot run.
    $r = [pscustomobject]@{ Image = $null; Package = $null; WindowsVersion = $null; Version = $null; Major = 0 }
    try {
        $image = [string](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\nvlddmkm' -ErrorAction Stop).ImagePath
        if (-not $image) { return $r }
        $r.Image = $image
        $r.Package = ($image -split '\\') | Where-Object { $_ -like 'nv*.inf_amd64_*' } | Select-Object -First 1
        $file = if ($image -match '^\\SystemRoot\\(.*)$') { Join-Path $env:SystemRoot $Matches[1] }
                elseif ($image -match '^\\\?\?\\(.*)$') { $Matches[1] }
                elseif ($image -match '^[A-Za-z]:\\') { $image }
                else { Join-Path $env:SystemRoot $image }
        $r.WindowsVersion = [string][System.Diagnostics.FileVersionInfo]::GetVersionInfo($file).FileVersion
        $r.Version = ConvertTo-QwenNvidiaVersion $r.WindowsVersion
        if ($r.Version) { $r.Major = [int](($r.Version -split '\.')[0]) }
    } catch { }
    return $r
}

function Get-QwenPciDeviceId([string]$PnpId) {
    # 'PCI\VEN_10DE&DEV_1CBD&SUBSYS_...' -> '1CBD'
    if ($PnpId -match 'DEV_([0-9A-Fa-f]{4})') { return $Matches[1].ToUpperInvariant() }
    return $null
}

function Read-QwenSeenDevices {
    # Cards nvidia-smi has listed before (Select-QwenCompute.ps1 keeps
    # data\compute-devices-seen.json): uuid -> name, memory_mib, pci_dev, last_seen.
    param([string]$Root)
    $seen = [ordered]@{}
    try {
        $path = Join-Path $Root 'data\compute-devices-seen.json'
        if (Test-Path -LiteralPath $path) {
            foreach ($p in (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties) { $seen[$p.Name] = $p.Value }
        }
    } catch { }
    return $seen
}

function Test-QwenSameCard {
    # Is this Windows device the card known by this name (and PCI device id, when one was
    # recorded)? The name alone fails once Windows renames a card without a driver
    # "Microsoft Basic Display Adapter"; its PCI device id stays. Cards remembered before
    # ids were recorded: the Quadro P620 is DEV_1CBD.
    param([string]$Name, [string]$PciDev, $Device)
    if (-not $Device) { return $false }
    $dev = Get-QwenPciDeviceId ([string]$Device.PnpId)
    if ($PciDev -and $dev) { return ($PciDev -ieq $dev) }
    $n = [string]$Device.Name
    if ($Name -and $n -and ($n -like "*$Name*" -or $Name -like "*$n*")) { return $true }
    return ($Name -like '*P620*' -and $dev -eq '1CBD')
}

function Get-QwenComputeSelectionPath {
    param([string]$Root)
    return (Join-Path (Join-Path $Root 'data') 'compute-selection.json')
}

function Read-QwenComputeSelection {
    # The user's explicit choice of processing unit (Select-QwenCompute.ps1, the web page
    # on :8765, or the compute_device chat tool). $null = none made = automatic.
    param([string]$Root)
    try {
        $path = Get-QwenComputeSelectionPath -Root $Root
        if (-not (Test-Path -LiteralPath $path)) { return $null }
        $sel = Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        if (-not $sel -or -not $sel.device) { return $null }
        return $sel
    } catch { return $null }
}

function Test-QwenSelectionExpired {
    param($Selection)
    if (-not $Selection -or -not $Selection.expires_at) { return $false }
    try { return ([datetime]::Parse([string]$Selection.expires_at) -lt (Get-Date)) } catch { return $false }
}

function Get-QwenSelectionKey {
    # What the choice means, independent of when it was saved: 'auto', 'cpu' or
    # 'gpu:<uuid,...>'. The watcher restarts Ollama only when this differs from the key the
    # running server was started with, so saving the same choice again changes nothing.
    param($Selection)
    if (-not $Selection -or (Test-QwenSelectionExpired $Selection)) { return 'auto' }
    switch ([string]$Selection.device) {
        'cpu' { return 'cpu' }
        'gpu' {
            $ids = @($Selection.gpus | Where-Object { $_ } | ForEach-Object { [string]$_ } | Sort-Object)
            if ($ids.Count -gt 0) { return 'gpu:' + ($ids -join ',') }
        }
    }
    return 'auto'
}

function Save-QwenComputeSelection {
    # device: auto | cpu | gpu (with GPU UUIDs). -Hours > 0 makes the choice fall back to
    # auto after that long (used for choices made from chat). Serialised with a named mutex
    # because the menu, the web page and the chat tool can all write it. Returns the saved
    # record with .changed = $false when the choice was already in place (nothing written).
    param([string]$Root, [string]$Device, [string[]]$Gpus = @(), [string[]]$Labels = @(), [double]$Hours = 0, [string]$By = '')
    $Device = $Device.ToLowerInvariant()
    if ($Device -notin @('auto', 'cpu', 'gpu')) { throw "device must be auto, cpu or gpu (got '$Device')" }
    if ($Device -eq 'gpu' -and @($Gpus).Count -eq 0) { throw 'device gpu needs at least one GPU UUID' }
    $lock = New-Object System.Threading.Mutex($false, 'Local\QwenSuite-ComputeSelection')
    $owns = $false
    try {
        try { $owns = $lock.WaitOne(15000) } catch [System.Threading.AbandonedMutexException] { $owns = $true }
        if (-not $owns) { throw 'another change of the processing unit is being saved; try again' }
        $old = Read-QwenComputeSelection -Root $Root
        $record = [ordered]@{
            version    = 2
            device     = $Device
            gpus       = @($Gpus | Where-Object { $_ })
            labels     = @($Labels | Where-Object { $_ })
            expires_at = if ($Hours -gt 0) { (Get-Date).AddHours($Hours).ToString('o') } else { $null }
            updated_at = (Get-Date).ToString('o')
            updated_by = $By
        }
        $newKey = Get-QwenSelectionKey ([pscustomobject]$record)
        if ($old -and (Get-QwenSelectionKey $old) -eq $newKey -and -not $old.expires_at -and -not $record.expires_at) {
            $old | Add-Member -NotePropertyName changed -NotePropertyValue $false -Force
            return $old
        }
        $path = Get-QwenComputeSelectionPath -Root $Root
        $dir = Split-Path -Parent $path
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        # Never delete the user's choice because one write failed.
        if (-not (Write-QwenGpuProfileText -Path $path -Text (ConvertTo-Json -InputObject $record -Depth 5) -KeepOnFailure)) { throw "could not write $path" }
        Write-QwenGpuLog -Root $Root -Message ("selection: {0} by={1}{2}" -f $newKey, $By, $(if ($record.expires_at) { " until $($record.expires_at)" } else { '' }))
        $out = [pscustomobject]$record
        $out | Add-Member -NotePropertyName changed -NotePropertyValue $true -Force
        return $out
    } finally {
        if ($owns) { try { $lock.ReleaseMutex() } catch { } }
        $lock.Dispose()
    }
}

function Get-QwenGpuProfilePath {
    param([string]$Root)
    return (Join-Path (Join-Path $Root 'data') 'gpu-profile.json')
}

function Read-QwenGpuProfile {
    param([string]$Root)
    try {
        $path = Get-QwenGpuProfilePath -Root $Root
        if (-not (Test-Path -LiteralPath $path)) { return $null }
        return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json)
    } catch { return $null }
}

function Write-QwenGpuProfileText {
    # Atomic replace with a short retry: a reader (router, video worker) may have the file
    # open for a moment. On final failure the old record is deleted rather than left
    # looking current. Returns $true only when the new text is in place.
    param([string]$Path, [string]$Text, [switch]$KeepOnFailure)
    $tmp = "$Path.tmp"
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            [System.IO.File]::WriteAllText($tmp, $Text, (New-Object System.Text.UTF8Encoding($false)))
            Move-Item -LiteralPath $tmp -Destination $Path -Force -ErrorAction Stop
            return $true
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    try { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } catch { }
    if (-not $KeepOnFailure) { try { Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue } catch { } }
    return $false
}

function Save-QwenGpuProfile {
    param([string]$Root, $Record)
    try {
        $path = Get-QwenGpuProfilePath -Root $Root
        $dir = Split-Path -Parent $path
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null }
        return (Write-QwenGpuProfileText -Path $path -Text (ConvertTo-Json -InputObject $Record -Depth 6))
    } catch {
        return $false
    }
}

function Set-QwenGpuProfileValue {
    # Change one top-level field of the record in place. A text edit rather than a
    # ConvertFrom-Json round trip: Windows PowerShell 5.1 can re-serialise arrays read back
    # from JSON as {"value":[...],"Count":n}.
    param([string]$Root, [string]$Name, $Value)
    try {
        $path = Get-QwenGpuProfilePath -Root $Root
        if (-not (Test-Path -LiteralPath $path)) { return $false }
        $text = [System.IO.File]::ReadAllText($path)
        $json = if ($null -eq $Value) { 'null' } elseif ($Value -is [int] -or $Value -is [long]) { [string]$Value } else { '"' + ([string]$Value -replace '\\', '\\' -replace '"', '\"') + '"' }
        $pattern = '"' + [regex]::Escape($Name) + '"\s*:\s*(null|-?\d+|"(?:[^"\\]|\\.)*")'
        if (-not [regex]::IsMatch($text, $pattern)) { return $false }
        $text = [regex]::Replace($text, $pattern, ('"{0}":  {1}' -f $Name, $json).Replace('$', '$$'), 1)
        return (Write-QwenGpuProfileText -Path $path -Text $text)
    } catch { return $false }
}

function Set-QwenGpuProfileServerPid {
    param([string]$Root, [int]$ServerPid)
    return (Set-QwenGpuProfileValue -Root $Root -Name 'server_pid' -Value $ServerPid)
}

function Get-QwenOllamaLogFacts {
    # What the server log of the current start says about devices. The launchers truncate
    # their log on every start, so everything in it belongs to the running server.
    param([string]$LogPath)
    $facts = [ordered]@{ Exists = $false; Discovered = $false; GpuLines = @(); ZeroVram = $false; Config = $null; LastWrite = $null }
    if (-not $LogPath -or -not (Test-Path -LiteralPath $LogPath)) { return [pscustomobject]$facts }
    $facts.Exists = $true
    $facts.LastWrite = (Get-Item -LiteralPath $LogPath).LastWriteTime
    $lines = @()
    try {
        # The discovery lines are near the top; reading the head keeps this cheap on a
        # log that grows to megabytes.
        $lines = @(Get-Content -LiteralPath $LogPath -TotalCount 400 -ErrorAction Stop)
    } catch { return [pscustomobject]$facts }
    $facts.Config = $lines | Where-Object { $_ -match 'msg="server config"' } | Select-Object -First 1
    $compute = @($lines | Where-Object { $_ -match 'msg="inference compute"' })
    if ($compute.Count -gt 0) { $facts.Discovered = $true }
    $facts.GpuLines = @($compute | Where-Object { $_ -notmatch 'library=cpu' } | ForEach-Object {
        [pscustomobject]@{
            Description = if ($_ -match 'description="([^"]+)"') { $Matches[1] } else { '?' }
            Library     = if ($_ -match 'libdirs=(\S+)') { $Matches[1] } else { '?' }
            Total       = if ($_ -match 'total="([^"]+)"') { $Matches[1] } else { '?' }
        }
    })
    $ctx = $lines | Where-Object { $_ -match 'vram-based default context' } | Select-Object -First 1
    if ($ctx -and $ctx -match 'total_vram="0 B"') { $facts.ZeroVram = $true }
    return [pscustomobject]$facts
}

function Resolve-QwenGpuProfile {
    # Sets the GPU-related environment of the current process for the server that is
    # about to be started, records the decision, and returns the previous process values
    # so a caller that keeps running (Start-Ollama.ps1 inside start-webui.ps1) can put
    # them back. -Test* parameters let Test-QwenGpu.ps1 -SelfTest exercise the decision
    # without the hardware; the launchers never pass them.
    param(
        [string]$Root, [string]$OllamaExe, [string]$Launcher = '', [string]$ServerLog = '',
        $TestInventory = $null, $TestPnp = $null, $TestMode = $null, [switch]$NoRetry, $TestExternal = $null,
        $TestSelection = $null, [string]$OutRoot = ''
    )
    # -OutRoot: where the record is written (the watcher's adoption check dry-runs the
    # decision into a scratch folder so a hypothetical server never looks current).
    if (-not $OutRoot) { $OutRoot = $Root }
    $ErrorActionPreference = 'Continue'
    $script:QwenGpuRecordWritten = $false
    $saved = @{}
    $configured = @{}
    foreach ($name in $script:QwenGpuEnvNames) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        $configured[$name] = Get-QwenConfiguredValue -Name $name
    }
    try {
        if ($null -ne $TestMode) { $mode = [string]$TestMode }
        else { $mode = [string][Environment]::GetEnvironmentVariable('QWEN_GPU_MODE', 'User') }
        $mode = $mode.Trim().ToLowerInvariant()
        if (-not $mode) { $mode = 'auto' }
        if ($mode -notin @('auto', 'cpu', 'all', 'vulkan', 'manual')) {
            Write-QwenGpuLog -Root $Root -Message "unknown QWEN_GPU_MODE '$mode' - using auto"
            $mode = 'auto'
        }
        # A library or a device list the user configured wins: "auto" would override both.
        if ($null -eq $TestMode -and ($configured['OLLAMA_LLM_LIBRARY'] -or $configured['CUDA_VISIBLE_DEVICES'])) { $mode = 'manual' }
        # The processing unit chosen with Select-QwenCompute.ps1 / the web page / the chat
        # tool. It beats QWEN_GPU_MODE (which only applies while the choice is 'auto'); only a
        # manual User/Machine OLLAMA_LLM_LIBRARY or CUDA_VISIBLE_DEVICES beats it.
        $selection = if ($null -ne $TestSelection) { $TestSelection } else { Read-QwenComputeSelection -Root $Root }
        $selectionStamp = if ($selection) { [string]$selection.updated_at } else { '' }
        $selectionKey = Get-QwenSelectionKey $selection
        $selectedUuids = @()
        $selectionMissing = $null
        if ($mode -ne 'manual') {
            if ($selectionKey -eq 'cpu') { $mode = 'cpu' }
            elseif ($selectionKey -like 'gpu:*') { $selectedUuids = @($selectionKey.Substring(4) -split ','); $mode = 'selected' }
        }

        $pnp = if ($null -ne $TestPnp) { @($TestPnp) } else { Get-QwenNvidiaPnpDevices }
        $pnpList = if ($null -eq $pnp) { @() } else { @($pnp) }
        $working = @($pnpList | Where-Object { $_.ErrorCode -eq 0 -and $_.Service -ieq 'nvlddmkm' })
        # Disabled in Device Manager (code 22) is a choice, not a fault: path B disables the
        # P620 on purpose.
        $disabled = @($pnpList | Where-Object { $_.ErrorCode -eq 22 } | ForEach-Object {
            [ordered]@{ name = $_.Name; pnp_id = $_.PnpId }
        })
        $problems = @($pnpList | Where-Object { $_.ErrorCode -ne 22 -and ($_.ErrorCode -ne 0 -or $_.Service -ieq 'BasicDisplay') } | ForEach-Object {
            [ordered]@{ name = $_.Name; pnp_id = $_.PnpId; service = $_.Service; error_code = $_.ErrorCode }
        })

        $inventory = $null
        $inventoryOk = $true
        $used = @(); $ignored = @()
        if ($mode -in @('auto', 'vulkan', 'selected')) {
            $inventory = if ($null -ne $TestInventory) { $TestInventory } else { Get-QwenGpuInventory }
            # "Incomplete": nvidia-smi is there, Windows shows at least one NVIDIA card
            # working, and nvidia-smi failed or listed fewer. Right after a Thunderbolt
            # plug-in or logon that is usually a card still initialising (on path B the RTX is
            # the only working card). No nvidia-smi at all, or no working card (path B
            # without an eGPU: "No devices were found"), is a normal state and not retried.
            $isIncomplete = {
                param($inv)
                ($null -ne $inv.SmiExitCode) -and ($working.Count -ge 1) -and
                    (($inv.SmiExitCode -ne 0) -or ($working.Count -gt @($inv.Gpus).Count))
            }
            $incomplete = & $isIncomplete $inventory
            if ($incomplete -and $null -eq $TestInventory -and -not $NoRetry) {
                # A Thunderbolt card that has just been bound can still be initialising.
                Start-Sleep -Seconds 5
                $inventory = Get-QwenGpuInventory
                $incomplete = & $isIncomplete $inventory
            }
            if ($incomplete) { $inventoryOk = $false }
            foreach ($gpu in @($inventory.Gpus)) {
                $reason = $null
                $driverMajor = 0; [void][int]::TryParse((([string]$gpu.Driver) -split '\.')[0], [ref]$driverMajor)
                if ($gpu.ComputeCap -lt $script:QwenGpuMinComputeCap) { $reason = ('compute capability {0} < {1}' -f $gpu.ComputeCap, $script:QwenGpuMinComputeCap) }
                elseif ($gpu.MemoryMiB -lt $script:QwenGpuMinMemoryMiB) { $reason = ('{0} MiB < {1} MiB' -f $gpu.MemoryMiB, $script:QwenGpuMinMemoryMiB) }
                elseif ($driverMajor -lt $script:QwenGpuMinDriverMajor) { $reason = ('driver {0} older than {1}' -f $gpu.Driver, $script:QwenGpuMinDriverMajor) }
                if ($reason) { $ignored += ,@($gpu, $reason) } else { $used += $gpu }
            }
            $used = @($used | Sort-Object -Property MemoryMiB -Descending)
            if ($mode -eq 'selected') {
                # An explicit choice is honoured even for a card "auto" would skip (the P620);
                # every other card is hidden. If none of the chosen cards is present, fall
                # back to the automatic choice until it comes back.
                $picked = @(@($inventory.Gpus) | Where-Object { $selectedUuids -contains $_.Uuid } | Sort-Object -Property MemoryMiB -Descending)
                if ($picked.Count -gt 0) {
                    $ignored = @(@($inventory.Gpus) | Where-Object { $selectedUuids -notcontains $_.Uuid } | ForEach-Object { ,@($_, 'not selected') })
                    $used = $picked
                } else {
                    $selectionMissing = ((@($selection.labels) + @($selectedUuids)) | Where-Object { $_ } | Select-Object -First 1)
                    $mode = 'auto'
                }
            }
        }

        # Start from what the user configured, then apply the decision on top. Inherited
        # process values never survive.
        $envPlan = [ordered]@{}
        foreach ($name in $script:QwenGpuEnvNames) {
            if ($name -ne 'OLLAMA_LLM_LIBRARY' -or $mode -ne 'manual') { $envPlan[$name] = $configured[$name] }
        }
        $profileName = if ($mode -eq 'selected') { 'gpu' } else { $mode }
        $note = $null
        switch ($mode) {
            # manual applies exactly what is configured, including a Machine-scope library
            # the launchers themselves do not read.
            'manual' { $envPlan['OLLAMA_LLM_LIBRARY'] = $configured['OLLAMA_LLM_LIBRARY'] }
            'all' { }
            'vulkan' {
                $envPlan['CUDA_VISIBLE_DEVICES'] = '-1'
                $envPlan['OLLAMA_VULKAN'] = '1'
                $envPlan['OLLAMA_LLM_LIBRARY'] = 'vulkan'
            }
            'cpu' {
                $envPlan['CUDA_VISIBLE_DEVICES'] = '-1'
                $envPlan['OLLAMA_VULKAN'] = '0'
                $envPlan['GGML_VK_VISIBLE_DEVICES'] = $null
                $envPlan['OLLAMA_LLM_LIBRARY'] = 'cpu'
            }
            default {
                if ($used.Count -gt 0) {
                    $profileName = 'gpu'
                    $envPlan['CUDA_VISIBLE_DEVICES'] = (($used | ForEach-Object { $_.Uuid }) -join ',')
                    $envPlan['OLLAMA_VULKAN'] = '0'
                    $envPlan['GGML_VK_VISIBLE_DEVICES'] = $null
                    $library = $null
                    $libRoot = if ($OllamaExe) { Join-Path (Split-Path -Parent $OllamaExe) 'lib\ollama' } else { $null }
                    $maxCc = ($used | Measure-Object -Property ComputeCap -Maximum).Maximum
                    if ($libRoot -and (Test-Path -LiteralPath (Join-Path $libRoot 'cuda_v12')) -and $maxCc -le 12.0) { $library = 'cuda_v12' }
                    $envPlan['OLLAMA_LLM_LIBRARY'] = $library
                } elseif (-not $inventoryOk -and $working.Count -ge 2) {
                    # Several NVIDIA cards work but nvidia-smi could not describe them: Ollama's
                    # own discovery would mix the P620 with an eGPU. CPU for this start.
                    $profileName = 'cpu'
                    $note = 'nvidia-smi incomplete with several working NVIDIA devices'
                    $envPlan['CUDA_VISIBLE_DEVICES'] = '-1'
                    $envPlan['OLLAMA_VULKAN'] = '0'
                    $envPlan['GGML_VK_VISIBLE_DEVICES'] = $null
                    $envPlan['OLLAMA_LLM_LIBRARY'] = 'cpu'
                } elseif ($script:QwenGpuNoEgpuProfile -eq 'internal') {
                    $profileName = 'internal'
                    if (-not $inventoryOk) { $note = 'nvidia-smi could not describe the working NVIDIA card - Ollama picks it itself for this start; the watcher tries once more when idle (if it stays like this: Restart-QwenOllama.ps1)' }
                } else {
                    $profileName = 'cpu'
                    $envPlan['CUDA_VISIBLE_DEVICES'] = '-1'
                    $envPlan['OLLAMA_VULKAN'] = '0'
                    $envPlan['GGML_VK_VISIBLE_DEVICES'] = $null
                    $envPlan['OLLAMA_LLM_LIBRARY'] = 'cpu'
                }
            }
        }
        foreach ($name in $envPlan.Keys) {
            [Environment]::SetEnvironmentVariable($name, $envPlan[$name], 'Process')
        }
        $notes = New-Object System.Collections.Generic.List[string]
        if ($note) { $notes.Add($note) }
        $selectionUnusable = $false
        if ($selectionMissing -and $inventoryOk) {
            # A chosen card that Windows shows but that is disabled or has no working driver
            # (the P620 on path B) will not come back by itself; a card in its box will.
            # Matched by the remembered name and PCI device id (a driverless card is renamed).
            $seenMap = Read-QwenSeenDevices -Root $Root
            $candidates = @()
            foreach ($u in $selectedUuids) { if ($seenMap.Contains($u)) { $candidates += ,@([string]$seenMap[$u].name, [string]$seenMap[$u].pci_dev) } }
            foreach ($l in @($selection.labels)) { if ($l) { $candidates += ,@([string]$l, '') } }
            $unusableNow = @($pnpList | Where-Object { $_.ErrorCode -ne 0 -or $_.Service -ieq 'BasicDisplay' })
            $s = $null; $sLabel = $null
            foreach ($c in $candidates) {
                $hit = @($unusableNow | Where-Object { Test-QwenSameCard -Name $c[0] -PciDev $c[1] -Device $_ }) | Select-Object -First 1
                if ($hit) { $s = $hit; $sLabel = $c[0]; break }
            }
            if ($s) {
                $why = if ($s.ErrorCode -eq 22) { 'is disabled in Device Manager' } elseif ($s.Service -ieq 'BasicDisplay') { 'has no NVIDIA driver' } else { "has no working driver (code $($s.ErrorCode))" }
                $notes.Add("chosen GPU $sLabel $why - running the automatic choice instead; choose auto or another GPU")
                $selectionUnusable = $true
            } else {
                $notes.Add("selected GPU not present ($selectionMissing) - using the automatic choice until it is back")
            }
        } elseif ($mode -eq 'selected') {
            $slow = @($used | Where-Object { $_.ComputeCap -lt $script:QwenGpuMinComputeCap -or $_.MemoryMiB -lt $script:QwenGpuMinMemoryMiB })
            if ($slow.Count -gt 0 -and $used.Count -gt 1) { $notes.Add('selected cards include a small or old one; a model split across them runs at its speed') }
        }
        # CPU only: said outright, so nobody reads "internal" as "the internal GPU helps".
        # Only when Windows lists NVIDIA devices and none works - a machine without NVIDIA
        # may still have a GPU that Ollama finds by itself.
        $cpuOnly = $null
        if ($profileName -eq 'cpu') { $cpuOnly = $true }
        elseif ($profileName -in @('gpu', 'vulkan')) { $cpuOnly = $false }
        # (@() around $pnpList: one device comes back as a bare object, and a bare
        # PSCustomObject has no .Count in Windows PowerShell 5.1)
        elseif ($profileName -eq 'internal' -and $null -ne $pnp -and @($pnpList).Count -gt 0) {
            $cpuOnly = ($working.Count -eq 0)
            if ($cpuOnly -and $problems.Count -gt 0) {
                $notes.Add(('no working NVIDIA GPU: {0} - Ollama runs on the CPU; run Test-QwenGpu.ps1' -f (($problems | ForEach-Object {
                    '{0} ({1})' -f $_.name, $(if ($_.service -ieq 'BasicDisplay') { 'no NVIDIA driver' } else { "code $($_.error_code)" }) }) -join ', ')))
            } elseif ($cpuOnly) {
                $notes.Add(('no working NVIDIA GPU ({0} disabled in Device Manager) - Ollama runs on the CPU' -f (($disabled | ForEach-Object { $_.name }) -join ', ')))
            }
        }

        $expected = 'none'
        if ($profileName -in @('gpu', 'vulkan') -and $used.Count -gt 0) {
            $largest = ($used | Measure-Object -Property MemoryMiB -Maximum).Maximum
            # tiny = a card like the P620 (only possible when chosen explicitly): it holds no
            # layers of the big models, so the router must not treat it as a GPU setup.
            $expected = if ($largest -ge $script:QwenGpuLargeMemoryMiB) { 'large' } elseif ($largest -ge $script:QwenGpuMinMemoryMiB) { 'small' } else { 'tiny' }
            if ($profileName -eq 'vulkan' -and @($ignored | Where-Object { $_[1] -notlike 'driver*' }).Count -gt 0) {
                # Vulkan cannot be told which NVIDIA card to skip from here, so a working
                # P620 would take part: never treat that as a card the big model owns.
                $expected = 'small'
                $notes.Add('vulkan mode with an internal NVIDIA card still working - disable it for full speed')
            }
        }
        $note = if ($notes.Count -gt 0) { $notes -join ' | ' } else { $null }
        $smiInfo = $null
        if ($inventory) { $smiInfo = [ordered]@{ exit_code = $inventory.SmiExitCode; error = $inventory.SmiError } }
        # Which used cards are removable (eGPU): the watcher's sleep rule only applies to those.
        $external = @{}
        if ($used.Count -gt 0) {
            $external = if ($null -ne $TestExternal) { $TestExternal } else { Get-QwenGpuExternalFlags -Devices $working }
        }
        $busOf = {
            param($pciBus)
            try { return [Convert]::ToInt32((([string]$pciBus) -split ':')[-2], 16) } catch { return -1 }
        }
        $envNow = [ordered]@{}
        foreach ($name in $script:QwenGpuEnvNames) { $envNow[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
        $signature = Get-QwenNvidiaPnpSignature -Devices $pnp
        $record = [ordered]@{
            version         = 2
            written_at      = (Get-Date).ToString('o')
            launcher        = $Launcher
            server_log      = $ServerLog
            mode            = $mode
            selection_stamp = $selectionStamp
            selection_key   = $selectionKey
            profile         = $profileName
            vram_class      = if ($expected -eq 'none') { 'none' } else { 'pending' }
            vram_expected   = $expected
            server_pid      = $null
            inventory_ok    = $inventoryOk
            note            = $note
            gpus_used       = @($used | ForEach-Object {
                $b = & $busOf $_.PciBus
                $info = if ($external.ContainsKey($b)) { $external[$b] } else { $null }
                # (tests pass plain booleans)
                $ext = if ($null -eq $info) { $null } elseif ($info -is [bool]) { $info } else { [bool]$info.External }
                $pid_ = if ($info -and $info -isnot [bool]) { [string]$info.PnpId } else { $null }
                [ordered]@{ name = $_.Name; uuid = $_.Uuid; memory_mib = $_.MemoryMiB; compute_cap = $_.ComputeCap; pci_bus = $_.PciBus; driver = $_.Driver; external = $ext; pnp_id = $pid_ }
            })
            gpus_ignored    = @($ignored | ForEach-Object { [ordered]@{ name = $_[0].Name; uuid = $_[0].Uuid; memory_mib = $_[0].MemoryMiB; compute_cap = $_[0].ComputeCap; reason = $_[1] } })
            nvidia_problem_devices = $problems
            nvidia_disabled_devices = $disabled
            cpu_only        = $cpuOnly
            selection_unusable = $selectionUnusable
            nvidia_smi      = $smiInfo
            pnp_signature   = if ($null -eq $signature) { 'unknown' } else { $signature }
            env             = $envNow
        }
        $script:QwenGpuRecordWritten = [bool](Save-QwenGpuProfile -Root $OutRoot -Record $record)

        $summary = 'profile={0} mode={1} vram={2} launcher={3} CUDA_VISIBLE_DEVICES={4} OLLAMA_LLM_LIBRARY={5} OLLAMA_VULKAN={6}' -f `
            $profileName, $mode, $expected, $Launcher, $envNow['CUDA_VISIBLE_DEVICES'], $envNow['OLLAMA_LLM_LIBRARY'], $envNow['OLLAMA_VULKAN']
        if ($used.Count -gt 0) { $summary += ' using: ' + (($used | ForEach-Object { '{0} {1}MiB cc{2} drv{3}' -f $_.Name, $_.MemoryMiB, $_.ComputeCap, $_.Driver }) -join ', ') }
        if ($ignored.Count -gt 0) { $summary += ' ignored: ' + (($ignored | ForEach-Object { '{0} ({1})' -f $_[0].Name, $_[1] }) -join ', ') }
        if ($problems.Count -gt 0) { $summary += ' NVIDIA device without a working driver: ' + (($problems | ForEach-Object { '{0} service={1} code={2}' -f $_.name, $_.service, $_.error_code }) -join ', ') }
        if ($disabled.Count -gt 0) { $summary += ' disabled in Device Manager: ' + (($disabled | ForEach-Object { $_.name }) -join ', ') }
        if ($cpuOnly -eq $true) { $summary += ' (CPU only)' }
        # "No devices were found" is expected when no NVIDIA card is enabled (path B, no eGPU).
        if ($inventory -and $inventory.SmiError -and $working.Count -gt 0) { $summary += ' nvidia-smi: ' + $inventory.SmiError }
        if ($note) { $summary += ' NOTE: ' + $note }
        if ($null -eq $pnp) { $summary += ' (PnP query failed)' }
        if (-not $script:QwenGpuRecordWritten) {
            $stillThere = Test-Path -LiteralPath (Get-QwenGpuProfilePath -Root $OutRoot)
            $summary += ' WARNING: data\gpu-profile.json could not be written' + $(if ($stillThere) { ' and the old one could not be removed' } else { ' (old record removed)' })
        }
        # The watcher's what-if runs write to a scratch folder: say so, so this log only
        # reads as starts that happened.
        if ($OutRoot -ne $Root) { $summary = 'dry run, not applied: ' + $summary }
        Write-QwenGpuLog -Root $Root -Message $summary
    } catch {
        # The decision itself failed: start with Ollama's defaults plus whatever the user
        # configured - never with values inherited from the process that ran the launcher.
        foreach ($name in $script:QwenGpuEnvNames) {
            if ($name -eq 'OLLAMA_LLM_LIBRARY') { continue }  # the launcher already set it from the registry
            [Environment]::SetEnvironmentVariable($name, $configured[$name], 'Process')
        }
        try { Remove-Item -LiteralPath (Get-QwenGpuProfilePath -Root $OutRoot) -Force -ErrorAction SilentlyContinue } catch { }
        $script:QwenGpuRecordWritten = $false
        Write-QwenGpuLog -Root $Root -Message ('GPU profile step failed, starting with Ollama defaults: ' + $_.Exception.Message)
    }
    return $saved
}

function Restore-QwenGpuEnvironment {
    param($Saved)
    if (-not $Saved) { return }
    foreach ($name in @($Saved.Keys)) {
        try { [Environment]::SetEnvironmentVariable($name, $Saved[$name], 'Process') } catch { }
    }
}

function Start-QwenGpuWatch {
    # The watcher restarts the server when an eGPU is plugged in or pulled out. It holds
    # its own mutex, so this only starts one if none is running. It is started with the
    # user's configured GPU variables, not this process's, so the launchers it runs later
    # start from a clean slate.
    param([string]$Root)
    try {
        $existing = $null
        if ([System.Threading.Mutex]::TryOpenExisting($script:QwenGpuWatchMutexName, [ref]$existing)) {
            $existing.Dispose()
            # One is running. Keep it if it is current; replace a watcher from older code.
            $info = $null
            try { $info = Get-Content -LiteralPath (Join-Path $Root 'data\gpu-watch.json') -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
            if ($info -and [int]$info.version -ge $script:QwenGpuWatchVersion -and (Get-Process -Id ([int]$info.pid) -ErrorAction SilentlyContinue)) { return }
            $old = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue | Where-Object { [string]$_.CommandLine -like '*watch-ollama-gpu.ps1*' })
            foreach ($o in $old) { Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue }
            Write-QwenGpuLog -Root $Root -Message ("replaced an outdated GPU watcher (pid {0})" -f (($old | ForEach-Object { $_.ProcessId }) -join ','))
            Start-Sleep -Milliseconds 500
        }
        $watch = Join-Path $Root 'watch-ollama-gpu.ps1'
        if (-not (Test-Path -LiteralPath $watch)) { return }
        $keep = @{}
        foreach ($name in $script:QwenGpuEnvNames) {
            $keep[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, (Get-QwenConfiguredValue -Name $name), 'Process')
        }
        try {
            $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            Start-Process -FilePath $shell -WindowStyle Hidden -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', ('"{0}"' -f $watch)
            ) | Out-Null
        } finally {
            Restore-QwenGpuEnvironment $keep
        }
    } catch {
        Write-QwenGpuLog -Root $Root -Message ('could not start the GPU watcher: ' + $_.Exception.Message)
    }
}

function Get-QwenRestartMarkerPath {
    param([string]$Root)
    return (Join-Path (Join-Path $Root 'data') 'ollama-restart.json')
}
