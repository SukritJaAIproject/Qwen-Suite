# Qwen Internet Prompt Gateway

เกตเวย์ส่วนตัวสำหรับส่งข้อความหา `qwen3.8:27b` จากอินเทอร์เน็ตผ่านหน้าเว็บ, REST API และ LINE Messaging API

## ขอบเขตความปลอดภัย

- รับและตอบเฉพาะข้อความ
- เรียก Ollama ที่ `127.0.0.1:11434` โดยกำหนดโมเดลตายตัว
- ไม่ส่ง `tools` และไม่เชื่อม MCP, Open WebUI, CMD, ไฟล์, browser หรือโปรแกรมในเครื่อง
- ตัวเกตเวย์และ Ollama ฟังเฉพาะ loopback; Cloudflare เปิดเฉพาะพอร์ตเกตเวย์ 8780
- REST API ต้องใช้ Bearer key และ `Idempotency-Key`
- LINE ตรวจ HMAC-SHA256 จาก raw request body ก่อนอ่าน JSON และใช้ allowlist
- Channel secret/access token ถูกเข้ารหัสด้วย Windows DPAPI ของบัญชี Windows ปัจจุบัน
- เก็บคิวและ memory แยกตามช่องทาง/session ใน SQLite และล้าง memory ที่เกิน 30 วันอัตโนมัติ
- มีโหมดค้นเว็บผ่าน DDGS แบบไม่ใช้ API key พร้อมแนบ URL ของผลการค้นหาโดยระบบ
- ผลค้นเว็บเป็นข้อมูลที่ไม่เชื่อถือและไม่สามารถเรียก tools, เปิดไฟล์ หรือรันคำสั่งได้

> คำสั่งจากอินเทอร์เน็ตไม่สามารถสั่งงานคอมพิวเตอร์ได้โดยตั้งใจ หากจะเพิ่ม remote actions ภายหลัง ต้องทำ allowlist และการยืนยันแยกต่างหาก

## เปิดใช้งาน

ดับเบิลคลิก `Qwen Internet Setup` บน Desktop หรือรัน:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\localllm\internet-gateway\open-internet-gateway.ps1
```

หน้า setup ในเครื่อง: `http://127.0.0.1:8780/local/setup`

หน้า setup แสดง:

- Internet URL ปัจจุบัน
- API key สำหรับหน้าเว็บ/API
- pairing token ของ LINE (สุ่ม 144 บิต, อายุ 30 นาที และเปลี่ยนหลังจับคู่สำเร็จ)
- สถานะการตั้งค่า LINE

## ใช้หน้าเว็บจากอินเทอร์เน็ต

1. เปิด Internet URL ที่แสดงในหน้า setup
2. ใส่ API key จากหน้า setup
3. หากต้องการข้อมูลล่าสุด ให้เปิดสวิตช์ **ค้นข้อมูลล่าสุดจากอินเทอร์เน็ต**
4. เปิด **Extend Mode** หากต้องการให้ระบบต่ออัตโนมัติเมื่อคำตอบชนเพดาน (สูงสุด 3 ช่วงต่อรอบ)
5. ส่งข้อความ ระบบจะเข้าคิวและหน้าเว็บจะรอผลอัตโนมัติ

หลังได้คำตอบแล้ว ปุ่ม **ต่อคำตอบ** จะปรากฏเสมอ การกดแต่ละครั้งสร้างงานช่วงใหม่ใน session เดิมและส่งเป็น `user` turn จริง จึงหลีกเลี่ยงบั๊ก `no user query found in messages` ของ Qwen3.8 ได้ดีกว่าการต่อแบบ assistant-only ผู้ใช้สามารถกดต่อเองได้เรื่อย ๆ ภายใต้คิวและ rate limit ของเกตเวย์

API key เก็บใน `sessionStorage` ของ browser เท่านั้น ไม่อยู่ใน URL

## ตั้งค่า LINE

ต้องมี LINE Official Account และ Messaging API channel ก่อน แล้วคลิกขวาไฟล์นี้ > **Run with PowerShell** (ดับเบิลคลิกไฟล์ .ps1 จะเปิดใน Notepad ไม่ได้รันสคริปต์):

```text
E:\localllm\internet-gateway\configure-line.ps1
```

กรอก Channel secret และ Channel access token ในหน้าต่าง PowerShell โดยตรง อย่าส่งค่าเหล่านี้ในแชท สคริปต์จะ:

1. เก็บค่าด้วย Windows DPAPI
2. ตั้ง webhook เป็น `<Internet URL>/webhooks/line`
3. ขอให้ LINE ทดสอบ webhook

จากนั้นเพิ่มเพื่อน Official Account แล้วส่ง:

```text
/pair วาง-pairing-token-จากหน้า-setup
```

แทนข้อความด้านหลัง `/pair` ด้วย token จากหน้า setup คำสั่งเพิ่มเติมคือ:

- `/web คำถาม` หรือ `/ค้นเว็บ คำถาม` ค้นข้อมูลล่าสุดและแนบแหล่งอ้างอิง
- `/new` เริ่มบทสนทนาใหม่
- `/status` ดูงานล่าสุด
- `/help` ดูวิธีใช้

## การค้นเว็บ

- ปิดไว้เป็นค่าเริ่มต้นในแต่ละแท็บเพื่อความเป็นส่วนตัว เพราะคำถามในโหมดนี้จะถูกส่งให้ผู้ให้บริการค้นหา
- ใช้ DDGS ที่ติดตั้งในเครื่อง โดยลอง backend อัตโนมัติก่อนแล้วจึงสลับแหล่งสำรอง ค้นหาไม่เกิน 3 ผลลัพธ์ และใช้เฉพาะชื่อเรื่อง, ข้อความย่อ และ HTTPS URL
- ไม่เปิดหน้าเว็บตาม URL, ไม่รัน JavaScript และไม่ส่ง `tools` ให้โมเดล จึงไม่เปิดทางให้เนื้อหาเว็บควบคุมคอมพิวเตอร์
- เนื้อหาเว็บดิบไม่ถูกบันทึกใน memory; เก็บเฉพาะคำถามและคำตอบสุดท้าย
- งานที่ค้างเกิน 30 นาทีจะถูกหยุด เพื่อรองรับ Qwen 27B ที่รันบน CPU โดยไม่ปล่อยให้งานหนึ่งค้างทั้งคิวถาวร
- DDGS เป็นบริการค้นหาแบบ best-effort อาจถูกจำกัดหรือใช้งานไม่ได้ชั่วคราว หากต้องการ SLA ให้ตั้ง Search API แบบมี key ภายหลัง

## REST API

ตัวอย่าง PowerShell:

```powershell
$headers = @{
  Authorization = 'Bearer ใส่-api-key'
  'Idempotency-Key' = [guid]::NewGuid().ToString()
}
$job = Invoke-RestMethod -Method Post -Uri 'https://YOUR-TUNNEL.trycloudflare.com/api/v1/prompt' `
  -Headers $headers -ContentType 'application/json' `
  -Body (@{ prompt = 'ข่าวพลังงานหมุนเวียนล่าสุด'; session_id = 'engineering'; web_search = $true } | ConvertTo-Json)
Invoke-RestMethod -Uri ("https://YOUR-TUNNEL.trycloudflare.com" + $job.status_url) -Headers $headers
```

ต่อคำตอบเดิมหลังงานสำเร็จ:

```powershell
$headers['Idempotency-Key'] = [guid]::NewGuid().ToString()
$next = Invoke-RestMethod -Method Post `
  -Uri ("https://YOUR-TUNNEL.trycloudflare.com/api/v1/jobs/" + $job.job_id + "/continue") `
  -Headers $headers -ContentType 'application/json' `
  -Body (@{ session_id = 'engineering' } | ConvertTo-Json)
```

## Extend Mode และ token

- ไม่มีโมเดลใดมี context ไม่จำกัดจริง รุ่นนี้ประกาศเพดานสูงสุด 262,144 tokens แต่การใช้เต็มเพดานต้องใช้หน่วยความจำและเวลาสูงมาก
- ค่าใช้งานจริงของเครื่องนี้คือ context 32,768 tokens และ output 1,536 tokens ต่อช่วง
- หน้าเว็บรวมทุกช่วงให้เป็นคำตอบต่อเนื่อง พร้อมแสดง `done_reason`, input tokens และ output tokens ที่ Ollama ส่งกลับ
- Auto Extend จำกัด 3 ช่วงต่อรอบเพื่อป้องกันลูปไม่รู้จบ แต่ปุ่ม **ต่อคำตอบ** แบบ manual ใช้ซ้ำได้

## หมายเหตุเรื่อง Quick Tunnel

Quick Tunnel สร้าง URL แบบสุ่มใหม่เมื่อ tunnel เริ่มใหม่ (รวมถึงทุกครั้งที่รีสตาร์ตเครื่อง) และไม่มี SLA เหมาะกับใช้งานส่วนตัว/ทดลอง สำหรับ URL ถาวรควรเปลี่ยนเป็น Cloudflare Named Tunnel พร้อม domain ในภายหลัง (ต้องล็อกอินบัญชี Cloudflare ของเจ้าของเครื่อง)

Watchdog (`watch-internet-gateway.ps1`) ตรวจ `/ready` ของ cloudflared ทุก 2 นาที:

- ถ้า cloudflared ยังรันอยู่แต่หลุดการเชื่อมต่อ จะรอให้ต่อใหม่เองประมาณ 6 นาทีก่อน เพื่อคง URL เดิมไว้
- ถ้าเครื่องไม่มีอินเทอร์เน็ต (หา `api.trycloudflare.com` ไม่เจอ) จะเลื่อนการซ่อมไปจนกว่าเน็ตกลับมา
- ถ้าซ่อมไม่สำเร็จซ้ำ ๆ จะเว้นช่วงลองใหม่ 2, 4, 8, 16 แล้วทุก 30 นาที
- `start-internet-watchdog.ps1` เปิด watchdog เสมอ แม้การเปิด tunnel ครั้งแรกตอนเปิดเครื่องจะล้มเหลว

เมื่อได้ URL ใหม่ `start-quick-tunnel.ps1` จะ:

1. บันทึก URL ลง `data\public-url.txt`
2. ถ้าตั้งค่า LINE แล้ว จะลงทะเบียน webhook ใหม่กับ LINE ให้อัตโนมัติ (ถ้าไม่สำเร็จจะลองใหม่ครั้งถัดไป)
3. ส่งแจ้งเตือนทาง Telegram ถึงเจ้าของ + toast บนเครื่อง ว่า "URL ของ tunnel เปลี่ยนเป็น ..." ผ่าน `local-tools\notifier.py` (ปิด/เปิดได้ที่ `E:\localllm\data\notify.json`) บันทึกผลที่ `E:\localllm\logs\internet-url-notify.log`

เครื่องต้องเปิดอยู่, ไม่ Sleep และมีอินเทอร์เน็ตจึงจะตอบจากภายนอกได้

## ไฟล์สำคัญ

- Config/credentials: `E:\localllm\internet-gateway\data`
- Database: `E:\localllm\internet-gateway\data\gateway.db`
- Logs: `E:\localllm\logs\internet-gateway*.log`, `internet-tunnel*.log`, `internet-watchdog*.log`, `internet-url-notify.log`

ห้ามคัดลอก `client-access.json` ไปยัง cloud storage หรือส่งให้ผู้อื่น
