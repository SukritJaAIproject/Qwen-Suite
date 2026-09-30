$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$openClaw = 'E:\localllm\openclaw-runtime\openclaw.cmd'

function Ensure-Gateway {
    if (-not (Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName 'OpenClaw Gateway'
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Get-NetTCPConnection -LocalPort 18789 -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
        }
    }
}

function Show-Status {
    Write-Host ''
    & $openClaw channels status --probe
    Write-Host ''
}

Ensure-Gateway

do {
    Clear-Host
    Write-Host 'QwenLocal + OpenClaw' -ForegroundColor Cyan
    Write-Host 'โมเดล: qwen3.8:27b (ในเครื่อง)'
    Write-Host 'ความปลอดภัย: DM pairing / ปิด Group / Gateway ใช้เฉพาะเครื่องนี้'
    Write-Host ''
    Write-Host '1  เชื่อม Telegram (ต้องมี Bot Token จาก BotFather)'
    Write-Host '2  เชื่อม WhatsApp (สแกน QR ด้วยโทรศัพท์)'
    Write-Host '3  เชื่อม LINE (ต้องมี Channel Access Token + Channel Secret)'
    Write-Host '4  ตรวจสถานะช่องทาง'
    Write-Host '5  ดู/อนุมัติ Pairing Code'
    Write-Host '6  เปิดหน้า OpenClaw Control'
    Write-Host '0  ปิดหน้าต่าง'
    Write-Host ''
    $choice = Read-Host 'เลือกหมายเลข'

    switch ($choice) {
        '1' {
            & $openClaw channels add --channel telegram --agent qwenlocal
            & $openClaw gateway restart
            Show-Status
            Read-Host 'กด Enter เพื่อกลับเมนู' | Out-Null
        }
        '2' {
            & $openClaw channels login --channel whatsapp --agent qwenlocal --verbose
            & $openClaw gateway restart
            Show-Status
            Read-Host 'กด Enter เพื่อกลับเมนู' | Out-Null
        }
        '3' {
            & $openClaw channels add --channel line --agent qwenlocal
            & $openClaw gateway restart
            Show-Status
            Write-Host 'LINE ต้องมี Public HTTPS Webhook หลังเพิ่ม Token/Secret แล้ว' -ForegroundColor Yellow
            Read-Host 'กด Enter เพื่อกลับเมนู' | Out-Null
        }
        '4' {
            Show-Status
            Read-Host 'กด Enter เพื่อกลับเมนู' | Out-Null
        }
        '5' {
            & $openClaw pairing --help
            Write-Host ''
            Write-Host 'ตัวอย่าง: openclaw pairing approve telegram CODE'
            Read-Host 'กด Enter เพื่อกลับเมนู' | Out-Null
        }
        '6' {
            & $openClaw dashboard --yes
            Read-Host 'กด Enter เพื่อกลับเมนู' | Out-Null
        }
    }
} while ($choice -ne '0')
