# Check that the local LLM stack is really using the GPU it should. Added 2026-09-29.
#
#   Test-QwenGpu.ps1                 read-only report: driver, devices, profile, server log
#   Test-QwenGpu.ps1 -SelfTest       run the GPU decision against simulated hardware
#   Test-QwenGpu.ps1 -Bench          also time qwen3.8:27b (loads the model if needed)
#   Test-QwenGpu.ps1 -EvictionCheck  after -Bench: does an embedding call push the chat
#                                    model out of VRAM?
#
# Run it after plugging in an eGPU, installing a driver, or changing QWEN_GPU_MODE.
# The report only reads; -Bench and -EvictionCheck send requests to Ollama. Its driver
# section checks "path B" (the latest NVIDIA driver for the RTX, the P620 disabled): which
# driver every NVIDIA device really runs on, old packages left in the driver store, and
# whether Windows is kept from reinstalling the Dell driver for the P620.

param(
    [switch]$SelfTest,
    [switch]$Bench,
    [switch]$EvictionCheck,
    [string]$Model = 'qwen3.8:27b'
)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
. (Join-Path $Root 'Resolve-OllamaGpu.ps1')
$Api = 'http://127.0.0.1:11434'
$script:Failures = 0
$script:Warnings = 0

function Say([string]$Level, [string]$Text) {
    $color = @{ OK = 'Green'; WARN = 'Yellow'; FAIL = 'Red'; INFO = 'Gray' }[$Level]
    if ($Level -eq 'FAIL') { $script:Failures++ }
    if ($Level -eq 'WARN') { $script:Warnings++ }
    Write-Host ('[{0,-4}] {1}' -f $Level, $Text) -ForegroundColor $color
}

function Invoke-SelfTest {
    $configuredOverride = @('OLLAMA_LLM_LIBRARY', 'CUDA_VISIBLE_DEVICES') | Where-Object { Get-QwenConfiguredValue -Name $_ }
    if (@($configuredOverride).Count -gt 0) { Say WARN ('User/Machine {0} is set: the expected values below assume it is not, so some cases may FAIL for that reason alone' -f ($configuredOverride -join ', ')) }
    $sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ('qwen-gpu-selftest-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $sandbox | Out-Null
    $exe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    $hasV12 = Test-Path (Join-Path (Split-Path $exe) 'lib\ollama\cuda_v12')
    function Gpu($name, $uuid, $mib, $cc, $drv = '581.80') {
        [pscustomobject]@{ Index = '0'; Uuid = $uuid; Name = $name; MemoryMiB = $mib; ComputeCap = $cc; PciBus = '00000000:07:00.0'; Driver = $drv }
    }
    function Inv($gpus, $exit = 0, $err = $null) {
        [pscustomobject]@{ SmiPath = 'fake'; SmiExitCode = $exit; SmiError = $err; Gpus = @($gpus) }
    }
    function Dev($id, $svc, $code, $name = 'fake') { [pscustomobject]@{ Name = $name; PnpId = $id; Service = $svc; ErrorCode = $code } }
    $p620 = Gpu 'Quadro P620' 'GPU-p620' 4096 6.1
    $g4090 = Gpu 'NVIDIA GeForce RTX 4090' 'GPU-4090' 24564 8.9
    $g4060 = Gpu 'NVIDIA GeForce RTX 4060 Ti' 'GPU-4060ti' 8188 8.9
    $g3060old = Gpu 'NVIDIA GeForce RTX 3060' 'GPU-3060' 6144 8.6 '537.13'
    $devP620 = Dev 'PCI\VEN_10DE&DEV_1CBD\A' 'nvlddmkm' 0
    $dev4090 = Dev 'PCI\VEN_10DE&DEV_2684\B' 'nvlddmkm' 0
    # path B: the P620 disabled on purpose; the RTX working, or broken by an old driver
    $devP620off = Dev 'PCI\VEN_10DE&DEV_1CBD\A' 'nvlddmkm' 22 'NVIDIA Quadro P620'
    $dev4090ok = Dev 'PCI\VEN_10DE&DEV_2684\B' 'nvlddmkm' 0 'NVIDIA GeForce RTX 4090'
    $dev4090bad = Dev 'PCI\VEN_10DE&DEV_2684\B' 'nvlddmkm' 43 'NVIDIA GeForce RTX 4090'
    $lib = if ($hasV12) { 'cuda_v12' } else { $null }
    # With no eGPU, "auto" either leaves the internal card to Ollama or hides it.
    if ($script:QwenGpuNoEgpuProfile -eq 'internal') { $none = @{ Profile = 'internal'; Cvd = $null; Lib = $null } }
    else { $none = @{ Profile = 'cpu'; Cvd = '-1'; Lib = 'cpu' } }
    $cases = @(
        @{ Name = 'no eGPU, P620 present (today)';            Inv = (Inv @($p620));           Pnp = @($devP620); Mode = 'auto'; Profile = $none.Profile; Cvd = $none.Cvd; Lib = $none.Lib; Vram = 'none'; Exp = 'none' },
        @{ Name = 'RTX 4090 + P620 (driver 581.80, path A)';  Inv = (Inv @($p620, $g4090));   Pnp = @($devP620, $dev4090); Mode = 'auto'; Profile = 'gpu'; Cvd = 'GPU-4090'; Lib = $lib; Vram = 'pending'; Exp = 'large' },
        @{ Name = 'RTX 4060 Ti + P620';                       Inv = (Inv @($p620, $g4060));   Pnp = @(); Mode = 'auto'; Profile = 'gpu'; Cvd = 'GPU-4060ti'; Lib = $lib; Vram = 'pending'; Exp = 'small' },
        @{ Name = 'RTX 4090 alone (P620 disabled, path B)';   Inv = (Inv @($g4090));          Pnp = @(); Mode = 'auto'; Profile = 'gpu'; Cvd = 'GPU-4090'; Lib = $lib; Vram = 'pending'; Exp = 'large' },
        @{ Name = 'both cards (biggest first)';               Inv = (Inv @($g4060, $g4090));  Pnp = @(); Mode = 'auto'; Profile = 'gpu'; Cvd = 'GPU-4090,GPU-4060ti'; Lib = $lib; Vram = 'pending'; Exp = 'large' },
        @{ Name = 'nvidia-smi finds no device (path B, unplugged)'; Inv = (Inv @() 6 'No devices were found'); Pnp = @(); Mode = 'auto'; Profile = $none.Profile; Cvd = $none.Cvd; Lib = $none.Lib; Vram = 'none'; Exp = 'none' },
        @{ Name = 'nvidia-smi timed out with P620 + eGPU working'; Inv = (Inv @() -1 'timed out'); Pnp = @($devP620, $dev4090); Mode = 'auto'; Profile = 'cpu'; Cvd = '-1'; Lib = 'cpu'; Vram = 'none'; Exp = 'none'; InvOk = $false },
        @{ Name = 'nvidia-smi lists only the P620 while the eGPU works'; Inv = (Inv @($p620)); Pnp = @($devP620, $dev4090); Mode = 'auto'; Profile = 'cpu'; Cvd = '-1'; Lib = 'cpu'; Vram = 'none'; Exp = 'none'; InvOk = $false },
        @{ Name = 'no nvidia-smi at all (AMD/Intel machine)';  Inv = (Inv @() $null 'nvidia-smi not found'); Pnp = @(); Mode = 'auto'; Profile = $none.Profile; Cvd = $none.Cvd; Lib = $none.Lib; Vram = 'none'; Exp = 'none' },
        @{ Name = 'nvidia-smi fails with only the P620 working'; Inv = (Inv @() 9 'driver not loaded'); Pnp = @($devP620); Mode = 'auto'; Profile = $none.Profile; Cvd = $none.Cvd; Lib = $none.Lib; Vram = 'none'; Exp = 'none'; InvOk = $false },
        @{ Name = 'RTX 3060 on driver 537 (too old for Ollama CUDA)'; Inv = (Inv @($g3060old)); Pnp = @(); Mode = 'auto'; Profile = $none.Profile; Cvd = $none.Cvd; Lib = $none.Lib; Vram = 'none'; Exp = 'none' },
        @{ Name = 'QWEN_GPU_MODE=cpu with a 4090 attached';   Inv = (Inv @($g4090));          Pnp = @(); Mode = 'cpu';  Profile = 'cpu'; Cvd = '-1'; Lib = 'cpu'; Vram = 'none'; Exp = 'none' },
        @{ Name = 'QWEN_GPU_MODE=all (old behaviour)';        Inv = (Inv @($p620, $g4090));   Pnp = @(); Mode = 'all';  Profile = 'all'; Cvd = $null; Lib = $null; Vram = 'none'; Exp = 'none' },
        @{ Name = 'QWEN_GPU_MODE=vulkan, P620 disabled';      Inv = (Inv @($g4090));          Pnp = @(); Mode = 'vulkan'; Profile = 'vulkan'; Cvd = '-1'; Lib = 'vulkan'; Vram = 'pending'; Exp = 'large' },
        @{ Name = 'QWEN_GPU_MODE=vulkan, P620 still working'; Inv = (Inv @($p620, $g4090));   Pnp = @(); Mode = 'vulkan'; Profile = 'vulkan'; Cvd = '-1'; Lib = 'vulkan'; Vram = 'pending'; Exp = 'small' },
        @{ Name = 'manual mode applies only configured values'; Inv = (Inv @($g4090));      Pnp = @(); Mode = 'manual'; Profile = 'manual'; Cvd = $null; Lib = $null; Vram = 'none'; Exp = 'none'; Inherit = 'GPU-stale' },
        @{ Name = 'path B: P620 disabled + RTX 4090';        Inv = (Inv @($g4090));          Pnp = @($devP620off, $dev4090ok); Mode = 'auto'; Profile = 'gpu'; Cvd = 'GPU-4090'; Lib = $lib; Vram = 'pending'; Exp = 'large'; Problems = 0; Disabled = 1; CpuOnly = $false }
    )
    if ($none.Profile -eq 'internal') {
        $cases += @(
            @{ Name = 'path B: P620 disabled, no eGPU (CPU only, no warnings)'; Inv = (Inv @() 6 'No devices were found'); Pnp = @($devP620off); Mode = 'auto'; Profile = 'internal'; Cvd = $null; Lib = $null; Vram = 'none'; Exp = 'none'; Problems = 0; Disabled = 1; CpuOnly = $true; NoteLike = '*disabled in Device Manager*CPU*' },
            @{ Name = 'path B: an old driver came back (RTX code 43)'; Inv = (Inv @() 6 'No devices were found'); Pnp = @($devP620off, $dev4090bad); Mode = 'auto'; Profile = 'internal'; Cvd = $null; Lib = $null; Vram = 'none'; Exp = 'none'; Problems = 1; Disabled = 1; CpuOnly = $true; NoteLike = '*no working NVIDIA GPU*code 43*' },
            @{ Name = 'path B: RTX works but nvidia-smi timed out (retried by the watcher)'; Inv = (Inv @() -1 'timed out'); Pnp = @($devP620off, $dev4090ok); Mode = 'auto'; Profile = 'internal'; Cvd = $null; Lib = $null; Vram = 'none'; Exp = 'none'; InvOk = $false; CpuOnly = $false; NoteLike = '*nvidia-smi could not describe*' }
        )
    }
    foreach ($case in $cases) {
        $env:CUDA_VISIBLE_DEVICES = $case.Inherit
        $env:OLLAMA_LLM_LIBRARY = if ($case.Inherit) { 'inherited' } else { $null }
        $saved = Resolve-QwenGpuProfile -Root $sandbox -OllamaExe $exe -Launcher 'selftest' -TestInventory $case.Inv -TestPnp $case.Pnp -TestMode $case.Mode -TestExternal @{ 7 = $true }
        $record = Read-QwenGpuProfile -Root $sandbox
        $cvd = $env:CUDA_VISIBLE_DEVICES; $libNow = $env:OLLAMA_LLM_LIBRARY
        Restore-QwenGpuEnvironment $saved
        $env:CUDA_VISIBLE_DEVICES = $null; $env:OLLAMA_LLM_LIBRARY = $null
        $wantLib = $case.Lib
        $wantInvOk = if ($case.ContainsKey('InvOk')) { $case.InvOk } else { $true }
        $extOk = (@($record.gpus_used).Count -eq 0) -or (@($record.gpus_used | Where-Object { $_.external -eq $true }).Count -eq @($record.gpus_used).Count)
        $ok = ($record.profile -eq $case.Profile) -and ($cvd -eq $case.Cvd) -and ($libNow -eq $wantLib) -and
              ($record.vram_class -eq $case.Vram) -and ($record.vram_expected -eq $case.Exp) -and $script:QwenGpuRecordWritten -and
              ($record.inventory_ok -eq $wantInvOk) -and $extOk
        if ($case.ContainsKey('Problems')) { $ok = $ok -and (@($record.nvidia_problem_devices | Where-Object { $_ }).Count -eq $case.Problems) }
        if ($case.ContainsKey('Disabled')) { $ok = $ok -and (@($record.nvidia_disabled_devices | Where-Object { $_ }).Count -eq $case.Disabled) }
        if ($case.ContainsKey('CpuOnly')) { $ok = $ok -and ($null -ne $record.cpu_only) -and ([bool]$record.cpu_only -eq $case.CpuOnly) }
        if ($case.NoteLike) { $ok = $ok -and ([string]$record.note -like $case.NoteLike) }
        $detail = 'profile={0} CUDA_VISIBLE_DEVICES={1} OLLAMA_LLM_LIBRARY={2} vram={3}/{4} inventory_ok={5}' -f $record.profile, $cvd, $libNow, $record.vram_class, $record.vram_expected, $record.inventory_ok
        if ($case.ContainsKey('Problems') -or $case.ContainsKey('CpuOnly') -or $case.NoteLike) { $detail += (' problems={0} disabled={1} cpu_only={2} note={3}' -f @($record.nvidia_problem_devices | Where-Object { $_ }).Count, @($record.nvidia_disabled_devices | Where-Object { $_ }).Count, $record.cpu_only, $record.note) }
        if ($ok) { Say OK "$($case.Name): $detail" } else { Say FAIL "$($case.Name): $detail (expected $($case.Profile)/$($case.Cvd)/$wantLib/$($case.Vram)/$($case.Exp)/$wantInvOk, external flags ok=$extOk)" }
    }

    # The processing unit chosen with Select-QwenCompute.ps1 / the web page / the chat tool.
    function Sel($device, $gpus = @(), $labels = @()) { [pscustomobject]@{ device = $device; gpus = @($gpus); labels = @($labels); updated_at = 'stamp-' + $device } }
    $selCases = @(
        @{ Name = 'chosen: CPU (4090 attached)';               Inv = (Inv @($p620, $g4090)); Sel = (Sel 'cpu');                                   Profile = 'cpu'; Cvd = '-1';        Lib = 'cpu';  Exp = 'none' },
        @{ Name = 'chosen: the P620 although a 4090 is attached'; Inv = (Inv @($p620, $g4090)); Sel = (Sel 'gpu' @('GPU-p620') @('Quadro P620'));   Profile = 'gpu'; Cvd = 'GPU-p620';  Lib = $lib;   Exp = 'tiny' },
        @{ Name = 'chosen: the 4090';                           Inv = (Inv @($p620, $g4090)); Sel = (Sel 'gpu' @('GPU-4090') @('RTX 4090'));        Profile = 'gpu'; Cvd = 'GPU-4090';  Lib = $lib;   Exp = 'large' },
        @{ Name = 'chosen: the 4090 but it is unplugged';       Inv = (Inv @($p620));         Sel = (Sel 'gpu' @('GPU-4090') @('RTX 4090'));        Profile = $none.Profile; Cvd = $none.Cvd; Lib = $none.Lib; Exp = 'none'; Note = 'selected GPU not present'; Unusable = $false },
        @{ Name = 'chosen: the P620, disabled on path B';       Inv = (Inv @($g4090));        Pnp = @($devP620off, $dev4090ok); Sel = (Sel 'gpu' @('GPU-p620') @('Quadro P620')); Profile = 'gpu'; Cvd = 'GPU-4090'; Lib = $lib; Exp = 'large'; Note = 'is disabled in Device Manager'; Unusable = $true },
        @{ Name = 'chosen: the P620, renamed by Windows (no driver)'; Inv = (Inv @($g4090));   Pnp = @((Dev 'PCI\VEN_10DE&DEV_1CBD\A' 'BasicDisplay' 0 'Microsoft Basic Display Adapter'), $dev4090ok); Sel = (Sel 'gpu' @('GPU-p620') @('Quadro P620')); Profile = 'gpu'; Cvd = 'GPU-4090'; Lib = $lib; Exp = 'large'; Note = 'has no NVIDIA driver'; Unusable = $true },
        @{ Name = 'chosen: auto';                               Inv = (Inv @($p620, $g4090)); Sel = (Sel 'auto');                                  Profile = 'gpu'; Cvd = 'GPU-4090';  Lib = $lib;   Exp = 'large' }
    )
    foreach ($case in $selCases) {
        $pnpFake = if ($case.Pnp) { $case.Pnp } else { @() }
        $saved = Resolve-QwenGpuProfile -Root $sandbox -OllamaExe $exe -Launcher 'selftest' -TestInventory $case.Inv -TestPnp $pnpFake -TestSelection $case.Sel -TestMode 'auto' -TestExternal @{ 7 = $true }
        $record = Read-QwenGpuProfile -Root $sandbox
        $cvd = $env:CUDA_VISIBLE_DEVICES; $libNow = $env:OLLAMA_LLM_LIBRARY
        Restore-QwenGpuEnvironment $saved
        $ok = ($record.profile -eq $case.Profile) -and ($cvd -eq $case.Cvd) -and ($libNow -eq $case.Lib) -and ($record.vram_expected -eq $case.Exp) -and ($record.selection_key -eq (Get-QwenSelectionKey $case.Sel))
        if ($case.Note) { $ok = $ok -and ([string]$record.note -like "*$($case.Note)*") }
        if ($case.ContainsKey('Unusable')) { $ok = $ok -and ([bool]$record.selection_unusable -eq $case.Unusable) }
        $detail = 'profile={0} CUDA_VISIBLE_DEVICES={1} OLLAMA_LLM_LIBRARY={2} vram={3} note={4}' -f $record.profile, $cvd, $libNow, $record.vram_expected, $record.note
        if ($ok) { Say OK "$($case.Name): $detail" } else { Say FAIL "$($case.Name): $detail (expected $($case.Profile)/$($case.Cvd)/$($case.Lib)/$($case.Exp))" }
    }
    $selDir = Join-Path $sandbox 'selroot'
    $first = Save-QwenComputeSelection -Root $selDir -Device 'gpu' -Gpus @('GPU-4090') -Labels @('RTX 4090') -By 'selftest'
    $again = Save-QwenComputeSelection -Root $selDir -Device 'gpu' -Gpus @('GPU-4090') -Labels @('RTX 4090') -By 'selftest'
    $back = Read-QwenComputeSelection -Root $selDir
    if ($back.device -eq 'gpu' -and @($back.gpus)[0] -eq 'GPU-4090' -and $first.changed -and -not $again.changed -and (Get-QwenSelectionKey $back) -eq 'gpu:GPU-4090') { Say OK 'selection file round-trips; saving the same choice again changes nothing' } else { Say FAIL 'selection file round-trip / unchanged detection' }
    $timed = Save-QwenComputeSelection -Root $selDir -Device 'cpu' -Hours 2 -By 'selftest'
    $expired = [pscustomobject]@{ device = 'cpu'; gpus = @(); expires_at = (Get-Date).AddMinutes(-1).ToString('o') }
    if ((Get-QwenSelectionKey (Read-QwenComputeSelection -Root $selDir)) -eq 'cpu' -and $timed.expires_at -and (Get-QwenSelectionKey $expired) -eq 'auto') { Say OK 'a timed choice holds until it runs out, then counts as auto' } else { Say FAIL 'timed selection' }

    $saved = Resolve-QwenGpuProfile -Root $sandbox -OllamaExe $exe -Launcher 'selftest' -TestInventory (Inv @($p620)) -TestPnp @($devP620, (Dev 'PCI\VEN_10DE&DEV_2684\B' 'BasicDisplay' 0)) -TestMode 'auto'
    Restore-QwenGpuEnvironment $saved
    $record = Read-QwenGpuProfile -Root $sandbox
    if ($record.profile -eq $none.Profile -and @($record.nvidia_problem_devices).Count -eq 1) { Say OK "eGPU without an NVIDIA driver: not used (profile $($none.Profile)) and reported as a problem device" }
    else { Say FAIL ('eGPU without an NVIDIA driver: profile={0} problems={1}' -f $record.profile, @($record.nvidia_problem_devices).Count) }

    $saved = Resolve-QwenGpuProfile -Root $sandbox -OllamaExe $exe -Launcher 'selftest' -TestInventory (Inv @($g4090)) -TestPnp $null -TestMode 'auto'
    Restore-QwenGpuEnvironment $saved
    [void](Set-QwenGpuProfileServerPid -Root $sandbox -ServerPid 4242)
    [void](Set-QwenGpuProfileValue -Root $sandbox -Name 'vram_class' -Value 'large')
    $record = Read-QwenGpuProfile -Root $sandbox
    if ($record.server_pid -eq 4242 -and $record.vram_class -eq 'large' -and @($record.gpus_used).Count -eq 1 -and $record.gpus_used[0].uuid -eq 'GPU-4090') { Say OK 'record fields update in place (server_pid, vram_class) without damaging the rest' }
    else { Say FAIL ('in-place record update: pid={0} vram={1} gpus={2}' -f $record.server_pid, $record.vram_class, @($record.gpus_used).Count) }

    # A record that cannot be written must not survive looking current.
    $tmpDir = Join-Path $sandbox 'data\gpu-profile.json.tmp'
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    $saved = Resolve-QwenGpuProfile -Root $sandbox -OllamaExe $exe -Launcher 'selftest' -TestInventory (Inv @($g4090)) -TestPnp @() -TestMode 'auto'
    $cvd = $env:CUDA_VISIBLE_DEVICES
    Restore-QwenGpuEnvironment $saved
    $left = Test-Path (Join-Path $sandbox 'data\gpu-profile.json')
    if (-not $script:QwenGpuRecordWritten -and -not $left -and $cvd -eq 'GPU-4090') { Say OK 'failed record write: decision kept, stale record removed, pid will not be stamped' }
    else { Say FAIL ('failed record write: written={0} old file left={1} CVD={2}' -f $script:QwenGpuRecordWritten, $left, $cvd) }
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

    $sigA = Get-QwenNvidiaPnpSignature -Devices @($devP620)
    $sigB = Get-QwenNvidiaPnpSignature -Devices @($devP620, (Dev 'Y' 'BasicDisplay' 0))
    $sigC = Get-QwenNvidiaPnpSignature -Devices @($devP620, (Dev 'Y' 'nvlddmkm' 0))
    $sigN = Get-QwenNvidiaPnpSignature -Devices $null
    if ($sigA -ne $sigB -and $sigB -ne $sigC -and $null -eq $sigN) { Say OK 'PnP signature changes on plug-in and on driver bind; a failed query gives no signature' }
    else { Say FAIL 'PnP signature does not behave' }
    $sigD1 = Get-QwenNvidiaPnpSignature -Devices @((Dev 'A' 'nvlddmkm' 22))
    $sigD2 = Get-QwenNvidiaPnpSignature -Devices @((Dev 'A' '' 22))
    if ($sigD1 -eq $sigD2) { Say OK 'a disabled card stays "disabled" in the signature when its driver package changes (no pointless restart)' }
    else { Say FAIL "disabled card signature changes with its service ($sigD1 / $sigD2)" }

    # Which device changes make a waiting restart go ahead at once (Test-QwenServerDeviceLost).
    function Rec($prof, $sig, $ids = @(), $envVars = @{}) { [pscustomobject]@{ profile = $prof; pnp_signature = $sig; gpus_used = @($ids | ForEach-Object { [pscustomobject]@{ pnp_id = $_ } }); env = [pscustomobject]$envVars } }
    $lostCases = @(
        @{ Name = 'P620 disabled while the internal server uses it'; Rec = (Rec 'internal' 'A|nvlddmkm|0'); Now = 'A|disabled|22'; Want = $true },
        @{ Name = 'RTX appears without a driver next to a disabled P620'; Rec = (Rec 'internal' 'A|disabled|22'); Now = 'A|disabled|22;B|BasicDisplay|0'; Want = $false },
        @{ Name = 'RTX gets its driver (internal server, CPU only)'; Rec = (Rec 'internal' 'A|disabled|22;B|BasicDisplay|0'); Now = 'A|disabled|22;B|nvlddmkm|0'; Want = $false },
        @{ Name = 'eGPU in use unplugged'; Rec = (Rec 'gpu' 'A|disabled|22;B|nvlddmkm|0' @('B')); Now = 'A|disabled|22'; Want = $true },
        @{ Name = 'eGPU in use broken by an old driver (code 43)'; Rec = (Rec 'gpu' 'A|disabled|22;B|nvlddmkm|0' @('B')); Now = 'A|disabled|22;B|nvlddmkm|43'; Want = $true },
        @{ Name = 'an unused card goes away (gpu profile)'; Rec = (Rec 'gpu' 'A|disabled|22;B|nvlddmkm|0' @('B')); Now = 'B|nvlddmkm|0'; Want = $false },
        @{ Name = 'CPU profile never waits on devices'; Rec = (Rec 'cpu' 'A|nvlddmkm|0'); Now = 'none'; Want = $false },
        @{ Name = 'gpu profile without ids: the disabled P620 re-enabled (never used)'; Rec = (Rec 'gpu' 'A|disabled|22;B|nvlddmkm|0'); Now = 'A|nvlddmkm|43;B|nvlddmkm|0'; Want = $false },
        @{ Name = 'vulkan: a working P620 (seen by Vulkan) disabled'; Rec = (Rec 'vulkan' 'A|nvlddmkm|0;B|nvlddmkm|0' @('B')); Now = 'A|disabled|22;B|nvlddmkm|0'; Want = $true },
        @{ Name = 'vulkan with a GGML_VK_VISIBLE_DEVICES index: cannot tell, any working card counts'; Rec = (Rec 'vulkan' 'A|nvlddmkm|0;B|nvlddmkm|0' @('B') @{ GGML_VK_VISIBLE_DEVICES = '0' }); Now = 'A|disabled|22;B|nvlddmkm|0'; Want = $true },
        @{ Name = 'gpu with one used card not identified: its loss is urgent'; Rec = ([pscustomobject]@{ profile = 'gpu'; pnp_signature = 'A|nvlddmkm|0;B|nvlddmkm|0'; gpus_used = @([pscustomobject]@{ pnp_id = 'A' }, [pscustomobject]@{ pnp_id = $null }); env = [pscustomobject]@{} }); Now = 'A|nvlddmkm|0'; Want = $true },
        @{ Name = 'manual, every card allowed: eGPU unplugged'; Rec = (Rec 'manual' 'A|disabled|22;B|nvlddmkm|0' @() @{ OLLAMA_LLM_LIBRARY = 'cuda_v12' }); Now = 'A|disabled|22'; Want = $true },
        @{ Name = 'manual, CUDA list but Vulkan still on: urgent'; Rec = (Rec 'manual' 'A|nvlddmkm|0;B|nvlddmkm|0' @() @{ CUDA_VISIBLE_DEVICES = 'GPU-x' }); Now = 'B|nvlddmkm|0'; Want = $true },
        @{ Name = 'manual, CUDA list and Vulkan off: cannot tell, not urgent'; Rec = (Rec 'manual' 'A|nvlddmkm|0;B|nvlddmkm|0' @() @{ CUDA_VISIBLE_DEVICES = 'GPU-x'; OLLAMA_VULKAN = '0' }); Now = 'B|nvlddmkm|0'; Want = $false },
        @{ Name = 'manual with the cpu library: never'; Rec = (Rec 'manual' 'A|nvlddmkm|0' @() @{ OLLAMA_LLM_LIBRARY = 'cpu' }); Now = 'A|disabled|22'; Want = $false }
    )
    $bad = @($lostCases | Where-Object { [bool](Test-QwenServerDeviceLost -Record $_.Rec -Signature $_.Now) -ne $_.Want })
    if ($bad.Count -eq 0) { Say OK ('device loss rule: {0} cases (a used or, on internal, a working card lost = restart at once)' -f $lostCases.Count) }
    else { Say FAIL ('device loss rule wrong for: ' + (($bad | ForEach-Object { $_.Name }) -join '; ')) }

    # Select-QwenCompute.ps1's device list (the menu, the web page and the chat tool) in the
    # path-B states, with the P620 remembered from before (no PCI id recorded yet) and the
    # 4090 remembered with its id.
    try {
        $selPath = Join-Path $Root 'Select-QwenCompute.ps1'
        $selAst = [System.Management.Automation.Language.Parser]::ParseFile($selPath, [ref]$null, [ref]$null)
        $fnText = (@($selAst.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Get-ComputeDevices', 'Resolve-DeviceArgs') }, $true) | ForEach-Object { $_.Extent.Text }) -join "`n")
        $devP620bd = Dev 'PCI\VEN_10DE&DEV_1CBD\A' 'BasicDisplay' 0 'Microsoft Basic Display Adapter'
        $listCases = @(
            @{ Name = 'P620 disabled + a working 4090'; Inv = (Inv @($g4090)); Pnp = @($devP620off, $dev4090ok); Pick = 'P620'; Unav = @('NVIDIA Quadro P620'); Absent = 0; Gpu = 1; Refused = $true },
            @{ Name = 'P620 renamed by Windows (no driver), no eGPU'; Inv = (Inv @() 6 'No devices were found'); Pnp = @($devP620bd); Pick = 'P620'; Unav = @('Microsoft Basic Display Adapter (Quadro P620)'); Absent = 1; Gpu = 0; Refused = $true },
            @{ Name = '4090 working but nvidia-smi not ready (still choosable)'; Inv = (Inv @() -1 'timed out'); Pnp = @($devP620off, $dev4090ok); Pick = '4090'; Unav = @('NVIDIA Quadro P620'); Absent = 1; Gpu = 0; Refused = $false; AbsentLike = 'attached;*' }
        )
        $i = 0
        foreach ($lc in $listCases) {
            $i++
            $selRoot = Join-Path $sandbox "selectroot$i"
            New-Item -ItemType Directory -Force -Path (Join-Path $selRoot 'data') | Out-Null
            Set-Content -LiteralPath (Join-Path $selRoot 'data\compute-devices-seen.json') -Encoding UTF8 -Value '{"GPU-p620":{"name":"Quadro P620","memory_mib":4096,"last_seen":"2026-09-29"},"GPU-4090":{"name":"NVIDIA GeForce RTX 4090","memory_mib":24564,"pci_dev":"2684","last_seen":"2026-09-29"}}'
            $invFake = $lc.Inv; $pnpFake = $lc.Pnp; $pick = $lc.Pick
            $listing = & {
                $Root = $selRoot
                function Get-QwenGpuInventory { $invFake }
                function Get-QwenNvidiaPnpDevices { return ,@($pnpFake) }
                function Get-QwenGpuExternalFlags { @{} }
                . ([scriptblock]::Create($fnText))
                # (as the script itself does: assigned first - it returns its list with a comma)
                $list = Get-ComputeDevices
                $list = @($list)
                $err = $null
                try { [void](Resolve-DeviceArgs $list @($pick)) } catch { $err = $_.Exception.Message }
                [pscustomobject]@{ List = $list; Err = $err }
            }
            $unav = @($listing.List | Where-Object { $_.Kind -eq 'unavailable' })
            $abs = @($listing.List | Where-Object { $_.Kind -eq 'absent' })
            $gpuRows = @($listing.List | Where-Object { $_.Kind -eq 'gpu' })
            $names = @($unav | ForEach-Object { $_.Name })
            $ok = ($names.Count -eq $lc.Unav.Count) -and (@($names | Where-Object { $_ -isnot [string] }).Count -eq 0) -and
                  (@($lc.Unav | Where-Object { $names -notcontains $_ }).Count -eq 0) -and ($abs.Count -eq $lc.Absent) -and ($gpuRows.Count -eq $lc.Gpu) -and
                  ($(if ($lc.Refused) { $listing.Err -like '*cannot be used*' } else { -not $listing.Err }))
            if ($lc.AbsentLike) { $ok = $ok -and (@($abs | Where-Object { $_.Detail -like $lc.AbsentLike }).Count -eq 1) }
            $detail = 'unavailable=[{0}] absent=[{1}] gpu={2} choosing {3}: {4}' -f ($names -join '; '), (($abs | ForEach-Object { '{0} - {1}' -f $_.Name, $_.Detail }) -join '; '), $gpuRows.Count, $pick, $(if ($listing.Err) { $listing.Err } else { 'accepted' })
            if ($ok) { Say OK "device list, $($lc.Name): $detail" } else { Say FAIL "device list, $($lc.Name): $detail" }
        }
    } catch { Say FAIL ('device list test could not run: ' + $_.Exception.Message) }

    $log = Join-Path $sandbox 'server.log'
    Set-Content -LiteralPath $log -Encoding UTF8 -Value @(
        'time=x level=INFO msg="server config" env="map[CUDA_VISIBLE_DEVICES:GPU-4090 OLLAMA_LLM_LIBRARY:cuda_v12]"',
        'time=x level=INFO msg="inference compute" id=0 library=CUDA compute=8.9 name=CUDA0 description="NVIDIA GeForce RTX 4090" libdirs=ollama,cuda_v12 total="24.0 GiB" available="22.9 GiB"',
        'time=x level=INFO msg="vram-based default context" total_vram="24.0 GiB" default_num_ctx=32768')
    $f1 = Get-QwenOllamaLogFacts -LogPath $log
    Set-Content -LiteralPath $log -Encoding UTF8 -Value @('msg="inference compute" id=cpu library=cpu description=cpu total="63.6 GiB"')
    $f2 = Get-QwenOllamaLogFacts -LogPath $log
    if ($f1.Discovered -and @($f1.GpuLines).Count -eq 1 -and $f1.GpuLines[0].Description -eq 'NVIDIA GeForce RTX 4090' -and -not $f1.ZeroVram -and $f2.Discovered -and @($f2.GpuLines).Count -eq 0) {
        Say OK 'server log reading tells "found the RTX" from "CPU only"'
    } else { Say FAIL 'server log reading' }
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

function Show-Devices {
    Write-Host "`n== NVIDIA devices (Windows) ==" -ForegroundColor Cyan
    $pnp = Get-QwenNvidiaPnpDevices
    if ($null -eq $pnp) { Say WARN 'the Windows device query failed' }
    elseif (@($pnp).Count -eq 0) { Say INFO 'no NVIDIA display device present' }
    # Path B = a kernel driver of the 590 branch or later: it cannot run the P620.
    $pathB = (Get-QwenNvidiaKernelDriver).Major -ge 590
    foreach ($d in @($pnp)) {
        if (-not $d) { continue }
        # The Quadro P620 (DEV_1CBD) runs only on the R580 branch; path B keeps it disabled.
        $p620 = $pathB -and ((Get-QwenPciDeviceId ([string]$d.PnpId)) -eq '1CBD')
        $pathBAdvice = 'the current NVIDIA driver does not support the P620: disable it in Device Manager (path B); do NOT install the Dell driver for it - that would break the RTX'
        $hint = switch ($d.ErrorCode) {
            0 { if ($d.Service -ieq 'BasicDisplay') { if ($p620) { "NO NVIDIA DRIVER - $pathBAdvice" } else { 'NO NVIDIA DRIVER (Microsoft Basic Display) - install an NVIDIA driver whose INF lists this card' } } else { 'working' } }
            12 { 'Code 12: not enough PCI resources - attach only one eGPU at a time' }
            22 { if ($p620) { 'Code 22: disabled in Device Manager (path B - intended)' } else { 'Code 22: disabled in Device Manager' } }
            28 { if ($p620) { "Code 28: no driver - $pathBAdvice" } else { 'Code 28: no driver installed' } }
            43 { if ($p620) { "Code 43 - $pathBAdvice" } else { 'Code 43: the driver stopped the device - usually a driver branch that does not support this GPU' } }
            default { "Code $($d.ErrorCode)" }
        }
        $level = if ($d.ErrorCode -eq 0 -and $d.Service -ine 'BasicDisplay') { 'OK' } elseif ($d.ErrorCode -eq 22) { if ($p620) { 'OK' } else { 'INFO' } } else { 'WARN' }
        Say $level ('{0} [{1}] {2}' -f $d.Name, $d.Service, $hint)
    }
    # Only meaningful where the nView hook was deliberately disabled (renamed to .bak)
    # because it crashed Chrome; elsewhere nView may be in use on purpose.
    $nview = Join-Path ${env:ProgramFiles} 'NVIDIA Corporation\nview'
    if (Test-Path -LiteralPath (Join-Path $nview 'nviewH64.dll.bak')) {
        $hooks = @(Get-ChildItem -LiteralPath $nview -Filter 'nviewH*.dll' -ErrorAction SilentlyContinue)
        if ($hooks.Count -gt 0) { Say WARN ('a driver install put the nView hook back (' + ($hooks.Name -join ', ') + ') - it crashed Chrome on this machine; rename it to .bak again') }
        else { Say OK 'nView hook still disabled (Chrome crash fix in place)' }
    }

    Write-Host "`n== nvidia-smi ==" -ForegroundColor Cyan
    $q = Invoke-QwenNvidiaSmi -Arguments '--query-gpu=name,uuid,memory.total,memory.used,compute_cap,pcie.link.gen.current,pcie.link.width.current,driver_version --format=csv,noheader'
    if ($null -eq $q) { Say INFO 'nvidia-smi not installed' }
    elseif ($q.ExitCode -ne 0) { Say INFO ('nvidia-smi: ' + ([string]$q.Output + ' ' + [string]$q.Error).Trim()) }
    else {
        foreach ($line in ([string]$q.Output -split "`r?`n" | Where-Object { $_.Trim() })) {
            Say INFO $line.Trim()
            if (-not $script:LiveSmiDriver) { $script:LiveSmiDriver = (($line -split ',')[-1]).Trim() }
        }
    }
}

function Show-Driver {
    # Path A = the R580 branch, which still drives the P620 (Dell's 581.95 here); path B = the
    # latest GeForce driver for the RTX cards with the P620 disabled. Every NVIDIA device runs
    # on the ONE kernel driver the nvlddmkm service points to, so a driver installed for any
    # device (Windows Update gave the P620 Dell's package before) switches all of them.
    Write-Host "`n== NVIDIA driver (path A: R580 with the P620 / path B: latest for the RTX, P620 disabled) ==" -ForegroundColor Cyan
    $kd = Get-QwenNvidiaKernelDriver
    if (-not $kd.Image) { Say INFO 'no NVIDIA kernel driver installed'; return }
    $package = $kd.Package
    $kernelWin = $kd.WindowsVersion
    $kernel = $kd.Version
    $major = $kd.Major
    $branch = if ($major) { 'R{0}' -f ([math]::Floor($major / 5) * 5) } else { 'branch unknown' }
    $pathB = $major -ge 590
    $what = if (-not $major) { 'version unknown' } elseif ($pathB) { 'RTX 20 series and newer only - path B: the P620 must stay disabled' } else { 'still supports the Quadro P620 (path A)' }
    Say INFO ('kernel driver used by every NVIDIA device: {0} ({1}, {2}) - {3}' -f $(if ($kernel) { $kernel } else { $kernelWin }), $branch, $package, $what)

    $pnp = Get-QwenNvidiaPnpDevices
    foreach ($d in @($pnp)) {
        if (-not $d) { continue }
        $props = @{}
        try {
            Get-PnpDeviceProperty -InstanceId $d.PnpId -KeyName 'DEVPKEY_Device_DriverVersion', 'DEVPKEY_Device_DriverInfPath' -ErrorAction Stop |
                ForEach-Object { $props[[string]$_.KeyName] = [string]$_.Data }
        } catch { }
        $ver = ConvertTo-QwenNvidiaVersion ([string]$props['DEVPKEY_Device_DriverVersion'])
        $verMajor = if ($ver) { [int](($ver -split '\.')[0]) } else { 0 }
        $inf = [string]$props['DEVPKEY_Device_DriverInfPath']
        $bound = if ($ver) { "driver $ver ($inf)" } elseif ($inf) { $inf } else { 'no driver' }
        $isP620 = (Get-QwenPciDeviceId ([string]$d.PnpId)) -eq '1CBD'
        if ($d.ErrorCode -eq 22) { Say INFO ('{0}: disabled, still bound to {1} - harmless while it stays disabled and blocked' -f $d.Name, $bound); continue }
        if ($d.Service -ine 'nvlddmkm') { Say INFO ('{0}: no NVIDIA driver ({1})' -f $d.Name, $(if ($d.Service) { $d.Service } else { 'none' })); continue }
        if ($isP620 -and $pathB) {
            Say WARN ('{0}: bound to {1} under the {2} kernel driver, which does not support it - disable it in Device Manager (path B); do NOT reinstall its driver, that would switch every NVIDIA device back to R580' -f $d.Name, $bound, $kernel)
        } elseif ($ver -and $kernel -and (($verMajor -ge 590) -ne ($major -ge 590))) {
            # Across the 590 line the kernel cannot run this card (R580 knows no card newer
            # than its INF; R590+ knows no Pascal): something installed the other generation.
            Say WARN ('{0}: installed for {1}, but the kernel driver running is {2} - another NVIDIA package was installed over it (Windows Update or a Dell tool?). Reinstall the NVIDIA driver for this card, then run Restart-QwenOllama.ps1' -f $d.Name, $bound, $kernel)
        } elseif ($ver -and $kernel -and $ver -ne $kernel) {
            Say INFO ('{0}: {1}; the kernel driver running is {2} (same generation - both cards work)' -f $d.Name, $bound, $kernel)
        } else { Say INFO ('{0}: {1}' -f $d.Name, $bound) }
    }

    # NVIDIA display packages in the driver store (pnputil works without elevation). On
    # path B an R580 package left there is what Windows would put back on the P620.
    $store = @()
    try {
        $block = @{}
        foreach ($line in (@(& pnputil.exe /enum-drivers 2>$null) + '')) {
            if ([string]$line -match '^\s*$') {
                if ($block.Count -and $block['Provider Name'] -eq 'NVIDIA' -and [string]$block['Class Name'] -match 'Display') { $store += [pscustomobject]$block }
                $block = @{}
            } elseif ([string]$line -match '^\s*([^:]+?):\s+(.*)$') { $block[$Matches[1].Trim()] = $Matches[2].Trim() }
        }
    } catch { }
    if ($store.Count -eq 0) { Say INFO 'driver store: no NVIDIA display package could be read from pnputil (its output may be in another language)' }
    $old = 0
    foreach ($s in $store) {
        $v = ConvertTo-QwenNvidiaVersion ([string]$s.'Driver Version')
        $vMajor = if ($v) { [int](($v -split '\.')[0]) } else { 0 }
        $isOld = $pathB -and $vMajor -gt 0 -and $vMajor -lt 590
        if ($isOld) { $old++ }
        Say INFO ('driver store: {0} ({1}) {2}{3}' -f $s.'Published Name', $s.'Original Name', $v, $(if ($isOld) { ' - an R580 package (the P620 driver); keep it only while Windows is blocked from installing it' } else { '' }))
    }

    # Is Windows kept from reinstalling the P620 driver? Primary guard: Device Installation
    # Restrictions with the P620's IDs (gpedit, see README "path B").
    $restr = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceInstall\Restrictions' -ErrorAction SilentlyContinue
    $deny = @()
    if ($restr -and $restr.DenyDeviceIDs -eq 1) {
        $k = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceInstall\Restrictions\DenyDeviceIDs' -ErrorAction SilentlyContinue
        if ($k) { $deny = @($k.PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { [string]$_.Value }) }
    }
    $blocksP620 = @($deny | Where-Object { $_ -like 'PCI\VEN_10DE&DEV_1CBD*' }).Count -gt 0
    $tooBroad = @($deny | Where-Object { $_ -match '^PCI\\VEN_10DE$|^PCI\\VEN_10DE&CC_|^PCI\\CC_03' }).Count -gt 0
    if ($tooBroad) { Say WARN 'the device-install block lists a generic NVIDIA or display-class ID - it can block the RTX or the Intel GPU; keep only the PCI\VEN_10DE&DEV_1CBD entries' }
    if ($restr -and $restr.DenyDeviceIDsRetroactive -eq 1) { Say WARN "the device-install block has 'Also apply to matching devices that are already installed' ticked - untick it" }
    $p620Dev = @($pnp | Where-Object { $_ -and [string]$_.PnpId -like 'PCI\VEN_10DE&DEV_1CBD*' }) | Select-Object -First 1
    if ($pathB -or ($p620Dev -and $p620Dev.ErrorCode -eq 22)) {
        $wuNoDrivers = (Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -ErrorAction SilentlyContinue).ExcludeWUDriversInQualityUpdate -eq 1
        if ($blocksP620) { Say OK 'Windows may not install any driver for the P620 (Device Installation Restrictions): the old driver cannot come back' }
        elseif ($p620Dev) {
            $extra = if ($wuNoDrivers) { ' Windows Update drivers are off, but a Dell tool still could.' } else { '' }
            Say WARN ('nothing blocks a driver install for the P620: if Windows Update or a Dell tool puts the Dell R580 driver back, every NVIDIA device switches to it and the RTX stops working.{0} Add the device-ID block (README: path B)' -f $extra)
        }
        $dell = @(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
            Where-Object { [string]$_.DisplayName -match '^Dell (Command \| Update|SupportAssist)' } | ForEach-Object { [string]$_.DisplayName } | Select-Object -Unique)
        if ($dell.Count -gt 0) { Say INFO ('Dell update tools installed: {0} - keep video drivers out of their automatic updates' -f ($dell -join ', ')) }
        if ($p620Dev -and $p620Dev.ErrorCode -ne 22 -and $pathB) { Say WARN 'the P620 is not disabled - disable it in Device Manager (Display adapters > NVIDIA Quadro P620 > Disable device)' }
    }
}

function Show-Profile {
    Write-Host "`n== GPU profile the server was started with ==" -ForegroundColor Cyan
    $record = Read-QwenGpuProfile -Root $Root
    if (-not $record) { Say WARN 'no data\gpu-profile.json - the running server was not started through the GPU step; run Restart-QwenOllama.ps1'; return $null }
    Say INFO ('profile={0} mode={1} vram={2} (expected {3}) written {4} by {5}' -f $record.profile, $record.mode, $record.vram_class, $record.vram_expected, $record.written_at, $record.launcher)
    foreach ($part in @(([string]$record.note) -split ' \| ' | Where-Object { $_ })) {
        # "CPU only because the P620 is disabled" is the intended path-B state, not a problem.
        $lvl = if ($part -like '*disabled in Device Manager) - Ollama runs on the CPU*') { 'INFO' } else { 'WARN' }
        Say $lvl ('note: ' + $part)
    }
    foreach ($g in @($record.gpus_used)) {
        if (-not $g) { continue }
        $where = if ($g.external -eq $true) { 'removable/eGPU' } elseif ($g.external -eq $false) { 'built in' } else { 'removable: unknown' }
        Say OK ('using {0} ({1} MiB, cc {2}, driver {3}, {4}, {5})' -f $g.name, $g.memory_mib, $g.compute_cap, $g.driver, $where, $g.uuid)
        if ($script:LiveSmiDriver -and $g.driver -and [string]$g.driver -ne $script:LiveSmiDriver) { Say INFO ('the server started under driver {0}; the driver is now {1} - run Restart-QwenOllama.ps1 to use it' -f $g.driver, $script:LiveSmiDriver) }
        if ($g.external -ne $true) { Say INFO 'the watcher only restarts after sleep for a card Windows reports as removable' }
    }
    foreach ($g in @($record.gpus_ignored)) { if ($g) { Say INFO ('ignoring {0}: {1}' -f $g.name, $g.reason) } }
    foreach ($p in @($record.nvidia_problem_devices)) { if ($p) { Say WARN ('NVIDIA device without a working driver: {0} service={1} code={2}' -f $p.name, $p.service, $p.error_code) } }
    foreach ($p in @($record.nvidia_disabled_devices)) { if ($p) { Say INFO ('disabled in Device Manager: {0}' -f $p.name) } }
    if ($record.nvidia_smi -and $null -ne $record.nvidia_smi.exit_code -and $record.nvidia_smi.exit_code -ne 0) {
        # With no NVIDIA card enabled at start (path B without an eGPU) nvidia-smi has nothing to list.
        $workingAtStart = @(([string]$record.pnp_signature) -split ';' | Where-Object { $f = @($_ -split '\|'); $f.Count -ge 3 -and $f[1] -ieq 'nvlddmkm' -and $f[2] -eq '0' }).Count
        if ($workingAtStart -gt 0 -or [string]$record.pnp_signature -eq 'unknown') { Say WARN ('nvidia-smi failed at start (exit {0}): {1}' -f $record.nvidia_smi.exit_code, $record.nvidia_smi.error) }
        else { Say INFO ('nvidia-smi at start: {0} (expected - no NVIDIA card was enabled)' -f $record.nvidia_smi.error) }
    }
    if ($record.inventory_ok -eq $false) { Say WARN 'nvidia-smi listed fewer cards than Windows shows working - the watcher retries once; or run Restart-QwenOllama.ps1' }
    if ($record.vram_class -eq 'pending') { Say INFO 'vram_class is pending: the watcher confirms from the server log within about a minute that the card was found' }
    elseif ($record.vram_expected -ne 'none' -and $record.vram_class -eq 'none') { Say FAIL 'the server did not get the eGPU - driver problem, or Ollama issue #18581 (card seen with 0 B): set User QWEN_GPU_MODE=vulkan (P620 disabled) and run Restart-QwenOllama.ps1' }
    $owner = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -First 1
    if ([string]$record.pnp_signature -eq 'unknown') { Say WARN 'the device list could not be read when the server started - the watcher restarts once to get a baseline' }
    if ($owner) {
        $parent = $null
        try { $parent = Get-CimInstance Win32_Process -Filter ("ProcessId=" + (Get-CimInstance Win32_Process -Filter "ProcessId=$owner").ParentProcessId) } catch { }
        if ($parent -and $parent.Name -ieq 'ollama app.exe') { Say WARN 'the server belongs to the Ollama tray app, which ignores the GPU step - remove the tray app from Startup and run Restart-QwenOllama.ps1 -StartIfStopped after quitting it' }
    }
    if (-not $owner) { Say WARN 'Ollama is not running' }
    elseif ([string]$record.server_pid -eq [string]$owner) { Say OK "running server (pid $owner) was started with this profile" }
    else { Say WARN "running server (pid $owner) was NOT started with this profile (recorded pid $($record.server_pid)); the watcher restarts it when idle, or run Restart-QwenOllama.ps1" }
    $current = Get-QwenNvidiaPnpSignature
    if ($null -ne $current -and [string]$record.pnp_signature -ne 'unknown' -and $current -ne [string]$record.pnp_signature) { Say WARN 'NVIDIA devices changed since the server started - the watcher restarts it within a few minutes once idle, or run Restart-QwenOllama.ps1' }
    $watch = $null
    if ([System.Threading.Mutex]::TryOpenExisting($script:QwenGpuWatchMutexName, [ref]$watch)) { $watch.Dispose(); Say OK 'GPU watcher is running' } else { Say WARN 'GPU watcher is not running - it starts with any launcher; run Restart-QwenOllama.ps1 (log: logs\ollama-gpu-watch.log)' }
    return $record
}

function Get-ServerLogPath($record) {
    # The log that belongs to the running server: the record names it; otherwise the newer
    # of the two launchers' logs.
    if ($record -and $record.server_log -and (Test-Path -LiteralPath ([string]$record.server_log))) { return [string]$record.server_log }
    $candidates = @('ollama-server.log', 'ollama-error.log') | ForEach-Object { Join-Path $Root "logs\$_" } | Where-Object { Test-Path -LiteralPath $_ }
    return ($candidates | Sort-Object { (Get-Item -LiteralPath $_).LastWriteTime } -Descending | Select-Object -First 1)
}

function Show-Choice {
    Write-Host "`n== Processing unit chosen (Select-QwenCompute.ps1 / http://127.0.0.1:8765/compute) ==" -ForegroundColor Cyan
    $sel = Read-QwenComputeSelection -Root $Root
    $key = Get-QwenSelectionKey $sel
    $record = Read-QwenGpuProfile -Root $Root
    $have = if ($record -and $record.selection_key) { [string]$record.selection_key } else { 'auto' }
    $until = if ($sel -and $sel.expires_at -and $key -ne 'auto') { " until $($sel.expires_at)" } else { '' }
    Say INFO ("chosen: {0}{1}" -f $key, $until)
    if ($have -ne $key) { Say WARN "the running server was started with '$have'; the watcher switches it once Ollama is idle" }
    $placementPath = Join-Path $Root 'data\model-placement.json'
    if (Test-Path -LiteralPath $placementPath) {
        try {
            $pl = Get-Content -LiteralPath $placementPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $tags = @{}
            foreach ($m in @((Invoke-RestMethod "$Api/api/tags" -TimeoutSec 10).models)) { $tags[[string]$m.name] = 'sha256:' + [string]$m.digest }
            foreach ($p in @($pl.models.PSObject.Properties)) {
                if ($p.Value.placement -ne 'cpu') { continue }
                if ($p.Value.pending) { Say WARN ("{0}: a placement change was interrupted - run Set-QwenModelPlacement.ps1 -Model {0} -Placement auto to restore the original" -f $p.Name) }
                elseif ($tags[$p.Name] -eq [string]$p.Value.pinned_digest) { Say INFO ("{0}: kept on the CPU for every caller" -f $p.Name) }
                else { Say WARN ("{0}: the CPU pin was undone (model re-pulled or reinstalled) - set it again with -Placement cpu, or clear this with -Placement auto" -f $p.Name) }
            }
        } catch { Say WARN ('could not check model placement: ' + $_.Exception.Message) }
    }
}

function Show-ServerLog($record) {
    Write-Host "`n== Ollama server log ==" -ForegroundColor Cyan
    $log = Get-ServerLogPath $record
    if (-not $log) { Say INFO 'no server log found'; return }
    $facts = Get-QwenOllamaLogFacts -LogPath $log
    Say INFO ("log: $log (last written $($facts.LastWrite))")
    # A log last written before the running server started belongs to an earlier server.
    try {
        $owner = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -First 1
        $started = if ($owner) { (Get-Process -Id $owner -ErrorAction Stop).StartTime } else { $null }
        if ($started -and $facts.LastWrite -lt $started) { Say WARN 'this log was last written before the running server started - it belongs to an earlier server' }
    } catch { }
    if ($facts.Config) {
        foreach ($key in 'CUDA_VISIBLE_DEVICES', 'OLLAMA_LLM_LIBRARY', 'OLLAMA_VULKAN') {
            if ($facts.Config -match "$key`:([^ \]]*)") { Say INFO ("server saw {0}={1}" -f $key, $Matches[1]) }
        }
    }
    foreach ($g in @($facts.GpuLines)) {
        # The internal card is expected only when no eGPU is in use (profile internal/all).
        $level = if ($g.Description -match 'P620') { if ($record -and $record.profile -in @('gpu', 'cpu')) { 'FAIL' } else { 'INFO' } } else { 'OK' }
        Say $level ("inference device: {0} ({1}, {2})" -f $g.Description, $g.Library, $g.Total)
    }
    if (@($facts.GpuLines).Count -eq 0) {
        if ($record -and $record.profile -eq 'gpu' -and $facts.Discovered) { Say FAIL 'profile says GPU but the server found none - check the driver (Device Manager), or Ollama issue #18581: disable the P620, set User QWEN_GPU_MODE=vulkan and run Restart-QwenOllama.ps1' }
        elseif ($facts.Discovered) { Say OK 'server runs CPU-only (no GPU inference device)' }
        else { Say INFO 'no device discovery line in the log yet' }
    }
    if ($facts.ZeroVram -and $record -and $record.profile -eq 'gpu') { Say FAIL 'the server saw the card with 0 B of VRAM (Ollama issue #18581) - disable the P620, set User QWEN_GPU_MODE=vulkan and run Restart-QwenOllama.ps1' }
    $tail = @(Get-Content -LiteralPath $log -Tail 3000 -ErrorAction SilentlyContinue)
    $offload = $tail | Where-Object { $_ -match 'offloaded \d+/\d+ layers to GPU' } | Select-Object -Last 1
    if ($offload) { Say INFO ('last model load: ' + ($offload -replace '.*(offloaded \d+/\d+ layers to GPU).*', '$1')) }
    foreach ($pattern in 'no kernel image is available', 'CUDA error', 'failure during llama-server GPU discovery', 'NVIDIA driver too old', 'out of memory') {
        $hit = $tail | Where-Object { $_ -match [regex]::Escape($pattern) } | Select-Object -Last 1
        if ($hit) { Say WARN ("log contains '{0}': {1}" -f $pattern, $hit.Trim()) }
    }
}

function Show-Loaded {
    Write-Host "`n== Loaded models ==" -ForegroundColor Cyan
    try {
        $ps = Invoke-RestMethod "$Api/api/ps" -TimeoutSec 10
        if (@($ps.models).Count -eq 0) { Say INFO 'no model loaded' }
        foreach ($m in @($ps.models)) {
            $share = if ($m.size -gt 0) { [math]::Round(100.0 * $m.size_vram / $m.size) } else { 0 }
            Say INFO ('{0}: {1:N1} GB, {2}% in VRAM, ctx {3}' -f $m.name, ($m.size / 1GB), $share, $m.context_length)
        }
    } catch { Say WARN ('Ollama API not reachable: ' + $_.Exception.Message) }
}

function Invoke-Bench {
    Write-Host "`n== Benchmark: $Model ==" -ForegroundColor Cyan
    $para = 'Section {0}. The charging station controller reports its state every few seconds. Each report carries the connector status, the negotiated current limit, the measured voltage and current, the energy delivered so far, and any fault codes raised by the power modules. Operators compare these values against the expected profile for the vehicle and flag deviations above five percent for manual review before the next shift. '
    $body = -join (1..14 | ForEach-Object { $para -f $_ })
    $results = @()
    foreach ($run in 1..3) {
        # A unique first line defeats the prompt cache so every run pays the full prefill.
        $prompt = "Run $run $([guid]::NewGuid().ToString('N')).`n$body`nIn three sentences, summarise what operators must check."
        $payload = @{ model = $Model; prompt = $prompt; stream = $false; think = $false; keep_alive = '2h'
                      options = @{ num_ctx = 32768; temperature = 0; seed = 7; num_predict = $(if ($run -eq 1) { 1 } else { 96 }) } } | ConvertTo-Json -Depth 4
        try {
            $r = Invoke-RestMethod "$Api/api/generate" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -ContentType 'application/json' -TimeoutSec 3600
        } catch { Say FAIL ('request failed: ' + $_.Exception.Message); return }
        if ($run -eq 1) { Say INFO ('load {0:N1} s (the first request after a restart includes loading the model)' -f ($r.load_duration / 1e9)); continue }
        $pre = $r.prompt_eval_count / [math]::Max($r.prompt_eval_duration / 1e9, 0.001)
        $gen = $r.eval_count / [math]::Max($r.eval_duration / 1e9, 0.001)
        $results += [pscustomobject]@{ prefill = $pre; gen = $gen }
        Say INFO ('run {0}: prompt {1} tok at {2:N1} tok/s, answer {3} tok at {4:N2} tok/s' -f ($run - 1), $r.prompt_eval_count, $pre, $r.eval_count, $gen)
    }
    $rec = Read-QwenGpuProfile -Root $Root
    $ref = if ($rec -and $rec.profile -eq 'gpu') { 'no reference yet for this card - log it: logs\gpu-bench.jsonl' }
           elseif ($rec -and ($rec.cpu_only -eq $true -or $rec.profile -eq 'cpu')) { 'reference, this laptop CPU only: prompt 4.9-6.1, answer 1.9' }
           else { 'reference, this laptop with the P620 helping (path A): prompt 21.2, answer 1.66' }
    Say OK ('median prompt {0:N1} tok/s, answer {1:N2} tok/s ({2})' -f `
        (($results.prefill | Sort-Object)[[int][math]::Floor($results.Count / 2)]), (($results.gen | Sort-Object)[[int][math]::Floor($results.Count / 2)]), $ref)
    $log = @{ at = (Get-Date).ToString('o'); model = $Model; profile = (Read-QwenGpuProfile -Root $Root); runs = $results } | ConvertTo-Json -Depth 6 -Compress
    Add-Content -LiteralPath (Join-Path $Root 'logs\gpu-bench.jsonl') -Value $log -Encoding UTF8
    Show-Loaded
}

function Invoke-EvictionCheck {
    Write-Host "`n== Does an embedding call evict $Model? ==" -ForegroundColor Cyan
    $before = @((Invoke-RestMethod "$Api/api/ps" -TimeoutSec 10).models | ForEach-Object { $_.name })
    if ($before -notcontains $Model) { Say WARN "$Model is not loaded; run with -Bench first"; return }
    $payload = @{ model = 'qwen3-embedding:4b'; input = 'eviction check'; keep_alive = '5m' } | ConvertTo-Json
    try { Invoke-RestMethod "$Api/api/embed" -Method Post -Body $payload -ContentType 'application/json' -TimeoutSec 600 | Out-Null }
    catch { Say FAIL ('embedding request failed: ' + $_.Exception.Message); return }
    $after = @((Invoke-RestMethod "$Api/api/ps" -TimeoutSec 10).models | ForEach-Object { $_.name })
    if ($after -contains $Model) { Say OK "$Model stayed loaded next to the embedding model" }
    else { Say WARN "$Model was evicted by the embedding model - every RAG lookup will reload it. Fix: rebuild qwen3-embedding:4b with 'PARAMETER num_gpu 0' so it runs on the CPU" }
}

if ($SelfTest) {
    Write-Host '== Self-test: GPU decision against simulated hardware ==' -ForegroundColor Cyan
    Invoke-SelfTest
} else {
    Show-Devices
    Show-Driver
    $record = Show-Profile
    Show-Choice
    Show-ServerLog $record
    Show-Loaded
    if ($Bench) { Invoke-Bench }
    if ($EvictionCheck) { Invoke-EvictionCheck }
}
Write-Host ''
if ($script:Failures -gt 0) { Write-Host "$($script:Failures) failure(s), $($script:Warnings) warning(s)" -ForegroundColor Red; exit 1 }
Write-Host "no failures, $($script:Warnings) warning(s)" -ForegroundColor Green
exit 0
