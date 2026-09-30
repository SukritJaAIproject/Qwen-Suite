# Desktop launcher for the Codex-Qwen bridge (the Start Menu / desktop shortcuts).
#  - Default: a small window to type a coding job and pick the folder, job type and model.
#    The job then runs hidden (Invoke-QwenCodex.ps1 -Detach): it waits for a free Ollama,
#    runs, and the notifier reports when it is done.
#  - -Tui: pick a folder, then open Codex's interactive TUI in a new console window.
# Nothing here talks to a model; Invoke-QwenCodex.ps1 does the checks and the waiting.

param(
    [switch]$Tui,
    [string]$Cwd = ''
)

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Invoke = Join-Path $PSScriptRoot 'Invoke-QwenCodex.ps1'
$ToolsDir = Split-Path -Parent $PSScriptRoot
$Root = if ($env:QWEN_CODEX_ROOT) { $env:QWEN_CODEX_ROOT.TrimEnd('\') } else { Split-Path -Parent $ToolsDir }
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$PrefsPath = Join-Path $Root 'data\codex-bridge-ui.json'
$SettingsPath = Join-Path $Root 'data\codex-bridge.json'
$Shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Caption = 'Qwen Codex'
$UiFont = New-Object System.Drawing.Font('Leelawadee UI', 10)

function Read-JsonFile([string]$Path) {
    try { return ([System.IO.File]::ReadAllText($Path, $Utf8) | ConvertFrom-Json) } catch { return $null }
}

function Save-Prefs([string]$Folder, [string]$Model, [int]$KindIndex) {
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PrefsPath) | Out-Null
        [System.IO.File]::WriteAllText($PrefsPath, (@{ last_cwd = $Folder; last_model = $Model; last_kind = $KindIndex } | ConvertTo-Json), $Utf8)
    } catch { }
}

function Show-Message([string]$Text, [string]$Icon = 'Information') {
    [void][System.Windows.Forms.MessageBox]::Show($Text, $Caption, 'OK', $Icon)
}

function Select-Folder([string]$Initial) {
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = 'เลือกโฟลเดอร์งานของ Codex'
    $dlg.ShowNewFolderButton = $true
    if ($Initial -and (Test-Path -LiteralPath $Initial)) { $dlg.SelectedPath = $Initial }
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.SelectedPath }
    return $null
}

$prefs = Read-JsonFile $PrefsPath
$settings = Read-JsonFile $SettingsPath
$writeMode = 'disabled'
if ($settings -and $settings.PSObject.Properties['write_mode']) { $writeMode = [string]$settings.write_mode }
$startDir = $Cwd
if (-not $startDir -and $prefs -and $prefs.PSObject.Properties['last_cwd'] -and $prefs.last_cwd -and (Test-Path -LiteralPath $prefs.last_cwd)) { $startDir = [string]$prefs.last_cwd }
if (-not $startDir -and (Test-Path -LiteralPath 'E:\qwen-projects')) { $startDir = 'E:\qwen-projects' }

# ------------------------------------------------------------------------------ TUI
if ($Tui) {
    $folder = if ($Cwd) { $Cwd } else { Select-Folder $startDir }
    if (-not $folder) { exit 0 }
    $folder = $folder.TrimEnd('\')
    Save-Prefs $folder '' 0
    # A visible console that stays open, so Codex's screen and any refusal stay readable.
    $argLine = '-NoProfile -ExecutionPolicy Bypass -NoExit -File "{0}" -Tui -Cwd "{1}"' -f $Invoke, $folder
    Start-Process -FilePath $Shell -ArgumentList $argLine -WorkingDirectory $folder
    exit 0
}

# ------------------------------------------------------------------------ job window
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Qwen Codex - สั่งงานเบื้องหลัง'
$form.Font = $UiFont
$form.ClientSize = New-Object System.Drawing.Size(600, 470)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.TopMost = $true
$icon = Join-Path $Root 'qwen-suite.ico'
if (Test-Path -LiteralPath $icon) { try { $form.Icon = New-Object System.Drawing.Icon($icon) } catch { } }

$lblPrompt = New-Object System.Windows.Forms.Label
$lblPrompt.Text = 'สั่งงานเขียนโค้ด (ทำงานเบื้องหลัง เสร็จแล้วแจ้งเตือน)'
$lblPrompt.Location = New-Object System.Drawing.Point(15, 12)
$lblPrompt.AutoSize = $true
$form.Controls.Add($lblPrompt)

$txtPrompt = New-Object System.Windows.Forms.TextBox
$txtPrompt.Multiline = $true
$txtPrompt.ScrollBars = 'Vertical'
$txtPrompt.AcceptsReturn = $true
$txtPrompt.Location = New-Object System.Drawing.Point(15, 40)
$txtPrompt.Size = New-Object System.Drawing.Size(570, 170)
$form.Controls.Add($txtPrompt)

$lblFolder = New-Object System.Windows.Forms.Label
$lblFolder.Text = 'โฟลเดอร์งาน'
$lblFolder.Location = New-Object System.Drawing.Point(15, 225)
$lblFolder.AutoSize = $true
$form.Controls.Add($lblFolder)

$txtFolder = New-Object System.Windows.Forms.TextBox
$txtFolder.Location = New-Object System.Drawing.Point(15, 250)
$txtFolder.Size = New-Object System.Drawing.Size(470, 28)
$txtFolder.Text = $startDir
$form.Controls.Add($txtFolder)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Text = 'เลือก...'
$btnBrowse.Location = New-Object System.Drawing.Point(495, 248)
$btnBrowse.Size = New-Object System.Drawing.Size(90, 30)
$btnBrowse.Add_Click({
        $picked = Select-Folder $txtFolder.Text
        if ($picked) { $txtFolder.Text = $picked }
    })
$form.Controls.Add($btnBrowse)

$lblKind = New-Object System.Windows.Forms.Label
$lblKind.Text = 'ประเภทงาน'
$lblKind.Location = New-Object System.Drawing.Point(15, 292)
$lblKind.AutoSize = $true
$form.Controls.Add($lblKind)

$cmbKind = New-Object System.Windows.Forms.ComboBox
$cmbKind.DropDownStyle = 'DropDownList'
$cmbKind.Location = New-Object System.Drawing.Point(120, 288)
$cmbKind.Size = New-Object System.Drawing.Size(465, 28)
$writeLabel = 'สั่งงาน - แก้ไขไฟล์และรันโปรแกรมได้'
if ($writeMode -notin @('windows-sandbox', 'full-access')) { $writeLabel += ' (ยังไม่เปิดใช้)' }
[void]$cmbKind.Items.Add('สั่งงาน - อ่านและวิเคราะห์อย่างเดียว (ปลอดภัย)')
[void]$cmbKind.Items.Add($writeLabel)
[void]$cmbKind.Items.Add('รีวิวโค้ดที่ยังไม่ commit (git repo)')
$cmbKind.SelectedIndex = 0
if ($prefs -and $prefs.PSObject.Properties['last_kind'] -and [int]$prefs.last_kind -in 0..2) { $cmbKind.SelectedIndex = [int]$prefs.last_kind }
$form.Controls.Add($cmbKind)

$lblModel = New-Object System.Windows.Forms.Label
$lblModel.Text = 'โมเดล'
$lblModel.Location = New-Object System.Drawing.Point(15, 332)
$lblModel.AutoSize = $true
$form.Controls.Add($lblModel)

$cmbModel = New-Object System.Windows.Forms.ComboBox
$cmbModel.DropDownStyle = 'DropDownList'
$cmbModel.Location = New-Object System.Drawing.Point(120, 328)
$cmbModel.Size = New-Object System.Drawing.Size(465, 28)
[void]$cmbModel.Items.Add('qwen3-coder:30b')
[void]$cmbModel.Items.Add('gpt-oss:20b')
$cmbModel.SelectedIndex = 0
if ($prefs -and $prefs.PSObject.Properties['last_model'] -and $cmbModel.Items.Contains([string]$prefs.last_model)) { $cmbModel.SelectedItem = [string]$prefs.last_model }
$form.Controls.Add($cmbModel)

$lblNote = New-Object System.Windows.Forms.Label
$lblNote.Text = "โมเดลรันบน CPU ใช้เวลาหลายนาทีถึงหลายสิบนาที และจะรอจนแชตว่างก่อนเริ่ม`r`nระหว่างทำงาน แชตกับ Qwen จะช้าลงเพราะต้องโหลดโมเดลใหม่"
$lblNote.ForeColor = [System.Drawing.Color]::DimGray
$lblNote.Location = New-Object System.Drawing.Point(15, 368)
$lblNote.Size = New-Object System.Drawing.Size(570, 48)
$form.Controls.Add($lblNote)

$btnOk = New-Object System.Windows.Forms.Button
$btnOk.Text = 'เริ่มงาน'
$btnOk.Location = New-Object System.Drawing.Point(385, 425)
$btnOk.Size = New-Object System.Drawing.Size(95, 34)
$form.Controls.Add($btnOk)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = 'ยกเลิก'
$btnCancel.Location = New-Object System.Drawing.Point(490, 425)
$btnCancel.Size = New-Object System.Drawing.Size(95, 34)
$btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($btnCancel)
$form.CancelButton = $btnCancel

$btnOk.Add_Click({
        $folder = $txtFolder.Text.Trim().Trim('"')
        if ($cmbKind.SelectedIndex -ne 2 -and -not $txtPrompt.Text.Trim()) { Show-Message 'พิมพ์งานที่ต้องการก่อน' 'Warning'; return }
        if (-not $folder -or -not (Test-Path -LiteralPath $folder -PathType Container)) { Show-Message 'เลือกโฟลเดอร์งานที่มีอยู่จริง' 'Warning'; return }
        if ($cmbKind.SelectedIndex -eq 1 -and $writeMode -notin @('windows-sandbox', 'full-access')) {
            Show-Message ("โหมดแก้ไขไฟล์ยังไม่เปิดใช้ ต้องตัดสินใจก่อนว่าจะใช้ Windows sandbox ของ Codex (เปลี่ยนสิทธิ์ ACL ของโฟลเดอร์) หรือให้รันด้วยสิทธิ์ผู้ใช้เต็ม`r`nตั้งค่า write_mode ใน {0}`r`nรายละเอียดใน local-tools\codex\INSTRUCTIONS.md" -f $SettingsPath) 'Warning'
            return
        }
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    })
$form.Add_Shown({ $form.Activate(); $txtPrompt.Focus() })

if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }

$folder = $txtFolder.Text.Trim().Trim('"').TrimEnd('\')
$model = [string]$cmbModel.SelectedItem
Save-Prefs $folder $model $cmbKind.SelectedIndex
$params = @{ Cwd = $folder; Model = $model; Detach = $true; JsonOut = $true }
if ($cmbKind.SelectedIndex -eq 2) { $params['Review'] = $true } else { $params['Exec'] = $true }
if ($cmbKind.SelectedIndex -eq 1) { $params['Write'] = $true }
if ($txtPrompt.Text.Trim()) { $params['Prompt'] = $txtPrompt.Text.Trim() }

# In-process call: the prompt never passes through a command line. -Detach checks the
# arguments, parks the prompt in a file and starts the hidden job.
$lines = @(& $Invoke @params)
$answer = $null
foreach ($line in ($lines | Where-Object { $_ })) { try { $answer = ([string]$line | ConvertFrom-Json) } catch { } }
if ($answer -and $answer.status -eq 'started') {
    Show-Message ("เริ่มงานแล้ว รหัส {0}`r`nงานจะรอจนโมเดลว่างแล้วทำงานเบื้องหลัง เสร็จแล้วจะแจ้งเตือน`r`nบันทึกงาน: {1}" -f $answer.id, $answer.log)
} elseif ($answer -and $answer.PSObject.Properties['error']) {
    Show-Message ("เริ่มงานไม่ได้: " + $answer.error) 'Warning'
} else {
    Show-Message ("เริ่มงานไม่ได้ (ไม่มีคำตอบจาก Invoke-QwenCodex.ps1): " + (($lines | Out-String).Trim())) 'Warning'
}
