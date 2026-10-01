# Local LLM บนเครื่อง Dell Precision 3551

## อัปเดตโมเดล 1 ต.ค. 2026 (Qwen Suite 1.6)

- `qwen-agent:latest` และ `qwen-review:latest` ใช้ `muse-glimmer:30b` สำหรับ tools, agent workflow และ Second Opinion
- `qwen-fast:latest` และ `qwen-structured:latest` ใช้ `granite4.2:8b` สำหรับงานสั้น, JSON/schema/extraction และ RAG; embedding ยังเป็น `qwen3-embedding:4b`
- ถ้ายังไม่มีน้ำหนักใหม่ Router จะ fallback ไป `gpt-oss:20b` และ `qwen3:4b` โดยอัตโนมัติ ไม่ทำให้ระบบเดิมหยุด
- `generate_image` ใช้ `provider=auto`: เลือก Qwen-Image 2.1 ในเครื่องเมื่อชุดเสริมแยกติดตั้งครบ มิฉะนั้นใช้ Runway แบบเดิม
- Qwen-Image 2.1 ไม่อยู่ใน installer หลัก ต้องใช้ optional pack, ยอมรับ Qwen Research License และมีพื้นที่ว่างเพิ่มอย่างน้อย 50 GB

หมายเหตุ: ตาราง benchmark และบันทึกวันที่เก่ากว่าด้านล่างเก็บไว้เป็นประวัติของฮาร์ดแวร์/โมเดลเดิม

โมเดลและข้อมูลหลักรันในเครื่อง หน้า Open WebUI ยังเปิดเฉพาะ localhost ตามเดิม
มี `Qwen Internet Prompt Gateway` แยกต่างหากสำหรับส่งข้อความจากเว็บ/API/LINE ผ่าน HTTPS
โดยช่องทางนี้ไม่มีสิทธิ์ใช้ CMD, ไฟล์, browser หรือ Local Computer tools

## เริ่มใช้งาน

**เปิดเบราว์เซอร์ไปที่ http://127.0.0.1:3000 ได้เลย** — ทั้ง Ollama และ Open WebUI
ขึ้นเองตอนเข้าเครื่องแล้ว (Open WebUI ใช้เวลาบูตราว 30-60 วินาทีหลังล็อกอิน
จะเห็นหน้าต่างย่อไว้ที่ทาสก์บาร์ ปล่อยไว้อย่างนั้น)

ถ้ามันไม่ขึ้นด้วยเหตุใดก็ตาม ดับเบิลคลิก `E:\localllm\Open WebUI.lnk`
(รันซ้ำได้ ถ้าทำงานอยู่แล้วมันจะบอกแล้วออก)

เลิกใช้แล้วอยากคืนแรม → รัน `E:\localllm\stop-all.ps1`

## ใช้จากอินเทอร์เน็ตหรือ LINE

ดับเบิลคลิก `Qwen Internet Setup` บน Desktop เพื่อดู Internet URL, API key และรหัสจับคู่ LINE
ตัว Gateway เริ่มพร้อม Open WebUI ตอนเข้า Windows และเก็บ memory แยกตาม session/ผู้ใช้

- เว็บ/API ใช้งานได้ทันทีจาก Internet URL โดยต้องใส่ API key
- LINE ต้องกรอก Channel secret และ Channel access token ของ LINE Official Account หนึ่งครั้งผ่าน
  `E:\localllm\internet-gateway\configure-line.ps1`
- คู่มือฉบับเต็มอยู่ที่ `E:\localllm\internet-gateway\README.md`
- Quick Tunnel ใช้ URL ชั่วคราวซึ่งอาจเปลี่ยนเมื่อเริ่ม tunnel ใหม่ เครื่องต้องเปิดและไม่ Sleep

## โปรไฟล์ Qwen ที่ใช้งานปัจจุบัน (ปรับความเร็ว 12 ก.ย. 2026)

- ใช้โมเดลใหญ่ตัวเดิม `qwen3.8:27b` (Q4_K_M ประมาณ 17 GB) น้ำหนักอยู่บน CPU (ทาง A: P620 ช่วยอ่าน prompt · ทาง B ไม่มี eGPU: CPU ล้วน)
- ทุกช่องทางใช้ตัวเลือก runner ชุดเดียวกัน (`num_ctx` 32768, ไม่ส่ง `num_thread`, `shift` ตามค่าเริ่มต้น) เพื่อไม่ให้ Ollama โหลดโมเดลใหม่เมื่อสลับช่องทาง — ดูหัวข้อ "ตัวเลือก runner" ด้านล่าง (แก้ 29 ก.ย. 2026; ค่าเดิม 8192/12 ไม่ได้ใช้แล้ว)
- โมเดลโหลดตอนมีคำขอแรก (ไม่มีการโหลดล่วงหน้า) แล้วค้างไว้ 2 ชั่วโมง
- ปิด thinking เป็นค่าเริ่มต้นเพื่อลดเวลารอ; เปิดกลับใน Model settings ได้เมื่อต้องการวิเคราะห์เชิงลึก
- โปรไฟล์ Qwen ใช้ `Local Computer Fast` ซึ่งรวม 26 tools เป็น 2 gateway tools; Full tools ยังอยู่ในเมนูให้เลือกเอง
- tool schema ลดจาก 13,077 เหลือ 907 ไบต์ (93.1%); รอบเรียก tool ผ่าน Open WebUI ลดจาก 141.8 เหลือ 104.0 วินาที
- แชทยาวใน Open WebUI จะย่อบริบทเมื่อประมาณ 24,000 tokens ด้วย `qwen-fast` (`granite4.2:8b`; fallback เป็น `qwen3:4b` จนกว่าจะดาวน์โหลด Granite)
- benchmark หลังปรับ: output median 1.58 tok/s และ 64 tokens ใช้เวลา median 41.82 วินาที (3 warm runs — วัด 12 ก.ย. ด้วย num_ctx 8192 / num_thread 12 เทียบกับผลหลัง 29 ก.ย. ตรง ๆ ไม่ได้)

### ตัวเลือก runner ต้องเหมือนกันทุกช่องทาง ("runner options") — แก้ 29 ก.ย. 2026

Ollama 0.32 เทียบค่าเหล่านี้ของคำขอกับ runner ที่โหลดอยู่ ถ้าต่างกันจะปิดแล้วโหลดโมเดลใหม่
(`qwen3.8:27b` 17 GB และ cache ของ prompt หาย — prompt ~20k tokens ของ OpenClaw ต้องอ่านใหม่ 13–23 นาทีบน CPU):
`num_ctx`, `num_thread`, `num_batch`, `num_gpu`, `main_gpu`, `use_mmap` และ `shift` (ระดับบนสุดของคำขอ ไม่ใช่ใน options) ·
`keep_alive`, `temperature`, `num_predict`, `truncate`, `think` ไม่มีผล

- log 23–29 ก.ย.: reload แบบนี้ 7 ครั้ง — **6 ครั้งเพราะ `shift`** (OpenClaw ส่ง `shift:false` ทุกคำขอ ช่องทางอื่นไม่ส่ง = true)
- ค่ามาตรฐาน: `num_ctx` 32768 หรือไม่ส่ง (เซิร์ฟเวอร์ตั้ง `OLLAMA_CONTEXT_LENGTH=32768`) · ไม่ส่ง `num_thread` (llama-server ใช้ 6 คอร์จริง ซึ่งเป็นค่าที่ใช้กับเกือบทุกการโหลด) ·
  ไม่ส่ง `shift` · ไม่ส่ง `num_batch`/`num_gpu`/`main_gpu`/`use_mmap` (ย้ายโมเดลไป CPU ด้วย `Set-QwenModelPlacement.ps1` ซึ่งตั้งในตัวโมเดล —
  `-NumCtx` ใช้ได้เฉพาะโมเดลที่ไม่มีใครส่ง `num_ctx` เช่น `qwen3-embedding:4b` สคริปต์ปฏิเสธกับโมเดลแชท)
- ทำที่ไหน: router (`:8770`) ตัด `num_thread` ออกจากทุกคำขอ และตัด `shift` สำหรับ `qwen3.8` / `gpt-oss` (OpenClaw และ preset qwen-* ของ Open WebUI ผ่านทางนี้) ·
  preset ของ Open WebUI ไม่มี `num_thread` แล้ว (ค่าเดิมและสคริปต์คืนค่า: `backups\claude-20260929-runneropts\`) ·
  internet gateway `num_thread` 0 และการแปลคำค้นใช้ `num_ctx` เดียวกับคำตอบ (เดิม 4096) · video worker ไม่ส่ง `num_ctx` · `bench.ps1` ใช้ค่าเดียวกับ production
- `shift:false` ของ OpenClaw ไม่มีผลกับ `qwen3.8` และ `gpt-oss` (llama-server ปิด KV-cache shift ของสองโมเดลนี้เอง และ OpenClaw ส่ง `truncate:false` ด้วย) จึงตัดทิ้งได้ ·
  สำหรับ `qwen3-coder:30b` / `qwen3:4b` ยังส่งต่อ เพราะถ้าเปิด shift คำตอบที่ล้น 32k จะตอบต่อโดยตัด system prompt และ tool schema ทิ้ง แทนที่จะหยุด —
  สองโมเดลนี้โหลดไม่บ่อย จึงยอมให้ reload เป็นครั้งคราว
- ตรวจ: ใน `logs\ollama-server.log` ทุกบรรทัด `starting llama-server` ของโมเดลเดียวกันต้องมี `-c`, `-t` และ `--context-shift` แบบเดียวกัน
- เพิ่มช่องทางใหม่ที่เรียก Ollama ตรง: ส่งค่าตามนี้ หรือส่งผ่าน router

---

## เครื่องนี้มีอะไร

| | |
|---|---|
| CPU | Intel i7-10750H — 6 คอร์ / 12 เธรด |
| RAM | 64 GB DDR4-2933 (dual channel) |
| GPU | NVIDIA Quadro P620 — VRAM 4 GB — เคยปิดไว้เพราะวัดแล้วช้าลง แต่ **ค่าที่ปิดหายไปแล้ว ณ 22 ก.ย. 2026** (ดูตาราง environment ด้านล่าง) |
| ดิสก์ | NVMe SSD — โมเดลอยู่ที่ `E:\ollama-models` |

**ทำไมถึงเลือกโมเดลแบบนี้:** GPU มี VRAM แค่ 4 GB เล็กเกินกว่าจะใส่โมเดลใหญ่
แต่ RAM มี 64 GB จึงเลือกโมเดลตระกูล **MoE (Mixture-of-Experts)** ที่มีพารามิเตอร์รวมเยอะ
แต่ใช้งานจริงแค่ราว 3B ต่อ token — อ่านน้ำหนักจากแรมน้อยลงเกือบ 10 เท่า จึงเร็วกว่าโมเดล
หนาแน่น (dense) ที่เล็กกว่ามาก ทั้งที่ฉลาดกว่า

---

## ผลวัดจริงบนเครื่องนี้

| โมเดล | ขนาด | CPU อย่างเดียว | CPU+GPU | คุณภาพภาษาไทย |
|---|---|---|---|---|
| **qwen3-coder:30b** | 18 GB | **11.0 tok/s** | 8.7 tok/s | ดีที่สุด — ตอบกระชับ ตรงคำสั่ง เขียนโค้ดครบ จบเอง |
| gpt-oss:20b | 13 GB | 6.1 tok/s | 3.5 tok/s | พอใช้ แต่เผาโทเคนกับ reasoning จนมักตอบไม่จบ |
| qwen3:4b | 2.5 GB | 8.8 tok/s | 2.7 tok/s | อ่อน — คิดเป็นภาษาอังกฤษวนไปเรื่อย ๆ แทนที่จะตอบ |

**สองข้อที่สวนสามัญสำนึก แต่วัดออกมาแล้วเป็นแบบนี้:**

1. **โมเดล 30B เร็วกว่าโมเดล 4B** — 30B เป็น MoE ใช้จริงแค่ 3.3B ต่อ token
   ส่วน 4B เป็น dense ต้องอ่านน้ำหนักทั้งก้อนทุก token
2. **GPU ทำให้ช้าลงทุกโมเดล** (33-248%) — P620 อ่อนเกินไป ต้นทุนสลับข้อมูล
   CPU↔GPU ทุก token แพงกว่าที่ GPU ช่วยคำนวณได้

**ใช้ `qwen3-coder:30b` เป็นตัวหลัก** อีกสองตัวเป็นตัวสำรอง
(หลังดาวน์โหลด Muse/Granite และทดสอบครบแล้ว หากยืนยันว่าไม่ต้องการ fallback เดิมจึงค่อยพิจารณา `ollama rm gpt-oss:20b qwen3:4b` เพื่อคืนประมาณ 15.5 GB)

### ข้อจำกัดที่ต้องรู้

เอกสารยาวช้ามาก เพราะ CPU ต้องอ่านทุกโทเคนก่อนเริ่มตอบ วัดกับ qwen3-coder:30b:

| ความยาว input | เวลาอ่าน | ความเร็วตอนตอบ |
|---|---|---|
| ~135 tokens (คำถามสั้น) | ทันที | 11 tok/s |
| ~2,800 tokens (เอกสาร 4-5 หน้า) | **125 วินาที** | 3.5 tok/s |

ถ้าต้องทำงานกับเอกสารยาวบ่อย ให้ตัดเฉพาะส่วนที่เกี่ยวข้องเข้าไป จะเร็วกว่าโยนทั้งไฟล์มาก

---

## วิธีใช้

### แบบที่ 1 — หน้าเว็บแชท

`E:\localllm\Open WebUI.lnk` → **http://127.0.0.1:3000** ปิดด้วย `Ctrl+C` ในหน้าต่างที่เปิดไว้

> ใช้พอร์ต **3000** ไม่ใช่ 8080 ที่เป็นค่ามาตรฐานของ Open WebUI เพราะเครื่องนี้มีโปรแกรมชื่อ
> `ApplicationWebServer.exe` ครองพอร์ต 8080 อยู่แล้ว
>
> เปิดเฉพาะ `127.0.0.1` — เครื่องอื่นในเครือข่ายเข้าไม่ได้ และปิดระบบล็อกอินไว้เพราะใช้คนเดียว
> ถ้าอยากเปิดบัญชี/รหัสผ่าน แก้ `WEBUI_AUTH` ใน `start-webui.ps1` เป็น `'True'`

### แบบที่ 2 — บรรทัดคำสั่ง

```bash
ollama run qwen3-coder:30b
```

ออกด้วย `/bye` — หรือถามคำถามเดียวจบ:

```bash
ollama run qwen3-coder:30b "สรุปข้อความนี้เป็น 3 บรรทัด: ..."
```

### แบบที่ 3 — เรียกผ่าน API

API อยู่ที่ `http://127.0.0.1:11434` และมี endpoint เข้ากันได้กับ OpenAI ที่ `/v1`

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
r = client.chat.completions.create(
    model="qwen3-coder:30b",
    messages=[{"role": "user", "content": "สวัสดี"}],
)
print(r.choices[0].message.content)
```

> **เรียกจาก PowerShell 5.1 ด้วยข้อความภาษาไทย** ระวัง 2 เรื่อง (เจอมาแล้วตอนติดตั้ง):
> - อย่าใช้ `Get-Content -Raw` ส่งเข้า `ConvertTo-Json` — มันคืน PSObject ที่ห่อ string ไว้
>   ได้ JSON เป็น `"prompt": {"value": "..."}` แล้ว API ตอบ 400
>   ใช้ `[System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)` แทน
> - ส่ง body เป็น byte array UTF-8 เอง: `[System.Text.Encoding]::UTF8.GetBytes($json)`
>   ไม่งั้นภาษาไทยกลายเป็น `?`
> - ไฟล์ `.ps1` ที่มีตัวอักษรไทยต้องเซฟเป็น UTF-8 **with BOM** ไม่งั้น PS 5.1 อ่านเป็น ANSI

---

## สคริปต์ที่มีให้

| ไฟล์ | ทำอะไร |
|---|---|
| `Open WebUI.lnk` | เปิดหน้าเว็บแชท (สตาร์ท Ollama ให้เองถ้ายังไม่ทำงาน) |
| `start-webui.ps1` | ตัวเดียวกับข้างบน แบบสั่งเอง |
| `start-ollama-server.ps1` | สตาร์ทเฉพาะเซิร์ฟเวอร์ Ollama — รันซ้ำได้ไม่มีผลเสีย |
| `stop-all.ps1` | ปิดทุกอย่างและคืนแรม |
| `bench.ps1` | วัด `qwen3.8:27b` แบบไม่หยุดโมเดล: warmup แล้วรายงาน median/p95 โดยใช้ค่า production เดียวกัน |
| `internet-gateway\open-internet-gateway.ps1` | เปิด/ตรวจ Internet Gateway และหน้า setup |
| `internet-gateway\configure-line.ps1` | เก็บ credential ด้วย DPAPI แล้วลงทะเบียน LINE webhook |
| `disabled-startup\Ollama.lnk` | ทางลัดเดิมของแอป tray ที่ถอดออกจาก Startup — ย้ายกลับได้ถ้าต้องการ |

**ที่รันเองตอนเข้าเครื่อง** (Startup folder): `Ollama Server.lnk` และ `Open WebUI (background).lnk`
โดย `start-webui.ps1` จะตรวจและเริ่ม Internet Gateway/Tunnel ให้อัตโนมัติด้วย
ถ้าไม่อยากให้ Open WebUI ขึ้นเอง ลบตัวหลังออกจาก
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` แล้วเปิดเองด้วย `Open WebUI.lnk` แทน

---

## การตั้งค่าที่ปรับไว้ให้

### ฝั่ง Ollama

ตั้งเป็น environment variable ระดับ User (ถาวร)

| ตัวแปร | ค่า | เหตุผล |
|---|---|---|
| `OLLAMA_MODELS` | `E:\ollama-models` | ไม่ให้โมเดลกินไดรฟ์ C: |
| ~~`OLLAMA_LLM_LIBRARY`~~ | ~~`cpu`~~ | **ไม่ต้องตั้งแล้ว และไม่ควรตั้ง** — วัด 29 ก.ย. 2026: ให้ P620 ช่วยอ่าน prompt เร็วกว่า CPU อย่างเดียว 3.5 เท่า (21.2 vs 4.9–6.1 tok/s) · การเลือก GPU ย้ายไปทำใน `Resolve-OllamaGpu.ps1` ทุกครั้งที่สตาร์ท (ดูหัวข้อ eGPU ท้ายไฟล์) · ถ้าตั้งค่านี้เองในรีจิสทรี ระบบจะถือเป็นโหมด manual และไม่แตะอะไร |
| `OLLAMA_KEEP_ALIVE` | `2h` | ค้างโมเดลในแรม ลดการโหลด 17 GB ใหม่ซ้ำ ๆ |
| `OLLAMA_CONTEXT_LENGTH` | `32768` | ค่าปัจจุบันในรีจิสทรี (เดิมเอกสารนี้เขียนว่า 8192) |
| `OLLAMA_FLASH_ATTENTION` | `1` | เร็วขึ้นและกินหน่วยความจำน้อยลง |
| `OLLAMA_MAX_LOADED_MODELS` | `2` | ตั้งใจให้โมเดลเล็กอยู่คู่ตัวหลักได้ (20 + 5 GB) — แต่ตราบใดที่มี GPU ให้ Ollama เห็น มันตัดสินจาก VRAM (P620 4 GB) จึงไล่ตัวหลักออกทุกครั้งที่สลับโมเดล (วัด 29 ก.ย.: 16 ครั้งใน 6 วัน) |
| `OLLAMA_GPU_OVERHEAD` | `512 MB` | ใน Ollama 0.32 **ไม่ได้กันพื้นที่ VRAM จริง** (ใช้แค่เลือก context เริ่มต้นและพิมพ์ log) — ตัวที่กันจริงคือ `LLAMA_ARG_FIT_TARGET` (ค่าเริ่ม 1024 MiB ต่อการ์ด) |

`OLLAMA_LLM_LIBRARY=cpu` มีผลเฉพาะ Ollama **ไม่ได้ปิด CUDA ทั้งระบบ** — conda env
กับ PyTorch ในเครื่องยังใช้ GPU ได้ตามปกติ

### ฝั่ง Open WebUI

ค่าเริ่มต้นของ Open WebUI ออกแบบมาสำหรับเครื่องที่มี GPU แรง ๆ บนเครื่องนี้มันทำให้
คำถามสั้น ๆ ประโยคเดียวกลายเป็น prompt **6,673 tokens** ซึ่งต้องรอ ~4 นาทีก่อนโมเดลจะเริ่มพิมพ์
หลังปรับแล้วเหลือ **249 tokens**

| ค่า | เดิม | ใหม่ | เหตุผล |
|---|---|---|---|
| `code_interpreter.enable` | true | **false** | ยัด system prompt ของ pyodide เข้าไปทุกครั้ง |
| `task.tags.enable` | true | **false** | ยิงโมเดล 18 GB เพิ่มอีก 1 ครั้งต่อข้อความ |
| `task.follow_up.enable` | true | **false** | ยิงโมเดลเพิ่มอีก 1 ครั้ง |
| `task.query.retrieval.enable` | true | **false** | ยิงโมเดลเพิ่มอีก 1 ครั้ง |
| `task.query.search.enable` | true | **false** | ยิงโมเดลเพิ่มอีก 1 ครั้ง |
| `task.model.default` | (ตัวเดียวกัน) | **`qwen-fast:latest`** | ตั้งชื่อแชทด้วย Granite; fallback ไป Qwen3 4B หากยังไม่ติดตั้ง |
| `task.title.enable` | true | true | เก็บไว้ ตั้งชื่อแชทภาษาไทยได้ดี |

แก้เพิ่มได้ในหน้าเว็บที่ Settings → Interface / Admin Panel
ถ้าอยากเปิดฟีเจอร์ไหนกลับมา รู้ไว้ว่าแต่ละตัวคือการเรียกโมเดลเพิ่ม 1 ครั้งต่อข้อความ

### GPU

ดูหัวข้อ **eGPU ผ่าน Thunderbolt 3** ท้ายไฟล์ — ตั้งแต่ 29 ก.ย. 2026 การเลือก GPU ทำอัตโนมัติทุกครั้งที่ Ollama สตาร์ท
(ไม่มี eGPU = ให้ Ollama ใช้ P620 เองเหมือนเดิม · มี eGPU = ใช้เฉพาะ eGPU) · วัดความเร็วด้วย `Test-QwenGpu.ps1 -Bench`
(`bench.ps1` ใช้ตัวเลือก runner เดียวกับ production ตั้งแต่ 29 ก.ย. — ไม่ทำให้โหลดโมเดลใหม่)

---

## เรื่องที่ควรรู้ (เจอระหว่างติดตั้ง)

**1. แอป Ollama ในถาดระบบถูกถอดออกจาก Startup แล้ว**
เดิมมีสองตัวแย่งกันสตาร์ทเซิร์ฟเวอร์ตอนเข้าเครื่อง แล้วแอป tray ชนะ — ปัญหาคือมันสตาร์ท
โดยไม่ได้อ่านค่าที่ตั้งไว้ เลยไปชี้ที่ `C:\Users\EGAT\.ollama\models` ซึ่งว่างเปล่า
ผลคือ `ollama list` ไม่เห็นโมเดลเลย และ GPU ไม่ถูกปิด

แก้โดยย้าย `Ollama.lnk` ออกไปเก็บไว้ที่ `E:\localllm\disabled-startup\`
(ไม่ได้ลบ — ย้ายกลับได้) เหลือทางสตาร์ทเดียวคือ `Ollama Server.lnk` ที่รัน
`start-ollama-server.ps1` ซึ่งอ่านค่าจาก registry เอง

**อย่าเปิดแอป tray ของ Ollama** (เมนู Start "Ollama", ลิงก์ `ollama://`, หรือสั่ง `ollama` ขณะเซิร์ฟเวอร์ไม่ได้รัน) —
ตรวจกับซอร์ส 0.32.15 แล้ว (29 ก.ย. 2026): มันจะสตาร์ทเซิร์ฟเวอร์ของตัวเอง พอพอร์ต 11434 ไม่ว่างก็ **ฆ่า `ollama.exe serve` ทุกตัว
รวมถึงเซิร์ฟเวอร์ของชุดนี้** แล้วใช้ค่าของมันเอง (ครั้งล่าสุด: context 16384, CPU อย่างเดียว) ข้าม `Resolve-OllamaGpu.ps1`
(ข้อความเดิมที่ว่า "มันจะเกาะเซิร์ฟเวอร์ที่รันอยู่" ถูกเฉพาะ 0.32.9) · กู้คืน: ปิดแอป tray แล้วรัน `Restart-QwenOllama.ps1 -StartIfStopped`

**1b. มี junction กันพลาดไว้อีกชั้น**
`C:\Users\EGAT\.ollama\models` ตอนนี้เป็น junction ชี้ไปที่ `E:\ollama-models`
ถ้าวันไหนมีเซิร์ฟเวอร์สตาร์ทมาโดยไม่ได้อ่านค่า config มันก็ยังเจอโมเดลอยู่ดี
ไม่กินพื้นที่ C: เพิ่ม เพราะเป็นแค่ทางลัดระดับระบบไฟล์

**2. ปิด Ollama แล้วแรมไม่คืน**
`ollama.exe` มีลูกชื่อ `llama-server.exe` ที่ถือโมเดลไว้ทั้งก้อน (19 GB) ถ้าฆ่าแต่ตัวแม่
ตัวลูกจะค้างกินแรมต่อไปโดยไม่มีใครใช้ — `stop-all.ps1` จัดการให้แล้ว
อย่าปิดด้วย Task Manager เฉย ๆ

**3. อย่าสั่ง Open WebUI ให้รันแบบ `-WindowStyle Hidden`**
ถ้าไม่มี console ผูกอยู่ `open-webui.exe` จะอ่าน secret key เสร็จแล้วออกทันที
ไม่เปิดพอร์ต ไม่มี error ให้เห็น shortcut ใน Startup จึงตั้งเป็น **minimized** ไม่ใช่ hidden
(หน้าต่างย่อค้างที่ทาสก์บาร์คือของปกติ อย่าปิด)

**4. GPU ยังถูกมองเห็นแม้ไดรเวอร์ CUDA ใหม่จะไม่รองรับ**
Ollama 0.32.9 คอมไพล์ CUDA v13 ที่ไม่รองรับ compute capability 6.1 ของ P620
แต่ถอยไปใช้ CUDA v12 ให้อัตโนมัติ ถ้าวันหนึ่งอัปเดตแล้ว GPU หายไป ไม่กระทบอะไร
เพราะเราปิด GPU อยู่แล้ว

---

## ที่เก็บไฟล์

| อะไร | ที่ไหน | ขนาด |
|---|---|---|
| โมเดล | `E:\ollama-models` | 32.5 GB |
| โปรแกรม Ollama | `C:\Users\EGAT\AppData\Local\Programs\Ollama` | ~3 GB |
| Open WebUI (venv) | `E:\localllm\webui-venv` | 2.4 GB |
| ประวัติแชท | `E:\localllm\webui-data` | โตตามใช้งาน |
| แคช HuggingFace | `E:\localllm\hf-cache` | โตตามใช้งาน |
| แคช pip | `E:\localllm\pip-cache` | ลบทิ้งได้ |
| ล็อกเซิร์ฟเวอร์ | `E:\localllm\logs` | เล็ก |

ทุกอย่างอยู่บนไดรฟ์ E: ยกเว้นตัวโปรแกรม Ollama เอง — ไดรฟ์ C: เสียพื้นที่ไปราว 3 GB

---

## สถานะจริง ณ 22 ก.ย. 2026 (ส่วนที่เอกสารด้านบนยังไม่ครอบคลุม)

เอกสารด้านบนเขียนไว้ช่วง 12 ก.ย. ตอนที่ระบบยังมีแค่ Ollama + Open WebUI
หลังจากนั้นมีบริการเพิ่มอีกหลายตัว ทั้งหมดผูกกับ `start-webui.ps1` และขึ้นเองตอนเข้าเครื่อง

### บริการที่รันจริง (ทุกตัว bind 127.0.0.1 เท่านั้น)

| พอร์ต | บริการ | ไฟล์ |
|---|---|---|
| 3000 | Open WebUI | `start-webui.ps1` |
| 8765 | Local Computer Tools (MCP เต็ม **71 tools** — 36 เดิม + 26 จากหัวข้อ "ความสามารถแบบ Codex" + 9 จากหัวข้อ "งานชุดที่ 3") | `local-tools\server.py` |
| 8766 | Permission gateway สำหรับ Open WebUI (2 tools: `local_read`, `local_action`) | `local-tools\fast_server.py --source webui` |
| 8767 | Permission gateway สำหรับ OpenClaw (Telegram/WhatsApp/LINE) | `local-tools\fast_server.py --source openclaw` |
| 8770 | Smart Model Router | `ai-services\router.py` |
| 8771 | Reranker (Qwen3-Reranker-0.6B) | `ai-services\reranker.py` |
| 8780 | Internet Prompt Gateway (ตัวเดียวที่ tunnel ชี้มา มี API key) | `internet-gateway\server.py` |
| 11434 | Ollama API | `start-ollama-server.ps1` |
| 18789 | OpenClaw gateway | scheduled task `\OpenClaw Gateway` |

เสริม: embedding `qwen3-embedding:4b` (RAG), Whisper STT (`ai-services\transcribe.py`),
Thai TTS `mms-tts-tha` (`synthesize.py`, **CC BY-NC 4.0 ใช้เชิงพาณิชย์ไม่ได้**),
OCR ผ่าน Tesseract (`local-tools\document_intelligence.py`),
Video Studio (`video_orchestrator.py` / `video_worker.py` / `video_studio.html`),
Permission Center (`permission_manager.py`, `permission_center.html`)

### ของที่ต้องรู้ก่อนแก้อะไรต่อ

- **Permission Center เปิดอนุญาตทั้งหมด** ทั้ง 8 หมวด ทั้งฝั่ง `webui` และ `openclaw` ตามที่ผู้ใช้สั่งไว้
  เมื่อ 19-20 ก.ย. และ `run_command` ตั้ง `confirmed=True` เป็นค่าเริ่มต้น ⇒ ข้อความจาก Telegram/WhatsApp
  สั่งคำสั่งบนเครื่องได้โดยไม่ถามยืนยัน · ด่านที่เหลือคือ block-list 4 regex ที่ `server.py:213`
  ซึ่ง **ทดสอบแล้วพบว่าเลี่ยงได้ทุกกรณีที่ลอง** ถ้าห่อคำสั่งไว้ใน `powershell -Command "..."`
  → **ปิดช่องนี้แล้ว 30 ก.ย. 2026** ด้วย parsed exec-policy `local-tools\command_policy.py` (ดูหัวข้อ "ความสามารถแบบ Codex" › ความปลอดภัย)
- **`chat.context_compaction.enable` เปิดกลับแล้ว 22 ก.ย.** (threshold/cap 24000, retention 40%, ปัจจุบันสรุปด้วย `qwen-fast`/Granite)
  `Configure-Qwen-WebUI.py` เคยตั้งค่านี้เป็น `False` และแก้แล้วเช่นกัน — ถ้ารันสคริปต์นั้นอีกจะไม่ปิดซ้ำ
- **watchdog ของ tunnel เปลี่ยนวิธีตรวจ** จาก "รันสคริปต์สตาร์ททั้งตัวทุก 2 นาทีแล้วดู exit code"
  เป็นการยิง metrics endpoint ของ cloudflared (`/ready`) และจะรันสคริปต์ซ่อมเฉพาะตอนที่ตรวจแล้วไม่แข็งแรง
  ⇒ ไม่มีการเขียนทับ `data\config.json` / `public-url.txt` ทุก 2 นาทีอีกแล้ว
- **`Start-Ollama.ps1` อ่านค่าจากรีจิสทรีแล้ว** เหมือน `start-ollama-server.ps1` (เดิม hardcode ค่าคนละชุด
  ทำให้ config ของ session ขึ้นกับว่า launcher ตัวไหนชนะ race ตอน logon)
- **`E:\localllm\runtime\` ไม่มีอยู่จริง** แม้สคริปต์จะ prepend เข้า PATH — ระบบทำงานได้เพราะ ffmpeg/ffprobe/cloudflared
  อยู่บน PATH จาก WinGet อยู่แล้ว
- **Desktop อยู่ที่ `C:\Users\EGAT\OneDrive - Chulalongkorn University\Desktop`** (redirect ไป OneDrive) ไม่ใช่ `C:\Users\EGAT\Desktop`
  shortcut `Qwen All-in-One`, `Qwen Permission Center`, `Qwen Suite Installers` อยู่ที่นั่นครบ และใช้โลโก้ `E:\localllm\qwen-suite.ico` แล้ว (24 ก.ย. 2026)
  — ข้อความเดิมตรงนี้ที่ว่า "shortcut หายไปหมด" ผิด เกิดจากการไปตรวจโฟลเดอร์ Desktop ผิดที่
- ตัวติดตั้ง: Online v1.3 อยู่ที่ `…\installer\QwenSuite\outputs\Qwen-Setup-Online.exe`
  Offline v1.3 คือชุด `Qwen-Setup-Offline.exe + 32 .bin` ในโฟลเดอร์เดียวกัน (~58 GB) และชุดเก่าที่ยังใช้ได้คือ
  `E:\QwenSuiteInstallers\v1.1`

---

## eGPU ผ่าน Thunderbolt 3 (RTX 4090 / RTX 4060 Ti) — เพิ่ม 29 ก.ย. 2026

### ระบบทำอะไรให้เอง

ทุกครั้งที่เซิร์ฟเวอร์ Ollama เริ่ม (ไม่ว่าจะผ่าน launcher ตัวไหน) `Resolve-OllamaGpu.ps1` จะเลือก GPU ให้ก่อนสตาร์ท:

| สิ่งที่เสียบอยู่ | Ollama ใช้ | ตั้งค่าอะไร (เฉพาะโปรเซสของ Ollama ไม่แตะรีจิสทรี) |
|---|---|---|
| RTX 4090 หรือ RTX 4060 Ti (compute ≥ 7.0 และหน่วยความจำ ≥ 6 GB) | **เฉพาะ eGPU** — P620 ถูกซ่อนเสมอ | `CUDA_VISIBLE_DEVICES=<UUID ของการ์ด>` · `OLLAMA_LLM_LIBRARY=cuda_v12` (มีโค้ดสำเร็จรูปสำหรับ Ada ไม่ต้องคอมไพล์ตอนโหลดแรก) · `OLLAMA_VULKAN=0` (ไม่งั้น P620 กลับมาทาง Vulkan) |
| ไม่มี eGPU (ทาง A) | Quadro P620 ตามเดิม (Ollama เลือกเอง = profile `internal`) | ไม่ตั้งอะไรเลย · **วัดจริง 29 ก.ย.**: P620 ช่วยให้อ่าน prompt เร็วกว่า CPU อย่างเดียว **3.5 เท่า** (21.2 vs 4.9–6.1 tok/s) แม้ตอบช้ากว่าราว 15% (1.66 vs 1.9 tok/s) — prompt ของ OpenClaw ยาวถึง ~20k tokens จึงคุ้มกว่ามาก |
| ไม่มี eGPU (ทาง B — P620 ปิดอยู่) | **CPU อย่างเดียว** (profile `internal`, บันทึก `cpu_only: true` และหมายเหตุ "P620 disabled") | ไม่ตั้งอะไร — Ollama ไม่เจอการ์ด NVIDIA ที่ใช้ได้ และตัด Intel UHD ทิ้งเอง ("dropping integrated GPU") |

- ผลการเลือกเขียนไว้ที่ `data\gpu-profile.json` และ `logs\ollama-gpu-profile.log`
- **เสียบ/ถอดระหว่างใช้งานได้**: `watch-ollama-gpu.ps1` (เริ่มพร้อมเซิร์ฟเวอร์ ตรวจทุก 45 วินาทีจาก Device Manager โดยไม่ปลุก GPU)
  เห็นการเปลี่ยนแปลงสองรอบติดกันแล้วรีสตาร์ท Ollama ผ่าน launcher ตัวเดิม
  - เสียบการ์ดเพิ่ม / เพิ่งติดตั้งไดรเวอร์ → รอให้ว่าง (llama-server ไม่ใช้ CPU) สูงสุด 10 นาทีแล้วรีสตาร์ท
  - การ์ดที่กำลังใช้หายไป → รีสตาร์ททันที (เซิร์ฟเวอร์พังไปแล้วอยู่ดี)
  - เครื่อง sleep ขณะใช้ eGPU → รีสตาร์ทเมื่อว่าง (ลิงก์ Thunderbolt รีเซ็ตระหว่าง Modern Standby)
  - "ว่าง" = 60 วินาทีติดที่ไม่มีงานเลย (runner ไม่ใช้ CPU, ไม่มี request จบ, ไม่มีการดาวน์โหลดโมเดล) · ถ้า 10 นาทียังไม่ว่าง จะยังไม่รีสตาร์ทและลองใหม่ทีหลัง (ไม่ตัดงานที่กำลังทำ)
  - จำกัด 3 ครั้งต่อ 30 นาที (การ์ดที่ใช้อยู่หลุดยังรีสตาร์ทได้ 1 ครั้งต่อการเปลี่ยนแปลง) · ถ้ารีสตาร์ทสองรอบแล้วสาเหตุไม่หาย จะหยุดลองและเขียน log แทน
  - ถ้าไม่มีเซิร์ฟเวอร์รันอยู่ (เช่นสั่ง `stop-all.ps1`) จะไม่สตาร์ทให้ — ยกเว้นกรณีที่การรีสตาร์ทถูกขัดจังหวะกลางทาง
  - Router จะใช้กฎ "การ์ดใหญ่" ก็ต่อเมื่อ watcher ยืนยันจาก log แล้วว่า Ollama เจอการ์ดจริง (`vram_class`: pending → large/small/none)
- **Smart Model Router 1.3**: คำถามสั้นและ structured/RAG ส่งไป `granite4.2:8b`; งาน tools/agent และ review ส่งไป `muse-glimmer:30b`
  · ระหว่างยังไม่มีน้ำหนักใหม่จะ fallback ไป `qwen3:4b` และ `gpt-oss:20b` ตามบทบาท
- **Video Studio**: ถ้าเสียบการ์ด 24 GB storyboard จะเขียนด้วย `qwen3.8:27b` แทนการตกไปใช้ storyboard สำเร็จรูป

### ก่อนเสียบครั้งแรก (ต้องทำเอง — Claude ทำให้ไม่ได้)

**1. ไดรเวอร์ — ตัวที่ติดตั้งอยู่ (Dell 581.95) ไม่รู้จักการ์ดทั้งสองใบ** (ไฟล์ `oem318.inf` ไม่มี `DEV_2684`/`DEV_2803`)
Windows ใช้ไดรเวอร์ NVIDIA ได้ชุดเดียวสำหรับทุกการ์ด และ R580 เป็นรุ่นสุดท้ายที่รองรับ P620 (Pascal) จึงมีสองทาง:

| | ทาง A: GeForce **581.80** WHQL (4 พ.ย. 2025) | ทาง B: GeForce ตัวล่าสุด (**617.14**, 22 ก.ย. 2026) + ปิด P620 |
|---|---|---|
| P620 | ยังใช้ได้ (Altium และโปรแกรมที่ใช้การ์ดแยก) | ใช้ไม่ได้ — ต้อง Disable ใน Device Manager (จอภาพไม่กระทบ: บน 3551 พอร์ต HDMI/USB-C DP ต่อกับ Intel) |
| ความปลอดภัย | ไม่ได้ patch CVE-2025-33217/33218 — **เท่ากับไดรเวอร์ที่ใช้อยู่วันนี้** | ได้ patch |
| NVENC ใน Video Studio | ไม่ได้ (FFmpeg ต้องการไดรเวอร์ ≥ 610) | ได้ |
| ความเสี่ยงที่รู้ | ไฟล์ INF ต้องมีบรรทัดของ P620 Dell — ตรวจก่อนติดตั้ง (ด้านล่าง) | Ollama issue #18581: บางเครื่องเห็นการ์ด 0 B → ใช้ `QWEN_GPU_MODE=vulkan` แทน |

ตรวจทาง A ก่อนติดตั้ง: แตกไฟล์ด้วย 7-Zip (ห้ามรัน setup) แล้ว
`findstr /i /c:"DEV_1CBD&SUBSYS_09C21028" Display.Driver\*.inf` ต้องเจอ และ
`findstr /i /c:"DEV_2684" /c:"DEV_2803" Display.Driver\nv_dispi.inf` ต้องเจอ — ถ้าบรรทัดแรกไม่เจอให้ไปทาง B

**ห้ามใช้**: ไดรเวอร์ RTX Enterprise/Quadro (ไม่ขับการ์ด GeForce และติดตั้ง RTX Desktop Manager ซึ่งคือ nView ตัวที่เคยทำ Chrome crash)
และห้ามแก้ INF / NVCleanstall
หลังติดตั้ง: ตรวจว่าไม่มี `nviewH64.dll`/`nviewH.dll` กลับมาใน `C:\Program Files\NVIDIA Corporation\nview` (`Test-QwenGpu.ps1` ตรวจให้)

#### ทาง B — ขั้นตอน (เลือกแล้ว 29 ก.ย. 2026 · ทำเองตามลำดับ ต้องใช้สิทธิ์ admin)

ตรวจข้อมูลแล้ว (29 ก.ย. 2026): ไดรเวอร์ล่าสุด = GeForce Game Ready **617.14** (22 ก.ย., R615) หรือ Studio **616.92** (9 ก.ย., R615) —
รองรับ Windows 10 64-bit, RTX 4090 และ RTX 4060 Ti, CUDA 13.4 (runner `cuda_v12` ของ Ollama ใช้ได้) ·
R615 **ไม่รองรับ P620** (R580 เป็นรุ่นสุดท้ายของ Pascal) จึงต้องปิด P620 · ไฟล์ติดตั้งจะไม่ยอมรันถ้าไม่เห็นการ์ดที่รองรับ → ต้องเสียบ eGPU ก่อนรัน

0. **เตรียม** — ดาวน์โหลด 617.14 จาก nvidia.com (GeForce RTX 40 Series → RTX 4090 → Windows 10 64-bit) ลง **D:** หรือ **E:** (C: เหลือ ~6 GB ไม่พอแตกไฟล์) ·
   ตรวจ Properties → Digital Signatures = NVIDIA Corporation · สร้าง System Restore point ·
   สำรองไดรเวอร์ P620: `pnputil /export-driver oem318.inf D:\DriverBackup\Dell-P620-581.95` · หยุดระบบ: `Stop-Qwen-Suite.ps1`
1. **กัน Windows ใส่ไดรเวอร์ Dell กลับให้ P620 — ข้อนี้สำคัญที่สุด**
   (Windows Update เคยติดตั้งไดรเวอร์ Dell ให้ P620 เองเมื่อ 8 ส.ค. 2026 · ทุกการ์ด NVIDIA ใช้ไดรเวอร์ตัวเดียวกัน ถ้าเกิดซ้ำหลังทาง B ทุกการ์ดจะกลับเป็น R580 แล้ว RTX ใช้ไม่ได้)
   - `gpedit.msc` → Computer Configuration → Administrative Templates → System → Device Installation → Device Installation Restrictions →
     **Prevent installation of devices that match any of these device IDs** → Enabled → Show… ใส่สองบรรทัด
     `PCI\VEN_10DE&DEV_1CBD&SUBSYS_09C21028` และ `PCI\VEN_10DE&DEV_1CBD`
   - **ไม่ติ๊ก** "Also apply to matching devices that are already installed" · **ห้าม**ใส่ `PCI\VEN_10DE` เฉย ๆ หรือรหัส `CC_03xx` (จะบล็อก RTX/Intel ไปด้วย)
   - `gpupdate /force` · Dell Command | Update: Settings → Update Filter → เอา **Video** ออก (หรือตั้งเป็น manual) · SupportAssist: ปิดการติดตั้งอัปเดตอัตโนมัติ
2. **ปิด P620**: Device Manager → Display adapters → NVIDIA Quadro P620 → **Disable device** (ไม่ใช่ Uninstall) · จอภาพยังปกติ (Intel ขับจอทุกช่อง)
3. **เสียบ eGPU หลังล็อกอินแล้ว**: เปิดไฟกล่อง → เสียบสาย → Thunderbolt Control Center: **Always connect** →
   Device Manager ต้องเห็นการ์ดใหม่ (Microsoft Basic Display Adapter) ก่อนไปข้อ 4
4. **ติดตั้ง**: คลิกขวาไฟล์ → Run as administrator → โฟลเดอร์แตกไฟล์บน D:/E: → NVIDIA Graphics Driver (NVIDIA App ไม่ต้อง) →
   Custom (Advanced) → ✔ **Perform a clean installation** → เสร็จแล้ว **รีบูต** โดย eGPU ยังเสียบอยู่
5. **ตรวจ** หลังล็อกอิน (ระบบสตาร์ทเอง; ถ้า Ollama ขึ้นก่อนการ์ดพร้อม watcher จะรีสตาร์ทไปใช้ RTX ให้ภายในไม่กี่นาที) — เปิด **Qwen GPU Check** ต้องเห็น
   - `kernel driver used by every NVIDIA device: 617.14 (R615 ...) - path B`
   - `NVIDIA Quadro P620 ... Code 22: disabled in Device Manager (path B - intended)` และ `Windows may not install any driver for the P620` (OK)
   - `using NVIDIA GeForce RTX 4090 ...` และ `inference device: NVIDIA GeForce RTX 4090` · จากนั้น `Test-QwenGpu.ps1 -Bench` วัดความเร็วจริง
6. **ย้อนกลับ** (ถ้าจำเป็น): ปิด policy ข้อ 1 ก่อน → ถอน "NVIDIA Graphics Driver 617.14" ใน Apps & Features (ไม่ใช่ Device Manager) → รีบูต →
   Enable P620 → `pnputil /add-driver D:\DriverBackup\Dell-P620-581.95\*.inf /subdirs /install`

ราคาของทาง B:
- **ไม่ได้เสียบ eGPU = CPU อย่างเดียว** อ่าน prompt ~5–6 tok/s (ทาง A ที่ P620 ช่วยได้ 21.2) — prompt ~20k tokens ของ OpenClaw
  อ่านรอบแรกราว 1 ชม. แทน ~16 นาที ถ้าใช้ Telegram/WhatsApp เป็นหลัก **ควรเสียบ eGPU ค้างไว้**
- Windows 10: NVIDIA ออกไดรเวอร์ Game Ready ที่รองรับ Win10 ถึง **ตุลาคม 2026** เท่านั้น จากนั้นมีแค่ security update รายไตรมาส (ถึง ต.ค. 2029)
  — "ตัวล่าสุด" บนเครื่องนี้จะหยุดที่รุ่นเดือนตุลาคม
- NVIDIA ไม่ได้รับรอง RTX 40 ผ่าน eGPU บน Windows อย่างเป็นทางการ (ใช้กันทั่วไป) · ถอดต้อง stop งาน → Safely remove → ถอดสาย

**2. กล่องต่อและไฟ**
- **ต่อได้ครั้งละใบ**: เครื่องมี Thunderbolt 3 พอร์ตเดียว (Titan Ridge 2C, PCIe 3.0 x4 ≈ 2.2–2.8 GB/s) และช่วงที่อยู่หน่วยความจำหลังพอร์ต 544 MB ไม่พอสำหรับสองการ์ด (ใบที่สองจะขึ้น Code 12)
- **ROG Strix 4090** (357.6 × 149.3 × 70.1 มม., 3.5 สล็อต, หัว 16-pin, ASUS แนะนำ PSU 1000 W): ใส่ Razer Core X / Core X Chroma / Sonnet 750ex / Mantiz Saturn Pro II **ไม่ได้**
  → ใช้ dock แบบเปิด TB3 (เช่น ADT-Link R43SG-TB3) + PSU ATX ≥ 850 W ที่มีสาย 12V-2x6 แท้ · Razer Core X V2 ขนาดพอดี แต่ Razer ไม่ได้รับรองเครื่อง TB3
- **TUF 4060 Ti** (300 × 139 × 62.4 มม., 8-pin, 160 W): Razer Core X ได้ถ้าใส่ลง (หนากว่าสเปก 60 มม. อยู่ 2.4 มม. ต้องลองจริง) หรือ dock แบบเปิด
- ช่อง USB-C ช่องเดียวจะถูก eGPU ใช้ → ชาร์จโน้ตบุ๊กด้วยอะแดปเตอร์หัวกลม 90 W หรือไฟ PD จากกล่อง (Core X 100 W)
- **อย่าติดตั้ง** ASUS GPU Tweak III / Aura Sync / Armoury Crate — ไม่จำเป็นสำหรับ CUDA และมี overlay ที่ฉีดเข้าโปรแกรมอื่นแบบเดียวกับ nView
- **อย่าต่อจอเข้ากับ eGPU** ให้เป็นการ์ดคำนวณล้วน และตั้ง Chrome/โปรแกรมอื่นเป็น "Power saving" ใน Settings → Graphics ถ้า Windows ย้ายไปใช้ eGPU

**3. Thunderbolt**
- เสียบครั้งแรกต้องนั่งอยู่หน้าเครื่อง: Thunderbolt Control Center → **Always Connect** (สั่งผ่าน Telegram ไม่ได้)
- เครื่องนี้เปิด Kernel DMA Protection บน Windows 10 → การ์ดจะเริ่มทำงาน **หลังล็อกอิน/ปลดล็อกหน้าจอ** เท่านั้น (**อย่าปิดการป้องกันนี้**)
- BIOS ไม่ต้องแก้ (Thunderbolt เปิดอยู่, Security = User Configuration)

### ใช้งานประจำวัน

- **เสียบ**: เปิดไฟกล่อง → เสียบสาย → ปลดล็อกหน้าจอ → ภายในราว 2 นาที (หรือหลังงานที่ค้างอยู่เสร็จ) watcher จะรีสตาร์ท Ollama ให้เอง
  ไม่อยากรอ: รัน `Restart-QwenOllama.ps1`
- **ตรวจ**: `Test-QwenGpu.ps1` (อ่านอย่างเดียว) · `Test-QwenGpu.ps1 -Bench` วัดความเร็ว `qwen3.8:27b` จริง ·
  `-EvictionCheck` ดูว่า embedding ไล่โมเดลหลักออกจาก VRAM หรือไม่
  ต้องเห็น `inference device: NVIDIA GeForce RTX ...` และ `offloaded 66/66 layers to GPU`
- **ถอด**: รัน `stop-all.ps1` (หรือรอให้ไม่มีงาน) → ไอคอน NVIDIA ที่ถาด "Disconnect GPU" / Safely remove → ถอดสาย
  ถ้าดึงสายขณะกำลังตอบ llama-server จะพังและ watcher จะรีสตาร์ทกลับไปโหมดไม่มี eGPU ทันที
- **บังคับโหมด** ด้วยตัวแปร User `QWEN_GPU_MODE` แล้วรัน `Restart-QwenOllama.ps1`:
  `auto` (ค่าเริ่มต้น) · `cpu` (ไม่ใช้ GPU เลย) · `all` (ให้ Ollama เลือกเอง รวม P620 = พฤติกรรมก่อน 29 ก.ย.) ·
  `vulkan` (ใช้เมื่อ CUDA มองไม่เห็นการ์ด — issue #18581 · **ใช้กับทาง B ที่ปิด P620 แล้วเท่านั้น** ไม่งั้น Vulkan จะใช้ P620 ร่วมด้วย)
  ถ้าตั้ง `OLLAMA_LLM_LIBRARY` เองในรีจิสทรี ระบบจะไม่แตะอะไรเลย (โหมด manual)

### ความเร็วที่คาด (ยังเป็นค่าประมาณจนกว่าจะวัดจริงด้วย `-Bench`)

| `qwen3.8:27b` | อ่าน prompt | ตอบ | ที่มา |
|---|---|---|---|
| ไม่มี eGPU, P620 ช่วย (ค่าเริ่มต้น) | **21.2 tok/s** | 1.66 tok/s | วัด 29 ก.ย. prompt 1,090 tokens |
| ไม่มี eGPU, CPU อย่างเดียว (ทาง B หรือ `QWEN_GPU_MODE=cpu`) | 4.9–6.1 tok/s | 1.9 tok/s | วัด 29 ก.ย. |
| RTX 4060 Ti 8 GB (โมเดลใส่ไม่หมด ส่วนที่เหลืออยู่ CPU) | ~100–300 | ~2–3 | ประมาณ |
| RTX 4090 24 GB (โมเดลอยู่ใน VRAM ทั้งตัว) | ~2,000–4,000 | ~35–45 | ประมาณ — ผ่าน TB3 การตอบใกล้เคียงต่อตรง ส่วนการอ่าน prompt ช้าลงราวครึ่งหนึ่ง |

- 4060 Ti ใส่ `granite4.2:8b` หรือ `qwen3-embedding:4b` ได้ แต่โมเดล 27B/30B ต้องแบ่งกับ CPU; Muse/Qwen-Image แนะนำ RTX 4090 24 GB
- 4090: `qwen3.8:27b` ที่ context 32k ใช้ราว 19.8 GB รวม vision และ margin — พอดีการ์ด 24 GB แต่จะไม่เหลือที่ให้โมเดลอื่นพร้อมกัน
  เรียกโมเดลอื่น (embedding, `qwen3:4b`) จะไล่ `qwen3.8` ออกแล้วต้องโหลดกลับ ~10–20 วินาที — router/Video Studio เลี่ยงให้แล้ว ส่วน embedding ตรวจด้วย `-EvictionCheck`
- โหลดโมเดล 17 GB ผ่าน TB3 ครั้งแรกราว 10–20 วินาที (keep_alive 2 ชม. จึงไม่บ่อย)

### แก้ปัญหา

| อาการ | สาเหตุ / ทางแก้ |
|---|---|
| `Test-QwenGpu.ps1` บอก "NO NVIDIA DRIVER (Microsoft Basic Display)" | ยังไม่ได้ติดตั้งไดรเวอร์ที่รู้จักการ์ด (ข้อ 1) |
| Code 43 บน P620 หรือ eGPU | ไดรเวอร์คนละรุ่นกัน / Windows Update หรือ Dell เปลี่ยนให้ → ติดตั้งชุดที่เลือกใหม่แบบ clean · ทาง B: P620 ขึ้น Code 43/28 = ยังไม่ได้ปิด → Disable |
| `Test-QwenGpu.ps1`: "installed for 617.xx, but the kernel driver running is 581.xx" | มีอะไรติดตั้งไดรเวอร์ R580 ทับ (Windows Update / Dell) → ตรวจ policy ข้อ 1 แล้วติดตั้ง 617.xx ใหม่แบบ clean |
| `Test-QwenGpu.ps1`: "nothing blocks a driver install for the P620" | ยังไม่ได้ทำข้อ 1 ของทาง B |
| Code 12 | เสียบสองการ์ดพร้อมกัน → ถอดใบหนึ่ง |
| profile เป็น gpu แต่ log ไม่มี RTX / VRAM 0 B | issue #18581 → ปิด P620 แล้วตั้ง `QWEN_GPU_MODE=vulkan` |
| Chrome ปิดตัวเองทันทีหลังติดตั้งไดรเวอร์ | nView hook กลับมา → เปลี่ยนชื่อ `nviewH64.dll`/`nviewH.dll` เป็น `.bak` อีกครั้ง |
| อยากย้อนทุกอย่างกลับ | `QWEN_GPU_MODE=all` แล้ว `Restart-QwenOllama.ps1` · ไฟล์เดิมก่อนแก้อยู่ที่ `backups\claude-20260929-egpu\` |

อย่าอัปเดต Ollama (มีตัว 0.33.2 ค้างรออยู่ในแอป tray) พร้อมกับเปลี่ยนไดรเวอร์ — เปลี่ยนทีละอย่างจะได้รู้ว่าอะไรทำให้พัง
- ตัวติดตั้ง 0.33.2 ดาวน์โหลดไว้ที่ `%LOCALAPPDATA%\Ollama\updates_v2\...\OllamaSetup.exe` แต่จะติดตั้ง**เอง**ก็ต่อเมื่อแอป tray ถูกเปิดแบบซ่อน
  (เช่นย้าย `disabled-startup\Ollama.lnk` กลับเข้า Startup) หรือกด "Restart to update" — ตอนนี้ไม่มีอะไรเปิดแอป tray (ตรวจ Startup, Run keys, scheduled tasks แล้ว)
- ถ้าจะปิดการอัปเดตอัตโนมัติด้วย: ตั้ง "Auto-download updates" ในแอป tray เป็นปิด — แต่ต้องทำตอนหยุดชุดนี้แล้ว (`stop-all.ps1` ก่อน เพราะเปิดแอป tray จะฆ่าเซิร์ฟเวอร์) แล้วค่อย `Restart-QwenOllama.ps1 -StartIfStopped`
- ติดตั้งเองเมื่อพร้อม (หลังไดรเวอร์ใหม่ทดสอบผ่าน): ตอน Ollama ว่าง รันตัวติดตั้ง → มันจะปิด Ollama และเปิดแอป tray → ปิดแอป tray แล้วรัน `Restart-QwenOllama.ps1 -StartIfStopped` เพื่อให้ขั้นเลือก GPU ทำงานอีกครั้ง

| ไฟล์ใหม่ | ทำอะไร |
|---|---|
| `Resolve-OllamaGpu.ps1` | ตัดสินใจเรื่อง GPU ก่อน Ollama สตาร์ท (launcher ทั้งสองตัวเรียก) |
| `watch-ollama-gpu.ps1` | เฝ้าการเสียบ/ถอด แล้วรีสตาร์ท Ollama · log `logs\ollama-gpu-watch.log` |
| `Restart-QwenOllama.ps1` | รีสตาร์ทเฉพาะ Ollama (ไม่แตะ WebUI/tools) ผ่าน launcher เดิม · สั่งจากแชทได้ ไม่มีผลเสีย |
| `Test-QwenGpu.ps1` | ตรวจไดรเวอร์/การ์ด/โปรไฟล์/log · ส่วน "NVIDIA driver" ตรวจทาง B (ไดรเวอร์ที่ทุกการ์ดใช้จริง, แพ็กเกจเก่าใน driver store, policy กันไดรเวอร์ Dell) · `-SelfTest` · `-Bench` · `-EvictionCheck` |

## เลือกหน่วยประมวลผลเอง (CPU / GPU ใบไหน) — เพิ่ม 29 ก.ย. 2026

ค่าเริ่มต้นคือ **อัตโนมัติ** (หัวข้อ eGPU ด้านบน) แต่เลือกเองได้ 2 ระดับ:

**1. ทั้งเซิร์ฟเวอร์** — อัตโนมัติ / CPU อย่างเดียว / GPU ใบที่ระบุ (P620, RTX 4090, RTX 4060 Ti หรือหลายใบ)

| ช่องทาง | วิธีใช้ |
|---|---|
| หน้าเว็บ | http://127.0.0.1:8765/compute (ไอคอน **Qwen Compute Device** บน Desktop) — คลิกการ์ดแล้วกด "ใช้ตัวที่เลือก" |
| เมนู | `Select-QwenCompute.ps1` แล้วพิมพ์หมายเลข · หรือ `Select-QwenCompute.ps1 -Device 4090` / `-Device cpu` / `-Device auto` |
| แชท (WebUI / Telegram / WhatsApp) | พิมพ์เช่น "ใช้ GPU 4090", "ใช้ CPU", "กลับเป็นอัตโนมัติ" — โมเดลเรียก `compute_device` เอง |

- การสลับเกิดตอน **Ollama ว่าง** (watcher รีสตาร์ทให้ ~1–2 นาทีหลังคำตอบปัจจุบันจบ) · หน้าเว็บ/เมนูเลือก "ทันที" ได้
- เลือก CPU จาก **แชท** จะกลับเป็นอัตโนมัติเองใน **4 ชั่วโมง** (ระบุชั่วโมงเองได้) เพราะบน CPU ล้วน prompt ยาวของ OpenClaw ใช้เวลาเกิน timeout — ถ้าไม่ตั้งเวลา อาจสั่งกลับจาก Telegram ไม่ได้
- เลือก GPU ที่ไม่ได้เสียบอยู่ → ใช้อัตโนมัติไปก่อน แล้วสลับให้เองเมื่อเสียบ
- เลือกอะไรไว้แล้ว การเสียบ/ถอดการ์ดอื่นจะ**ไม่**ทำให้รีสตาร์ทโดยไม่จำเป็น (ยกเว้นการ์ดที่ใช้อยู่หลุด)
- ถ้าตั้ง `OLLAMA_LLM_LIBRARY` หรือ `CUDA_VISIBLE_DEVICES` เองในตัวแปร User ค่านั้นชนะทุกอย่าง (หน้าเว็บจะเตือน)
- "CPU" ในเครื่องนี้มีตัวเดียว (i7-10750H 6 คอร์ 12 เธรด) — Ollama ไม่มีตัวเลือกผูกคอร์

**2. รายโมเดล** — บังคับให้โมเดลใดใช้ CPU เสมอ ทุกช่องทาง (WebUI, RAG, Telegram, Video Studio)
- หน้าเว็บส่วนล่าง หรือ `Set-QwenModelPlacement.ps1 -Model qwen3-embedding:4b -Placement cpu [-NumCtx 8192]` · กลับ: `-Placement auto`
- ประโยชน์หลัก: เมื่อมี eGPU ใหญ่ ให้ `qwen3-embedding:4b` อยู่ CPU จะได้ไม่ไล่ `qwen3.8` ออกจาก VRAM ทุกครั้งที่ค้น RAG
- ทำโดยสร้าง tag เดิมใหม่พร้อม `PARAMETER num_gpu 0` (ไม่ copy น้ำหนักโมเดล) · สำรอง manifest + blob เล็กไว้ที่ `data\placement-backups\` · "auto" คืนค่าเดิม**ตรงทุกไบต์** (ทดสอบแล้วกับ tag สำเนา 29 ก.ย.)
- `ollama pull` โมเดลนั้นใหม่จะล้างการตั้งค่านี้ → สถานะขึ้น **lost** (`Test-QwenGpu.ps1` และหน้าเว็บเตือน) · สั่ง `auto` แค่ลบบันทึก ไม่ย้อนโมเดลที่ pull ใหม่
- ถ้าขั้นตอนถูกขัดกลางทาง สถานะขึ้น **pending** → สั่ง `auto` เพื่อคืนต้นฉบับจาก backup
- เปลี่ยน `-NumCtx` ของโมเดลที่ตั้งไว้แล้ว → คืนต้นฉบับก่อนแล้วตั้งใหม่ (ไม่มีค่าเก่าค้าง) · โมเดลที่มี `num_gpu` อยู่แล้วจากที่อื่นจะไม่ถูกแตะ

| ไฟล์ | หน้าที่ |
|---|---|
| `Select-QwenCompute.ps1` | เลือกทั้งเซิร์ฟเวอร์ → `data\compute-selection.json` |
| `Set-QwenModelPlacement.ps1` | เลือกรายโมเดล → `data\model-placement.json` |
| `local-tools\compute_control.py`, `compute_center.html` | tool แชท `compute_device` / `list_compute_devices` และหน้าเว็บ `/compute` |

## ความสามารถแบบ Claude ที่เพิ่ม — 30 ก.ย. 2026

| ความสามารถ | ใช้ที่ไหน | วิธีใช้ |
|---|---|---|
| ทำไฟล์ Word / Excel / PowerPoint / PDF | WebUI + Telegram | พิมพ์เช่น "ทำรายงานเป็นไฟล์ Word เรื่อง…" · ไฟล์อยู่ที่ `E:\qwen-projects\<โปรเจกต์>\outputs\` (ไม่ทับไฟล์เดิม ใช้ฟอนต์ TH Sarabun New) · บน Telegram บอทส่งไฟล์กลับมาในแชท |
| ค้นเว็บ | Telegram (ใหม่) · WebUI มีอยู่แล้ว | "ค้นข้อมูลล่าสุดเรื่อง… พร้อมลิงก์" — ผลค้นเว็บถือเป็นข้อมูลไม่น่าเชื่อถือ โมเดลต้องอ้าง [n] พร้อม URL |
| รันโค้ด Python วิเคราะห์ข้อมูล | WebUI | กดปุ่ม **Code Interpreter** ในช่องพิมพ์ (รันในเบราว์เซอร์ ไม่แตะไฟล์ในเครื่อง) — ไม่กดก็ไม่เสียเวลา |
| ค้นความจำเดิม | Telegram | `memory_search` / `memory_get` ใช้ได้แล้ว — ค้นด้วยคำ (trigram รองรับภาษาไทย) ในเครื่อง ไม่ส่งข้อความไป OpenAI (`memory.search.provider = none`) |
| ตอบแบบทยอยขึ้น | Telegram | ข้อความค่อย ๆ ขึ้นระหว่างพิมพ์ (streaming partial) |
| สรุปแชทยาวอัตโนมัติ | WebUI | เกิน 24,000 token จะสรุปส่วนเก่าด้วย `qwen-fast` (ไม่คิดยาวเกินงบ) |
| พูดสั่งงานภาษาไทย | WebUI | ปุ่มไมค์ → Whisper ในเครื่อง ตัดช่วงเงียบ · ภาษาไทยเป็นค่าเริ่มต้นใน Settings › Audio (เปลี่ยนเป็นอังกฤษได้) |
| ปุ่มงานตัวอย่างภาษาไทย | WebUI | หน้าแชทใหม่ (สรุปเอกสาร, ทำไฟล์, ค้นเว็บ, ร่างหนังสือ, แปล, เขียนสคริปต์) |

ความปลอดภัยที่เพิ่ม (ไม่มีการถามยืนยันเพิ่ม — ถ้าเข้าข่ายจะ**ปฏิเสธ**พร้อมเหตุผลทันที):
- คำสั่งที่แตะที่เก็บรหัสผ่าน (Credential Manager, DPAPI, `Login Data` ของเบราว์เซอร์, `.openclaw\credentials`) และคำสั่งลบดิสก์/shadow copy ถูกบล็อก
- Open WebUI รับคำขอจากเบราว์เซอร์เฉพาะหน้า `http://127.0.0.1:3000` / `http://localhost:3000` (เดิม `*` — เว็บอื่นที่เปิดในเครื่องสั่งงานได้เพราะไม่มีล็อกอิน)

ปรับเพิ่ม:
- Router จับคำรูปอื่นด้วย (bugs, debugging, python3, APIs, github, sqlite …) และข้อความต่อเนื่องที่ไม่มีคำบอกงาน ("ทำให้เร็วขึ้นอีก") จะอยู่กับโมเดลเดิมของ 3 ข้อความล่าสุด — ไม่สลับโมเดลไปมา
- ชื่อแชทของ WebUI สร้างด้วย `qwen-fast` (เดิมเข้า coder 30B เพราะ prompt มีคำว่า "code" และไล่ qwen3.8 ออกจากหน่วยความจำ — Ollama โหลดได้ทีละ 1 โมเดล)
- `qwen-fast` ใช้ Granite 4.2 8B โดยตรง; ถ้ายังไม่ติดตั้งจะ fallback ไป qwen3:4b
- ตัวติดตั้ง (`Configure-Qwen-WebUI.py`, `Configure-OpenClaw.py`) เขียนค่าชุดนี้ด้วย ติดตั้งใหม่ไม่ย้อนค่า (ยกเว้น monitor scratch ของ heartbeat ที่อยู่ในฐานข้อมูลของ gateway — ต้องตั้งใหม่ด้วยคำสั่งด้านล่าง)

Heartbeat ของ OpenClaw: เดิมทุก 30 นาทีจะรันโมเดลค้นหา HEARTBEAT.md ที่ไม่มีอยู่จนหมดเวลา 30 นาที (เครื่องไม่ว่างทั้งวัน) — ตอนนี้ตั้ง "monitor scratch" ว่างไว้ ระบบจึงข้ามรอบโดยไม่เรียกโมเดล · อยากให้มันตรวจอะไรเป็นประจำ ให้เพิ่มบรรทัดงานด้วย
`openclaw.cmd cron scratch 6b8fb59d-5d00-4288-8925-c39b63a2385e --file <ไฟล์.md>` (`--unset` = กลับแบบเดิม)
เครื่องที่ติดตั้งใหม่จากตัวติดตั้ง 1.4: `Configure-OpenClaw.py` ตั้ง `heartbeat.every = "0m"` (ปิดรอบประจำ แต่งานเบื้องหลังที่จบแล้วยังปลุกได้) เพราะ scratch ตั้งได้หลัง gateway ขึ้นแล้วเท่านั้น — อยากใช้ heartbeat ให้ตั้ง `every` เป็น `30m` แล้วใส่งานใน scratch

ข้อควรระวังที่เจอ:
- ค่าบางตัวของ OpenClaw (เช่น `memory.search.*`) ต้องรีสตาร์ท gateway และ gateway อาจ**ไม่ขึ้นเอง** → `Start-ScheduledTask 'OpenClaw Gateway'`
- ค่าของ Open WebUI 0.11 อยู่ในตาราง `config` ของ `webui-data\webui.db` และอ่านสดทุกคำขอ · ค่าเดิมก่อนแก้: `backups\claude-20260930-features\webui-config-before.json`
- อย่าต่อ pipe (`| ...`) กับ `start-local-tools.ps1` / `start-ai-services.ps1` — เซิร์ฟเวอร์ลูกถือ pipe ไว้ ทำให้คำสั่งค้างไม่จบ

---

## ความสามารถแบบ Codex — 30 ก.ย. 2026


> หมายเหตุ (30 ก.ย. ตรวจทานรอบสุดท้าย): `/status` คาด `OLLAMA_MAX_LOADED_MODELS=2` ตามตาราง Ollama ด้านบน (เปลี่ยนได้ด้วย `QWEN_EXPECTED_MAX_LOADED`) · ไฟล์กฎ `data\permission-center\command-rules.json` และ `hooks.json` อ่านได้ทั้ง UTF-8 (มี/ไม่มี BOM) และ UTF-16 — ถ้าไฟล์เสีย ระบบใช้กฎชุดล่าสุดที่อ่านได้ (หรือค่าเริ่มต้น) ต่อ ไม่ปิดกฎทิ้ง · คำสั่งจากแชทที่แตะโฟลเดอร์ `.codex` / `.openclaw` / `.claude` / `codex-home` ถูกบล็อก (มีโทเค็นและบทสนทนาเต็ม) — จึงสั่ง `Invoke-QwenCodex.ps1` จากแชทไม่ได้ ตรงกับที่ Codex-Qwen ใช้จากเดสก์ท็อปเท่านั้น
ไล่เทียบกับ OpenAI Codex ทั้ง 66 หัวข้อ แล้วเพิ่มเฉพาะส่วนที่คุ้มบนเครื่อง CPU นี้ (qwen3.8 ~1.4 tok/s) · งานส่วนใหญ่**ไม่เรียกโมเดลเพิ่ม** (แปลงเวลา สแกนโฟลเดอร์ ตรวจสถานะ ทำด้วยโค้ด) · **ไม่มีการถามยืนยันเพิ่ม**
ใช้ได้เมื่อลงทะเบียน tool ใน `local-tools\server.py` / `fast_server.py` / `permission_manager.py` และรีสตาร์ต local-tools แล้ว — ถ้าโมเดลตอบ `Unknown action` แปลว่ายังไม่ได้ทำขั้นนั้น

### ใช้อะไรได้บ้าง

| ความสามารถ | ใช้ที่ไหน | วิธีใช้ (พิมพ์ได้ทั้ง Telegram และ Open WebUI เว้นแต่ระบุ) |
|---|---|---|
| แก้ไฟล์เฉพาะส่วน + ย้อนกลับ | WebUI + Telegram | "แก้ `E:\qwen-projects\X\app.py` เปลี่ยน timeout 10 เป็น 20" · "ย้อนการแก้ล่าสุดของไฟล์นี้" — `edit_text_file` ส่งแค่ข้อความเดิม→ใหม่ (~150 tokens แทนการเขียนทั้งไฟล์ ~2,500 = 20-30 นาทีบน CPU) คง encoding/BOM/CRLF · สำรองก่อนแก้หรือเขียนทับทุกครั้ง · ดู diff ใน VS Code / ย้อนกลับได้ที่ Path Manager แท็บ "การแก้ไขล่าสุด" |
| แก้เอกสาร Word / Excel / PowerPoint เดิม | WebUI + Telegram | "แก้ report.docx เปลี่ยนวันที่เป็น 1 ต.ค. 2569" · "เพิ่มแถวเดือน ต.ค. ใน budget.xlsx แล้วบอกยอดรวม" · "เพิ่มสไลด์สรุปท้าย deck นี้" · "ส่งภาพหน้าแรกของ report.docx" — ได้ไฟล์ใหม่ `<ชื่อ>-edit-<เวลา>` ใน `outputs\` (ไม่ทับต้นฉบับ) · Excel คำนวณสูตรใหม่ด้วย LibreOffice แล้วรายงานยอด/เซลล์ error · ไฟล์มีมาโครถูกปฏิเสธ |
| PDF | WebUI + Telegram | "ส่งหน้า 11 ของ B781001.pdf เป็นรูป" (ซูมบางส่วนได้) · รวม / แยก / ดึงหน้า / หมุน / กรอกฟอร์ม — ไฟล์ต้องอยู่ใน Path ของโปรเจกต์ (เพิ่ม Path ที่ Path Manager) · ค่าภาษาไทยในช่องฟอร์มพิมพ์ลงบนหน้าด้วย TH Sarabun New · PDF ติดรหัสผ่าน/ฟอร์ม XFA ทำไม่ได้ |
| แม่แบบ + กราฟ | WebUI + Telegram | "ทำบันทึกข้อความเรื่อง… เรียน…" (แม่แบบในตัว `memo-th`, `report-th`) · ส่งไฟล์หัวกระดาษของหน่วยงานแล้วบอก "จำเป็นแม่แบบ memo-egat" แล้วสั่ง "…ใช้แม่แบบ memo-egat" · "ทำกราฟแท่งยอดไฟ 4 ไตรมาส" (PNG ป้ายไทย) |
| ค้นคว้าเชิงลึก → รายงาน Word มีอ้างอิง | WebUI + Telegram | "ค้นคว้าเรื่อง… ทำรายงานให้หน่อย" — ตอบรับทันที ค้นเว็บไทย+อังกฤษเลย แต่ขั้นสรุปรอจน Ollama ว่าง 15 นาทีหรือช่วง 01:00–05:00 (พักเองถ้ามีคนแชทแทรก) · ~1 ชม. (quick) ถึง 2 ชม. (standard) · ทุก [n] ตรวจแล้วว่าตรงกับหน้าที่อ่านจริง · ได้ `outputs\research-*.docx` + แจ้งเตือน · "ทำต่อจากที่ค้าง" ใช้ `resume_id` |
| แจ้งเตือนงานเบื้องหลังเสร็จ + กล่องงานเสร็จ | Telegram + WebUI | งานเบื้องหลังที่สั่งจาก Telegram (รัน task, ทำวิดีโอ, ค้นคว้า) ส่งข้อความหาเจ้าของ 1 ครั้ง + toast · สั่งจาก WebUI = toast อย่างเดียว · บอก "ไม่ต้องแจ้งเตือน" = `notify=false` · "มีงานอะไรเสร็จบ้าง" (`inbox_list`) · Path Manager แท็บ "กล่องงานเสร็จ" · งานที่สั้นกว่า 60 วินาทีไม่แจ้ง |
| เตือนความจำ / นัดเวลา | Telegram + WebUI | "เตือนพรุ่งนี้ 8 โมงว่าโทรหาช่าง" · "ทุกวันจันทร์ 08:00 เตือนส่งรายงาน" · "ดูรายการเตือน" · "ยกเลิกเตือน <รหัส>" — ส่งเข้า Telegram ของเจ้าของตรงเวลา **ไม่เรียกโมเดล** · ถี่สุดชั่วโมงละครั้ง, ล่วงหน้าไม่เกิน 365 วัน, ค้างได้ 50 รายการ · เวลากำกวม ("3 โมง") ถูกปฏิเสธพร้อมตัวอย่าง · เครื่องหลับตอนถึงเวลา → เตือนเมื่อเครื่องตื่น |
| สถานะทั้งชุด (แบบ `/status` + doctor) | Telegram + WebUI + เว็บ | Telegram `/qstatus` · แชท "ตรวจสถานะระบบ" · http://127.0.0.1:8765/status · คอนโซล `powershell -ExecutionPolicy Bypass -File E:\localllm\local-tools\Test-QwenSuite.ps1` (exit 0 ปกติ, 1 ควรดู, 2 มีปัญหา, 3 ตรวจไม่ได้) — อ่านอย่างเดียว ไม่รีสตาร์ต ไม่เรียกโมเดล ~3-5 วินาที |
| ปุ่ม Action ของโปรเจกต์ | WebUI + Telegram + Path Manager | "ตั้ง action test ของโปรเจกต์ X ให้รัน python -m pytest" (สูงสุด 9 ต่อโปรเจกต์) · "รัน action health ของโปรเจกต์ General" · หรือกดปุ่มใต้ชื่อ Project ใน http://127.0.0.1:8765/path-manager · ตรวจนโยบายคำสั่งทุกครั้งที่รัน |
| AGENTS.md ของโปรเจกต์ (/init) | WebUI + Telegram | "สร้าง AGENTS.md ให้โปรเจกต์ X" — สแกนโฟลเดอร์โดยไม่ใช้โมเดล ไม่ทับของเดิม (มีแล้วได้ `AGENTS.proposed.md`) · `get_project_context` แนบให้โมเดลโดยติดป้ายว่าไม่น่าเชื่อถือ |
| คำสั่งลัด / สกิล | WebUI + Telegram | WebUI พิมพ์ `/` แล้วเลือก plan, review, summary, letter, minutes, drawing (แก้ได้ที่ Workspace › Prompts) · Telegram `/plan` `/letter` `/minutes` `/drawing` `/qstatus` — โหลดเฉพาะตอนเรียก ไม่เพิ่ม prompt ปกติ · `/letter` `/minutes` ได้ Word, `/drawing` ได้ Excel |
| สลับโมเดลบน Telegram | Telegram | `/model list` (กดปุ่มเลือก) · `/model ollama/qwen-code:latest -s` (เฉพาะแชทนี้) · `/model default -s` — ครั้งแรกช้าเพราะโหลดโมเดลใหม่และไล่ qwen3.8 ออก (`/model qwen-code` เฉย ๆ ใช้ไม่ได้) |
| อ่านหลายไฟล์ในรอบเดียว | WebUI + Telegram | อัตโนมัติ: `local_read` action `batch` สูงสุด 5 รายการต่อครั้ง (แต่ละรายการผ่าน Permission Center และลง audit แยก) ลดจำนวนรอบของโมเดล |
| หน้าต่างแชตด่วน / พูดแล้วได้ข้อความ | ทั้งเครื่อง | **Ctrl+Alt+Q** เปิด WebUI เป็นหน้าต่างเล็กของ Edge · **Ctrl+Alt+D** เริ่มฟัง กดอีกครั้งจบ → ถอดเสียงด้วย Whisper ในเครื่อง ข้อความอยู่ในคลิปบอร์ด (Ctrl+V) · ไฟล์เสียงลบทุกครั้ง · สูงสุด 5 นาที · ตั้งภาษา `data\dictate.json` · เปลี่ยนไมค์ `ai-services\dictate.ps1 -Mode devices` |
| ตอบเร็วขึ้นใน WebUI | WebUI | ปิดเครื่องมือในตัวที่ไม่ได้ใช้ (ปฏิทิน, automations, knowledge ฯลฯ ~8 KB ต่อข้อความ) · เปิดคืนที่ Workspace › Models › (โมเดล) › Builtin Tools |

งาน AI ตามเวลา (เช่น สรุปข่าวทุกเช้า) เจ้าของสั่งเองใน PowerShell เท่านั้น — โมเดลในแชทสร้างงานแบบนี้ไม่ได้ (ตั้งใจ):
`openclaw.cmd automations add --name <ชื่อ> --agent qwenlocal --cron "0 5 * * *" --tz Asia/Bangkok --message "..." --light-context --announce --channel telegram --to <chat id>` · ตั้งช่วง 00:00-06:00 เพราะโมเดลกินเครื่องทั้งตัว

### ความปลอดภัย (บล็อกแข็ง + เหตุผลภาษาไทย ไม่มีการถามยืนยันเพิ่ม)

- **Parsed exec-policy แทน block-list 4 regex** (`local-tools\command_policy.py`) ใช้กับ `run_command`, `project_run_command`, `start_project_task` และปุ่ม Action: แกะคำสั่งจริงออกจาก `powershell/pwsh -Command`, `-EncodedCommand` (ถอด base64), `-File` (อ่านเนื้อสคริปต์), `cmd /c`, `python -c` แล้วตัดเครื่องหมายคำพูด/backtick/caret และแปลง alias (rm/rd/del → Remove-Item, spps/kill → Stop-Process) ก่อนตรวจ ⇒ ช่องที่ห่อคำสั่งไว้ใน `powershell -Command "..."` ปิดแล้ว · ทดสอบ: คำสั่งอันตราย 51 แบบถูกบล็อกทั้งหมด (ลบซ้ำโฟลเดอร์, หยุด ollama/openclaw, ล้างถังขยะ, `git clean -fdx`, `reset --hard`, แตะที่เก็บรหัสผ่าน, format/diskpart ฯลฯ) และคำสั่งปกติ 48 แบบผ่านทั้งหมด
- กฎเพิ่มเองได้ที่ `data\permission-center\command-rules.json` (อ่านใหม่เมื่อไฟล์เปลี่ยน · ค่าเริ่มต้นจาก `local-tools\default-command-rules.json` · `start-local-tools.ps1` คัดลอกให้ครั้งแรก)
- **Hooks (บล็อกได้อย่างเดียว)**: `data\permission-center\hooks.json` (ไม่มีไฟล์ = ไม่มี hook) — Pre script ของเจ้าของรับ JSON `{source, action, arguments}` ทาง stdin แล้วคืน `{"decision":"block","reason":"..."}` เพื่อบล็อก · timeout 5 วินาที ถ้า hook พังให้ผ่าน (fail-open) · Post script รันเบื้องหลัง · ตัวอย่าง `local-tools\hooks\log_jsonl.py` · hook ที่ตรงเงื่อนไขเพิ่ม ~60 ms ต่อคำสั่ง
- ไม่มีด่านถามยืนยันใหม่ (ตามกฎของเจ้าของ 23 ก.ย. ว่าแชนแนลแชตรันคำสั่งได้โดยไม่ถาม): ภายใต้นโยบาย Allow ปัจจุบันทุก action ใหม่รันทันที · `run_project_action` ใช้กลไกเดียวกับ `run_command` จึงจะแสดงคำสั่งให้อนุมัติก็ต่อเมื่อเจ้าของตั้งหมวดนั้นเป็น Ask เอง · การแก้/เขียนทับไฟล์ด้วย `edit_text_file` / `write_text_file` / `edit_document` มีสำเนาให้ย้อน · งานค้นคว้าไม่อ่านที่อยู่ภายใน (127.x, 10.x, 192.168.x) และไม่ส่ง tools ให้โมเดล
- ข้อจำกัด: เป็นรั้ว ไม่ใช่ sandbox — สคริปต์ที่สร้างคำสั่งอันตรายขึ้นตอนรันยังหลุดได้

### Qwen Codex — Codex CLI คุยกับโมเดลในเครื่อง

- **เปิดใช้**: Start Menu › Qwen Suite › **Qwen Codex – สั่งงานเบื้องหลัง** (พิมพ์งาน เลือกโฟลเดอร์ / ประเภท อ่านอย่างเดียว-แก้ไฟล์-รีวิว / โมเดล แล้วทำเบื้องหลัง) · **Qwen Codex – โหมดโต้ตอบ (ช้า)** · สคริปต์ `local-tools\codex\Invoke-QwenCodex.ps1` (`-Exec`, `-Review`, `-Tui`, `-List`, `-Check`) และ `qwen-exec.cmd` สำหรับ Task Scheduler · คู่มือเต็ม `local-tools\codex\INSTRUCTIONS.md`
- **ทำได้**: อธิบาย/วิเคราะห์โค้ดในโฟลเดอร์ · รีวิว git repo → `<project>\outputs\review-<วันเวลา>.md` (+ `-Docx`) · งานกลางคืนที่ต้องตอบเป็น JSON ตาม schema (`-OutputSchema` บริดจ์ตรวจเอง ไม่ตรง = exit 6) · คุยต่อ `-ResumeLast` · เสร็จแล้วแจ้งเตือน · ถามสถานะจากแชทได้ ("งาน Codex ถึงไหนแล้ว" → `list_codex_jobs`)
- **ทำไม่ได้ (ตั้งใจ)**: เริ่มงานจาก Telegram/WebUI · แก้ไฟล์ จนกว่าเจ้าของจะเลือก `write_mode` (ดูหัวข้อถัดไป) · ออกเน็ต (proxy ปิด คุยได้แค่ 127.0.0.1) · แตะ `C:\Users\EGAT\.codex` (ใช้ `data\codex-home` แยก เขียน config ใหม่จากแม่แบบทุกครั้ง) · ไม่มี apply_patch — โมเดลเขียนทั้งไฟล์ · กฎคำสั่งห้ามแตะที่เก็บรหัสผ่าน ติดตั้ง/ดาวน์โหลด หยุดบริการ และ shell ซ้อน
- **ความเร็ว**: งานหนึ่ง ~10-25 นาที (รีวิว diff ≤3k tokens ~5-10 นาที) · โหลด `qwen3-coder:30b` แทน qwen3.8 ⇒ แชทถัดไปช้า (โหลดตัวหลักกลับ + อ่าน prompt ใหม่) · บริดจ์จึงรอจนไม่มีใครใช้โมเดลอื่น 15 นาที (exit 3 = Ollama ไม่ว่าง, 4 = มีงานอื่นอยู่) และคืน RAM ของ coder เองเมื่อจบ · คุณภาพการเรียก tool ของ qwen3-coder ใน Codex ยังไม่ได้พิสูจน์กับโมเดลจริง · ผลรีวิวเป็น "ความเห็นจากโมเดลในเครื่อง ต้องตรวจซ้ำ"

### ที่เก็บไฟล์ใหม่

| อะไร | ที่ไหน |
|---|---|
| สำเนาก่อนแก้ไฟล์ (30 วัน / 300 MB, ไฟล์ >25 MB ไม่มีสำเนา) | `E:\qwen-projects\.file-history\` |
| งานค้นคว้า (checkpoint ทำต่อได้) | `E:\qwen-projects\.research\jobs\<id>\` |
| แม่แบบเอกสารที่บันทึกไว้ | `E:\qwen-projects\_templates\` |
| ทะเบียนงาน + สถานะอ่านแล้ว/เก็บ | `E:\qwen-projects\.task-monitor\tasks.json` |
| เตือนความจำ | `data\reminders\` (+ OpenClaw automation ชื่อ `qwen-reminder <รหัส>`) |
| ตั้งค่าแจ้งเตือน / log (ไม่มีเลข Telegram) | `data\notify.json`, `data\notifier.log` |
| กฎคำสั่ง / hooks / audit | `data\permission-center\command-rules.json`, `hooks.json`, `audit.jsonl` |
| Qwen Codex | `data\codex-home\`, `data\codex-bridge.json`, `logs\codex-bridge\`, `<project>\.codex-runs\` |
| ถอดเสียง | `data\dictate.json` |

### รอเจ้าของเครื่องตัดสินใจ

- **รูปแบบหนังสือใน `/letter`** ทำตามระเบียบงานสารบรรณ (หนังสือภายนอก / บันทึกข้อความ) — ตรวจว่าตรงแบบของ กฟผ. หรือไม่
- **ส่งไฟล์วิดีโอทาง Telegram** — `data\notify.json` ค่า `video_media`: `true` (ตอนนี้) ส่ง mp4 ≤20 MB หรือภาพตัวอย่าง · `false` ส่งแค่ชื่อ/สถานะ
- ตัดสินแล้ว 30 ก.ย.: Codex แบบ full-access + สั่งจากแชทได้ · คลังเอกสาร B781001/OCPP · Whisper small · รูปด้วย Runway · Outlook อ่าน+ร่าง · สรุปเช้า 07:00 · tunnel/LINE · git repo · ภาพหน้าจอ → ดูหัวข้อ "งานชุดที่ 3"

### รอ eGPU (บน CPU slot เดียวไม่คุ้ม — ทุกข้อคือรอบโมเดลเต็มครั้งละหลายนาที)

subagents หลายตัวพร้อมกัน (ต้อง `OLLAMA_NUM_PARALLEL` ≥2) · git worktree แยกงานขนาน + best-of-N · Guardian ให้โมเดลรีวิวคำสั่งเสี่ยงก่อนรัน (ตอบได้แค่ อนุญาต/ปฏิเสธ ไม่ถามยืนยัน) · Goals งานยาวมีงบ · ควบคุมเดสก์ท็อป Windows (ต้องตัดสินใจอีกครั้ง) · สแกนความปลอดภัยด้วย Codex · คำแนะนำอัตโนมัติรายคืน · Python session ต่อเนื่อง · คุยเสียงแบบ realtime

### ข้อควรระวังที่เจอ

- เปลี่ยน AGENTS.md / รายการ tools / สกิลของ OpenClaw ทุกครั้ง ข้อความ Telegram แรกหลังจากนั้นต้องอ่าน prompt ใหม่ทั้งก้อน (หลายนาทีถึง ~20 นาทีบน CPU) — ทำรวดเดียวแล้วรอรอบเดียว · งานค้นคว้า/Codex ก็ทำให้ prompt cache หายเช่นกัน
- ผลค้นเว็บภาษาไทยจาก DuckDuckGo มีเว็บภาพสต็อก หน้าเครื่องมือค้นหา และเว็บผู้ใหญ่/พนันปน — งานค้นคว้ากรองออกก่อนอ่าน แต่ยังไม่เคยรันจริงทั้งรอบ ให้ดู `search.json` ของงานแรกก่อนเชื่อ
- `/status` จะเตือนเรื่อง P620 / ไดรเวอร์ จนกว่าจะทำทาง B ตามหัวข้อ eGPU เสร็จ (ตั้งใจ)
- สคริปต์ .ps1 ที่มีภาษาไทย (`dictate.ps1`, `Invoke-QwenCodex.ps1`, `Start-QwenCodex.ps1`) ต้องเป็น UTF-8 **มี BOM** ไม่งั้น Windows PowerShell 5.1 อ่านเพี้ยน
- ตัวติดตั้ง: `Check-PayloadParity.py` ไม่ตรวจโฟลเดอร์ `openclaw-tools` (สกิล) และไม่ฟ้องไฟล์ใหม่ที่ยังไม่ได้คัดลอกเข้า payload — ต้องคัดลอกตามรายการเอง · `Configure-OpenClaw.py` เขียน AGENTS.md ชุดใหม่นี้ให้แล้ว
- คีย์ลัด Ctrl+Alt+Q / D ไม่ตอบสนองหลังติดตั้ง → sign out แล้ว sign in ใหม่


## งานชุดที่ 3 — 30 ก.ย. 2026 (ค่ำ)

| ความสามารถ | ใช้ที่ไหน | วิธีใช้ |
|---|---|---|
| **สั่งงาน Codex จากแชท (full-access)** | WebUI + Telegram | "ให้ Codex แก้สคริปต์ ... ในโปรเจกต์ X" → `start_codex_job` ได้ job_id ทันที งานรันเบื้องหลังเมื่อเครื่องว่าง (มักรอให้แชทเงียบ 15 นาที) เสร็จแล้วแจ้ง Telegram · `get_codex_job` ดูผล · คำสั่งของโมเดลรันด้วยสิทธิ์ผู้ใช้เต็ม แต่ผ่าน `policy_hook.py` ที่ใช้กฎเดียวกับ `run_command` (บล็อก ไม่ถาม) · ห้ามรันใน `E:\localllm`, โฟลเดอร์โปรไฟล์, AppData, root ของไดรฟ์ |
| **ค้นคลังเอกสาร B781001 / OCPP** | WebUI + Telegram | "ในแบบ B781001 K5 K6 feedback ต่อไปที่ไหน", "HeartbeatInterval ตั้งไว้เท่าไร" → `search_knowledge` ตอบพร้อมอ้างอิงไฟล์ · หน้า/ชีต/แถว (ค้นด้วยคำ ไม่ใช้โมเดล ~50 ms) · อัปเดตดัชนี: "รัน action reindex ของโปรเจกต์ B781001" · ดัชนีอยู่ `data\knowledge\` ไฟล์ต้นฉบับอ่านอย่างเดียว |
| **Outlook อ่าน + ร่างเมล** | WebUI + Telegram | "วันนี้มีประชุมอะไร", "หาเมลเรื่อง...", "ร่างเมลถึง ... ว่า ..." → บันทึกใน Drafts เท่านั้น ส่งเองใน Outlook · **ต้องแก้โปรไฟล์ Outlook ก่อน** (ดูด้านล่าง) |
| **สรุปเช้า 07:00** | Telegram | ทุกวัน ไม่ใช้โมเดล: วันที่ · นัดวันนี้ · เมลยังไม่อ่าน · เตือนวันนี้ · สถานะระบบ · automation ชื่อ `qwen-morning-brief` · ดูตัวอย่าง: `webui-venv\Scripts\python.exe local-tools\morning_brief.py` |
| **ภาพหน้าจอ** | WebUI + Telegram | "ส่งภาพหน้าจอมาดู", "จับเฉพาะจอ 2", "ขอครึ่งซ้ายของจอ" → PNG ใน outputs ของโปรเจกต์ บน Telegram ส่งกลับในแชท (ภาพอาจมีข้อมูลงาน และขึ้นคลาวด์ของ Telegram) |
| **สร้างรูปด้วย Qwen-Image/Runway** | WebUI · Telegram | `generate_image provider=auto` ใช้ Qwen-Image 2.1 ในเครื่องก่อนเมื่อลง optional pack; ถ้ายังไม่ลงจึงใช้ Runway ตามค่าเดิม (ต้องมี API key/เครดิต) · บังคับได้ด้วย `provider=local` หรือ `provider=runway` |
| **ถอดเสียงแม่นขึ้น (Whisper small)** | เสียง Telegram · Ctrl+Alt+D · WebUI | `model-cache\faster-whisper-small` (486 MB) ผิดน้อยกว่า base (ทดสอบเสียงไทย 25% vs 38%) ช้ากว่า ~3 เท่า · คลิปยาวหรือเครื่องไม่ว่างใช้ base อัตโนมัติ (`QWEN_WHISPER_BUDGET_SECONDS`, `QWEN_WHISPER_MODEL=small|base`) |
| **tunnel สำหรับ LINE** | อินเทอร์เน็ต | ตัวเฝ้าแก้แล้ว (เดิมค้าง 27 ชม. และไม่เริ่มถ้าเปิดเครื่องตอนไม่มีเน็ต) · URL เปลี่ยนทุกครั้งที่รีสตาร์ต → แจ้ง Telegram อัตโนมัติ · ขั้นตอนตั้ง LINE อยู่ใน `internet-gateway\README.md` |
| **git ของชุดนี้** | เครื่องนี้เท่านั้น | `E:\localllm\.git` (ไม่มี remote) · ดู `GIT-README.txt`: `git status`, `git diff`, `git log`, คืนไฟล์ `git restore <ไฟล์>` |

ต้องทำเอง:
- **Outlook**: โปรไฟล์เริ่มต้นชี้ไป .pst ใน OneDrive จุฬาฯ ที่หายไป → Control Panel › Mail › Show Profiles › เลือก "Always use" โปรไฟล์ที่ใช้ได้ (หรือเอา .pst ที่หายออก) แล้วล็อกอินให้ซิงก์และเปิด Outlook ค้างไว้ · ระหว่างนี้เครื่องมือไม่เปิด Outlook เอง (กันค้าง) และสรุปเช้าจะบอกว่าอ่าน Outlook ไม่ได้
- **Runway**: ลงทะเบียน OAuth 2.1 ใหม่ตามแถวด้านบน (เดิมใช้ไม่ได้ตั้งแต่ 19 ก.ย. เพราะสคริปต์ตั้งชนิดล็อกอินผิด แก้สคริปต์แล้ว)
- **LINE**: สร้าง channel และใส่ Channel secret / access token เองด้วย `internet-gateway\configure-line.ps1` (คลิกขวา › Run with PowerShell)
