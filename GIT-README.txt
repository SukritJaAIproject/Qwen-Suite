Qwen Suite - Git สำหรับเก็บประวัติซอร์สโค้ด
================================================================

โฟลเดอร์ E:\localllm เป็น git repository ที่มีสำเนาบน GitHub แบบ Private
- remote ชื่อ origin = https://github.com/SukritJaAIproject/Qwen-Suite (Private, ตั้งเมื่อ 7 ต.ค. 2026)
- ใช้ดูว่าไฟล์ของ Qwen Suite ถูกแก้อะไรไปบ้าง และย้อนไฟล์กลับเมื่อแก้แล้วพัง
- snapshot แรก: "Qwen Suite snapshot 2026-09-30 (1.4.0)"
- ห้ามเปลี่ยน repo บน GitHub เป็น Public: ประวัติ commit เก่ามีข้อมูลส่วนตัวและงาน
  (README ภาษาไทย, ชื่อโปรเจ็กต์งาน, พาธ OneDrive) ถ้าจะเปิดสาธารณะให้ทำ snapshot ใหม่ที่ล้างข้อมูลแล้ว
- หน้าแรกบน GitHub แสดง .github\README.md (ภาษาอังกฤษ) ส่วน README.md ภาษาไทยยังเป็นคู่มือหลักในเครื่อง


สิ่งที่เก็บใน git (tracked)
---------------------------
- สคริปต์ที่ root: *.ps1, Configure-*.py, README.md, GIT-README.txt, .gitignore
- ai-services\
- internet-gateway\   (ยกเว้น data\ ซึ่งมี API key / ข้อมูล LINE)
- local-tools\        (ยกเว้น allowed-roots.json)
- openclaw-tools\
- theme\
ดูรายชื่อไฟล์ทั้งหมดที่เก็บ:   git ls-files


สิ่งที่ไม่เก็บ (ดูรายละเอียดใน .gitignore)
-----------------------------------------
- ข้อมูลและความลับ: data\, webui-data\, internet-gateway\data\, .webui_secret_key,
  allowed-roots.json, *.db*, *.sqlite*
- log / สำรอง / cache: logs\, backups\, hf-cache\, pip-cache\, model-cache\,
  __pycache__\, *.pyc, *.log
- โปรแกรมภายนอกและไฟล์ไบนารี: webui-venv\, openclaw-runtime\, runtime\,
  openclaw-install\, ocr\, test-artifacts\, disabled-startup\, node_modules\,
  *.exe, *.zip, *.7z, *.lnk, *.ico
- openclaw-workspace\ ไม่อยู่ใน repo นี้ เพราะมี git repo ของตัวเองอยู่แล้ว
  (E:\localllm\openclaw-workspace\.git) และมี memory\ กับ outputs\ ที่เป็นข้อมูลส่วนตัว


วิธีดูการเปลี่ยนแปลง (เปิด PowerShell แล้วพิมพ์  cd E:\localllm  ก่อน)
-------------------------------------------------------------------
  ไฟล์ไหนถูกแก้บ้าง:              git status
  แก้อะไรไปบ้าง:                   git diff
  ดูเฉพาะไฟล์เดียว:                git diff -- internet-gateway\start-quick-tunnel.ps1
  ประวัติ commit:                  git log --oneline
  ประวัติของไฟล์เดียว:             git log --oneline -- local-tools\server.py


วิธีย้อนไฟล์เดียวกลับ
--------------------
  กลับไปเหมือน commit ล่าสุด:
      git restore internet-gateway\start-quick-tunnel.ps1
  กลับไปเหมือน commit ที่ระบุ (ดูรหัส commit จาก git log --oneline):
      git restore --source <รหัส-commit> internet-gateway\start-quick-tunnel.ps1

  ข้อควรระวัง: git restore เขียนทับไฟล์ในเครื่องทันที สิ่งที่แก้ในไฟล์นั้นแต่ยังไม่ได้ commit จะหายไป
  ถ้าย้อนสคริปต์ของบริการที่กำลังทำงานอยู่ ต้องรีสตาร์ตบริการนั้นก่อนจึงจะมีผล


บันทึก snapshot ใหม่หลังแก้ไฟล์
------------------------------
  git status                      (ตรวจก่อนว่าไม่มีไฟล์ความลับหรือไฟล์ข้อมูลหลุดเข้ามา)
  git add -A
  git commit -m "อธิบายสั้น ๆ ว่าแก้อะไร"
  git push                        (ส่งขึ้น GitHub; ครั้งแรกจะเปิดหน้า sign-in ของ GitHub ในเบราว์เซอร์)


หมายเหตุ
--------
- repo นี้ตั้ง core.autocrlf=false ไว้ การ restore จึงได้ไฟล์ตรงตามที่ commit ไว้ทุกไบต์
- ถ้าลบโฟลเดอร์ E:\localllm\.git ประวัติทั้งหมดจะหายไป แต่ไฟล์ของ Qwen Suite ไม่ได้รับผลกระทบ
