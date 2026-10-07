# Qwen Codex – ให้ Codex CLI ทำงานกับโมเดลในเครื่อง

สคริปต์ในโฟลเดอร์นี้ใช้ Codex CLI ที่มากับแอป Codex (`%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe`)
แต่ให้คุยกับ Ollama ในเครื่อง (qwen3-coder:30b) แทน OpenAI
ใช้สั่งงานเขียนโค้ด วิเคราะห์โฟลเดอร์ หรือรีวิวโค้ดใน git repo แบบทำงานเบื้องหลัง แล้วแจ้งเตือนเมื่อเสร็จ

เริ่มงานได้จากเดสก์ท็อป (Start Menu > Qwen Suite หรือบรรทัดคำสั่ง) และ **จากแชต** (Telegram ผ่าน OpenClaw, Open WebUI)
ด้วย `start_codex_job` ของ gateway (`codex_dispatch.py` เพิ่ม 2026-09-30) ไม่มีคำถามยืนยัน: gateway ตอบรหัสงานทันที
งานรอคิว/รอ Ollama ว่างเหมือนงานจากเดสก์ท็อป เสร็จแล้วแจ้ง Telegram + toast (สั่งจาก Telegram) หรือ toast (สั่งจาก WebUI)
ดูสถานะด้วย `get_codex_job <รหัส>` หรือ `list_codex_jobs` งานจากแชตสั่งในโฟลเดอร์ของ Qwen Suite (`E:\localllm`),
โปรไฟล์ผู้ใช้ทั้งโฟลเดอร์, AppData หรือโฟลเดอร์ข้อมูลลับไม่ได้

## ใช้งาน

| วิธี | ทำอะไร |
|---|---|
| **Qwen Codex – สั่งงานเบื้องหลัง** (shortcut) | หน้าต่างพิมพ์งาน เลือกโฟลเดอร์ ประเภทงาน และโมเดล แล้วทำงานเบื้องหลัง |
| **Qwen Codex – โหมดโต้ตอบ (ช้า)** (shortcut) | เปิดหน้าจอ Codex (TUI) ในหน้าต่างใหม่ ถามก่อนรันคำสั่งที่ไม่ใช่การอ่าน |
| `Invoke-QwenCodex.ps1 -Exec -Cwd <โฟลเดอร์> -Prompt "..."` | งานเดียว รอจนเสร็จ (ใส่ `-Detach` เพื่อทำเบื้องหลัง) |
| `Invoke-QwenCodex.ps1 -Review -Cwd <git repo>` | รีวิวการแก้ไขที่ยังไม่ commit (`-Base <branch>` / `-Commit <sha>` ได้) |
| `qwen-exec.cmd ...` | ตัวเดียวกันสำหรับไฟล์ .bat และ Task Scheduler (คืน exit code) |
| `Invoke-QwenCodex.ps1 -List` / `-Check` | ดูงานล่าสุด / ตรวจการตั้งค่าโดยไม่เรียกโมเดล |

ตัวอย่าง

```powershell
$qc = 'E:\localllm\local-tools\codex\Invoke-QwenCodex.ps1'
# วิเคราะห์โฟลเดอร์ (อ่านอย่างเดียว - ค่าเริ่มต้น)
& $qc -Exec -Cwd E:\qwen-projects\demo\work -Prompt 'อธิบายโครงสร้างโค้ดในโฟลเดอร์นี้ และจุดที่ควรแก้'
# สรุปเป็น JSON ตาม schema (บริดจ์ตรวจคำตอบกับ schema เอง ไม่ตรง = งานล้มเหลว exit 6)
& $qc -Exec -Cwd E:\qwen-projects\demo\work -PromptFile E:\jobs\nightly.md -OutputSchema E:\jobs\summary.schema.json -JsonOut
# ถามต่อจากงานล่าสุดในโฟลเดอร์เดียวกัน
& $qc -Exec -Cwd E:\qwen-projects\demo\work -ResumeLast -Prompt 'ขยายความข้อ 2'
# รีวิว + ทำไฟล์ Word
& $qc -Review -Cwd E:\qwen-projects\demo\repo -Docx
```

งานกลางคืนด้วย Task Scheduler (ตัวอย่าง 02:00 ทุกวัน สรุปไฟล์ใหม่เป็น JSON `{summary, files[]}`):

```
schtasks /Create /TN "Qwen Codex nightly" /SC DAILY /ST 02:00 /TR "E:\localllm\local-tools\codex\qwen-exec.cmd -Cwd E:\qwen-projects\demo\inbox -PromptFile E:\jobs\nightly.md -OutputSchema E:\jobs\summary.schema.json"
```

(`schtasks` เป็นการตั้งค่าถาวร เจ้าของเครื่องเป็นคนสร้างเอง สคริปต์นี้ไม่สร้างให้)

ประเภทงาน `-Exec` เขียนคำสั่งยาวหรือภาษาไทยไว้ในไฟล์ UTF-8 แล้วใช้ `-PromptFile`

## โหมดงานและความปลอดภัย

- **อ่านอย่างเดียว (ค่าเริ่มต้น)** – `-s read-only` เมื่อไม่ได้ตั้ง Windows sandbox Codex 0.146 จะรันเฉพาะคำสั่งอ่านที่มัน
  รู้ว่าปลอดภัย (Get-ChildItem, Get-Content, Select-String, git status/diff/log) คำสั่งอื่นทั้งหมด (เขียนไฟล์, python,
  ติดตั้ง ฯลฯ) ถูกปฏิเสธเป็น "blocked by policy" – ทดสอบแล้ววันที่ 2026-09-30 ด้วยโมเดลจำลอง
- **แก้ไขไฟล์ (`-Write`)** – ปิดไว้จนกว่าเจ้าของเครื่องจะเลือก `write_mode` ใน `E:\localllm\data\codex-bridge.json`
  - `windows-sandbox`: ใช้ sandbox แบบ restricted token ของ Codex (`[windows] sandbox = "unelevated"`)
    ครั้งแรกที่ใช้ Codex จะ **เปลี่ยน ACL**: เพิ่มสิทธิ์ให้ capability SID ในโฟลเดอร์งาน และใส่ deny ACE ให้ทุกโฟลเดอร์
    ที่ Everyone เขียนได้ใต้ TEMP/TMP/USERPROFILE/PUBLIC/PATH (เก็บ SID ไว้ที่ `data\codex-home\cap_sid`)
    กันการเขียนนอกโฟลเดอร์งานได้ แต่ **กันการอ่านไม่ได้** และเครือข่ายถูกปิด
  - `full-access` (เลือกแล้ว 2026-09-30): `-s danger-full-access` + approval `never` คำสั่งของโมเดลรันด้วยสิทธิ์ผู้ใช้เต็ม
    เหมือน run_command ของ gateway ไม่เปลี่ยน ACL เขียนได้ทุกที่ที่ผู้ใช้เขียนได้ ป้องกันด้วย hook นโยบายคำสั่ง + กฎคำสั่ง
    + proxy ปิด (ด้านล่าง) เท่านั้น
  - แบบ `elevated` (สร้างผู้ใช้ CodexSandbox ต้องใช้สิทธิ์ admin) ไม่รองรับในบริดจ์นี้
- **hook นโยบายคำสั่ง** (`policy_hook.py`, PreToolUse ของ Codex) – ก่อน *ทุก* คำสั่ง Codex ส่งคำสั่งให้ hook ตรวจด้วย
  `command_policy.py` ชุดเดียวกับ run_command ของแชต (ที่เก็บรหัสผ่าน, ลบแบบซ้อน, หยุดบริการ AI, โฟลเดอร์ .codex/.openclaw/
  .claude/codex-home, กฎที่เจ้าของแก้ใน `data\permission-center\command-rules.json`) บวกกฎของบริดจ์ (ไฟล์ความปลอดภัยของ
  suite, .ssh/.aws) ถูกห้าม = โมเดลเห็นเหตุผลภาษาไทย และบันทึกใน `<run_dir>\policy-denials.jsonl`
  Codex 0.146 **ปล่อยคำสั่งผ่าน** ถ้า hook พัง/หมดเวลา/ไม่ได้ trust (ทดสอบแล้ว) บริดจ์จึงรัน `policy_hook.py --selftest`
  และถาม `codex app-server` (hooks/list) ว่า hook trusted ก่อนทุกงาน งานแก้ไขไฟล์จะไม่เริ่มถ้าไม่ผ่าน (exit 2)
  hash ที่ trust เก็บใน `data\codex-home\policy-hook.trust.json` (บริดจ์ trust เฉพาะคำสั่งที่แม่แบบของตัวเองสร้าง)
- **config ของ repo ไม่ถูกโหลด** – โหมด full-access ทำให้ Codex เชื่อถือโฟลเดอร์งานทันทีและโหลด `.codex\config.toml`
  ของ repo ซึ่งปิด hooks ได้ (ทดสอบแล้ว) บริดจ์จึงเขียน `[projects.'<โฟลเดอร์งาน>'] trust_level = "untrusted"` ลง config
  ทุกงาน (โฟลเดอร์งาน, git root, -AddDir) และส่ง `--enable hooks` ทุกครั้ง hook ของ repo ไม่รันเพราะไม่มี trusted_hash
- **กฎคำสั่ง** (`qwen-bridge.rules`) ห้ามเด็ดขาดพร้อมเหตุผลภาษาไทยที่โมเดลเห็น: ที่เก็บรหัสผ่าน (cmdkey, vaultcmd ...),
  ปิดเครื่อง/ดิสก์, ACL/ผู้ใช้/บริการ/registry/scheduled task, ollama/openclaw/หยุดโปรเซส, ดาวน์โหลด/ติดตั้งแพ็กเกจ,
  git push/pull/clone/clean/reset --hard, ลบทั้งโฟลเดอร์, shell ซ้อน (powershell/cmd/Start-Process/iex)
  Codex ตรวจทุกคำสั่งใน PowerShell ที่โมเดลเขียน แต่มองไม่เห็นคำสั่งใน `& x` หรือ `{ }` จึงเป็นด่านชะลอ ไม่ใช่ sandbox
- **การอ่านไฟล์กันได้แค่ตามชื่อ** – hook ห้ามคำสั่งที่ *เขียนชื่อ* โฟลเดอร์ลับ (.codex .openclaw .claude codex-home .ssh .aws
  Login Data ...) แต่ path ที่ประกอบจากตัวแปรหรือสคริปต์ยังอ่านได้ทุกไฟล์ที่ผู้ใช้อ่านได้
  ถ้าในโฟลเดอร์งานมีข้อความสั่งโมเดล (README, AGENTS.md, คอมเมนต์ในโค้ด) โมเดลอาจทำตามแล้วใส่เนื้อหาไว้ในคำตอบ
  **อย่าสั่งงานกับ repo หรือไฟล์ที่ไม่ไว้ใจ** ด่านที่มีคือ `list_codex_jobs` ไม่แสดงคำตอบของงานที่คำสั่งหรือคำตอบอ้างถึง
  โฟลเดอร์ข้อมูลลับ (.codex .openclaw .ssh .aws .claude credentials Login Data ฯลฯ) และปิดข้อความที่ดูเหมือนรหัส/โทเค็น/เลขไอดี
  ก่อนส่งเข้าแชต (เป็นแบบพยายามดีที่สุด คำตอบเต็มยังอยู่ใน `last.md`/`result.json` ของงานบนเครื่อง)
- **เครือข่าย** – codex.exe ได้ proxy ปิด (127.0.0.1:9) สำหรับทุกที่ยกเว้น 127.0.0.1 (ตรวจแล้ว: เชื่อมต่อแค่ 127.0.0.1)
  คำสั่งของโมเดลได้ proxy ปิดเช่นกัน (ได้ผลกับ pip/git/curl/python requests) ไม่มี `--oss` (ทางนั้นดึงโมเดลจากเน็ตได้)
- **แยกจาก Codex ของผู้ใช้** – ใช้ `CODEX_HOME = E:\localllm\data\codex-home` เสมอ ไม่แตะ `C:\Users\EGAT\.codex`
  ไม่ใช้ Credential Manager (`cli_auth_credentials_store = "file"`) ปิดฟีเจอร์คลาวด์ ปลั๊กอิน เบราว์เซอร์ และ memories
  (hooks เปิดเพื่อ hook นโยบายคำสั่งของบริดจ์เท่านั้น)
- **ไฟล์ที่เปลี่ยน** – ไม่มี apply_patch จึงไม่มี event file_change งานแก้ไขไฟล์เก็บรายการไฟล์ (ขนาด+เวลา) ก่อน/หลังรัน
  แล้วบันทึก `changed_files` (เพิ่ม/แก้/ลบ สูงสุด 200 รายการ ข้าม .git node_modules .venv .codex-runs) ใน result.json
- **ไม่แย่งแชต** – งานเดียวต่อครั้ง (ล็อก `data\codex-home\run.lock`) และเริ่มเมื่อ Ollama ว่าง: ไม่มี slot ของ llama-server
  กำลังประมวลผล และไม่มีการใช้โมเดลอื่น (เช่น qwen3.8) ในช่วง `recent_activity_minutes` นาทีล่าสุด รอสูงสุด
  `idle_wait_minutes` (60) ตรวจทุก 60 วินาที เสร็จแล้วปลดโมเดลของงานออกจาก RAM (เฉพาะถ้ายังโหลดอยู่ และเฉพาะงานที่ได้
  เริ่ม Codex จริง – งานที่รอล็อกหรือรอ Ollama จนหมดเวลา (exit 4/3) หรือโหมดโต้ตอบที่ถูกปฏิเสธ จะไม่ปลด qwen3-coder
  ที่งานอื่นหรือแชตเขียนโค้ดกำลังใช้)
- ไม่มีการเพิ่มคำถามยืนยันในช่องแชต (กฎของเจ้าของเครื่อง) ทุกด่านเป็นการห้ามพร้อมเหตุผลภาษาไทย
- **สิ่งที่ยังกันไม่ได้ในโหมด full-access**: สคริปต์ที่มีอยู่แล้วหรือที่โมเดลเขียนขึ้น (`python x.py`) ทำอะไรก็ได้ตามสิทธิ์ผู้ใช้
  โดย hook มองไม่เห็นเนื้อในสคริปต์; path ที่ประกอบจากตัวแปรหลบการตรวจชื่อได้; โปรแกรมที่ไม่สน proxy ออกเน็ตได้;
  เขียนไฟล์นอกโฟลเดอร์งานได้ - ระดับเดียวกับ run_command จากแชต

## ไฟล์และโฟลเดอร์

| ที่อยู่ | คืออะไร |
|---|---|
| `local-tools\codex\config.template.toml`, `qwen-models.template.json`, `qwen-bridge.rules.template` | แม่แบบ – เขียนทับ config ใน CODEX_HOME ทุกครั้งก่อนรัน (แก้ที่แม่แบบ) |
| `data\codex-home\` | CODEX_HOME ของบริดจ์ (session, sqlite ของ Codex, rules) ถ้ามีคนแก้ config.toml ตรงนี้จะเก็บสำเนา `.bak` แล้วเขียนใหม่ |
| `data\codex-bridge.json` | ค่าตั้ง: write_mode, default_model, review_model, idle_wait_minutes, recent_activity_minutes, max_minutes, max_review_diff_lines, unload_after, notify |
| `local-tools\codex\policy_hook.py` | hook นโยบายคำสั่ง (ทดสอบเอง: `python policy_hook.py --selftest`) |
| `local-tools\codex\codex_dispatch.py` | ตัวเริ่มงานจากแชต (start_codex_job / get_codex_job ของ gateway) |
| `logs\codex-bridge\dispatch\<id>.json`, `<id>.launch.log` | คำขอจากแชตและผลการเริ่มงาน (ถ้าเริ่มไม่ได้ ข้อความ error อยู่ที่นี่) |
| `logs\codex-bridge\bridge.log`, `jobs.jsonl` | บันทึกทุกงาน (jobs.jsonl คือที่ `list_codex_jobs` อ่าน) |
| `E:\qwen-projects\<slug>\.codex-runs\<id>\` | ไฟล์ของงานในโปรเจกต์: prompt.md, events.jsonl, last.md, result.json (ถ้าโฟลเดอร์นี้อยู่ใน git repo ของงาน จะย้ายไป `logs\codex-bridge\runs\<id>`) |
| `<project>\outputs\review-<วันเวลา>.md` | ผลรีวิว มีป้าย "ความเห็นจากโมเดลในเครื่อง ต้องตรวจซ้ำ" |

Exit code: 0 สำเร็จ, 1 งานล้มเหลว, 2 ตั้งค่า/อาร์กิวเมนต์ผิด, 3 Ollama ไม่ว่างนานเกิน, 4 มีงาน Codex อื่นอยู่,
5 ยังไม่เปิดโหมดแก้ไขไฟล์, 6 คำตอบไม่ตรง schema, 7 เกินเวลา `max_minutes`, 8 diff ใหญ่เกิน (>2000 บรรทัด)

## ข้อจำกัดที่ควรรู้

- บน CPU: โหลด qwen3-coder 1–2 นาที อ่าน prompt แรก 1–2 นาที แต่ละขั้น 0.5–2 นาที งาน 10 ขั้นใช้ 10–25 นาที
  และดัน qwen3.8 ออกจาก RAM – แชตครั้งถัดไปจะช้าเพราะต้องโหลดและอ่าน prompt ใหม่
- คุณภาพการเรียกเครื่องมือของ qwen3-coder ภายใต้ Codex ยังไม่ได้พิสูจน์กับงานจริง
- ไม่มีเครื่องมือ apply_patch (`apply_patch_tool_type = null` – ค่า "function" Codex 0.146 ไม่รับ) โมเดลจะแก้ไฟล์ด้วย
  Set-Content ทั้งไฟล์
- Codex ส่ง `--output-schema` ให้ Ollama เท่านั้น ไม่ตรวจคำตอบเอง บริดจ์ตรวจด้วย jsonschema แทน
- Codex อัปเดตพร้อมแอป (0.146 → 0.158) บริดจ์เลือก codex.exe รุ่นใหม่สุดเอง และใช้ `--strict-config`:
  ถ้ารุ่นใหม่ไม่รู้จักค่าใดในแม่แบบ งานจะล้มพร้อมข้อความ แทนที่จะรันโดยข้ามค่าความปลอดภัยไปเงียบ ๆ
  ใช้ `-Check` หลังอัปเดตแอป กำหนดตัวที่ใช้เองได้ด้วยตัวแปร `QWEN_CODEX_EXE`
- โหมดโต้ตอบ: ถ้า Codex เสนอให้ติดตั้ง sandbox ของ Windows ให้เลือกไม่ติดตั้ง/ออก จนกว่าจะตัดสินใจเรื่อง write_mode
  ค่าที่ TUI บันทึกลง config (เช่น trust หรือ rules "always allow") จะถูกรีเซ็ตก่อนงานถัดไป

## ตรวจปัญหา

```powershell
& E:\localllm\local-tools\codex\Invoke-QwenCodex.ps1 -Check   # ไม่เรียกโมเดล: ฟีเจอร์ รายชื่อโมเดล ขนาด prompt, --strict-config กับพอร์ตปิด, Ollama, ล็อก
& E:\localllm\local-tools\codex\Invoke-QwenCodex.ps1 -List
Get-Content E:\localllm\logs\codex-bridge\bridge.log -Tail 30
```
