# Changelog

All notable changes to Qwen Suite, newest first. Dates are Bangkok time (UTC+7). Versions follow the installer releases. The owner's day-by-day notes, with more machine-specific detail, are in the Thai operations manual [README.md](README.md); the project overview is the [GitHub front page](.github/README.md).

## [1.6.2] - 2026-11-09 (rebuild r5)

### Added
- **Forgot password on the Open WebUI sign-in page.** A "ลืมรหัสผ่าน?" (forgot password) button under the sign-in form opens a panel served by the local tools: `local-tools/account_recovery.py` and `local-tools/account.html`, with `/account/forgot`, `/account/reset` and `POST /account/api/forgot|token|reset` on `:8765` (local JSON requests only).
  - The owner's account gets a single-use link, valid for 15 minutes, e-mailed from the owner's Gmail through SMTP with an App Password (STARTTLS on port 587, then 465). The link points at `127.0.0.1`, so it opens only on this PC. The token is in the URL fragment, and only its SHA-256 hash is stored.
  - The new password (at least 8 characters, at most 72 bytes) is written as a bcrypt hash into Open WebUI's `auth` table for that one account, the way Open WebUI stores passwords itself.
  - Only admin accounts with a recovery address are e-mailed; any other address gets the same neutral answer, and the owner resets other users in Admin Panel › Users. Limits: 3 links per account and 10 in total per hour, 60 seconds apart; 20 wrong links in 10 minutes pause the page. Events go to `data\account-recovery.log`, never passwords or tokens.
  - One-time setup with `local-tools\Setup-RecoveryEmail.cmd`: the Gmail address, the App Password (shown as `*`; Ctrl+V pastes) and the address that receives the links. It sends a test e-mail before saving anything. The App Password is stored with Windows DPAPI for the current Windows user in `data\secrets\recovery-smtp.bin`, and the addresses in `data\account-recovery.json`. The setup needs no Open WebUI sign-in, so it also helps an owner who is already locked out.
- **Sign up with the owner's approval.** A "สมัครบัญชีใหม่" (sign up) button opens Open WebUI's own sign-up form. Sign-up is on with new accounts as `pending` (`ENABLE_SIGNUP=True` and `DEFAULT_USER_ROLE=pending` in `start-webui.ps1`; `ui.enable_signup` and `ui.default_user_role` in `Configure-Qwen-WebUI.py`), so a new account can do nothing until the owner approves it in Admin Panel › Users.
  - The owner is notified once per new account: a Windows toast and, when set up, a Telegram message (`notify_owner` in `local-tools/notifier.py`). The check runs every minute; `QWEN_ACCOUNT_WATCH=0` turns it off.

### Changed
- Approved users can chat with the suite's model presets (`share_presets_with_users` in `Configure-Qwen-WebUI.py` gives all users read access) but cannot use the PC's tools: the MCP tool servers stay owner-only, and `theme/qwen-account.js` hides the Permission Center, Video Studio and Projects & Tasks buttons for non-admin accounts and on the sign-in page. The `:8765` pages still have no sign-in of their own.
- Tested before going live: 64 unit checks with the real bcrypt and DPAPI on this PC, 35 browser checks, and the database change applied to a copy and reverted exactly. The owner then reset a forgotten password through the e-mailed link.
- Online and Offline 1.6.2 rebuilt (r5) with these files (`Qwen-Setup-Offline.exe` + 35 parts, 63.77 GiB; same file names as r4, new SHA-256 values). The install guide covers the recovery e-mail setup and sign-up approval. The installers contain no recovery settings or App Password: each PC sets up its own.

## [1.6.2] - 2026-11-09 (rebuild r4)

### Added
- **Channel watchdog** (`local-tools/channel_watchdog.py`, started by `local-tools/server.py`). When OpenClaw gives up on a channel after a network outage ("terminal disconnect"), the watchdog waits until the service's host name resolves again, checks with the gateway that the account is still linked and was stopped by OpenClaw, then calls OpenClaw's non-destructive `channels.start`, which keeps the pairing and credentials.
  - At most 3 restarts per channel per hour, at least 5 minutes apart.
  - Never restarts a logged-out or replaced session (WhatsApp 401, 403, 411, 440, 500), a channel that OpenClaw is still reconnecting, or a channel stopped by hand.
  - Writes times, channel names and outcomes only to `logs\channel-watchdog.log` and `data\channel-watchdog.json`; `QWEN_CHANNEL_WATCHDOG=0` turns it off.
  - Tested with fakes (13 cases) before going live.
  - Background: on 9 November the network dropped from about 07:32 to 07:58. OpenClaw retried WhatsApp 12 times (about 6 minutes), stopped it at 07:38 and never tried again, so it had to be restarted by hand at 08:06.

### Changed
- **Health dashboard:** a channel outage is counted once, at the time the channel stopped. OpenClaw's health monitor logs "skipping restart, terminal-disconnect" every 5 minutes, which had moved the reported time of the 9 November outage from 07:38 to 08:03. The Telegram/WhatsApp line now also says whether the watchdog reconnected the channel, will reconnect it when the internet is back, or needs a new QR login.
- **Offline installer:** installing over an Offline 1.6.1 install now updates the bundled Ollama runtime (0.32.15 to 0.40.15). Only Ollama processes started from the suite's `runtime\ollama` folder are stopped; the old folder is kept as `runtime\ollama.old-<version>-<time>` and put back if unpacking fails, and this step never fails the installation. Tested in a sandbox: a running old runtime is stopped, replaced and kept; a current runtime is left alone; a new install is unpacked; a broken archive restores the old runtime; the PC's own Ollama is not touched.
- Offline 1.6.2 rebuilt with these changes (`Qwen-Setup-Offline.exe` + 35 parts, 63.77 GiB; same file names as the morning build, new SHA-256 values). Online 1.6.2 rebuilt the same way, with the updated install guide.

## [1.6.2] - 2026-11-09

### Fixed
- **Project sync into Open WebUI works again.** It had failed since 1.6.1 turned the Open WebUI sign-in on, because `local-tools/project_store.py` still signed in the old way; local files and `MEMORY.md` were always written, only the sync failed.
  - The tools now sign in with the owner's own Open WebUI API key, read from `webui-data\webui.db` each time it is needed and never copied. A new key created in Settings › Account › API Keys is used automatically.
  - Open WebUI API keys are limited to `/api/v1/folders`, `/api/v1/memories` and `/api/v1/files` (Admin Settings › General). Such a key cannot read chats or users or create keys (tested: HTTP 403). Add endpoints on that page if an API key is needed for something else.
  - `Configure-Qwen-WebUI.py` sets this up (`ensure_project_sync_api_key`). An existing key is never replaced, and the endpoint list is only extended.
  - The old `admin@localhost` sign-in is now used only when Open WebUI runs without sign-in (`/api/config` reports `auth=false`), so it never counts as a failed owner sign-in (Open WebUI locks an account after 15 failures in 3 minutes).
  - `server.py` catches up on its own: when Open WebUI answers again, and every 10 minutes while something is pending. That covers folder settings that changed while sync was down, memories without an Open WebUI id (an identical existing memory is linked instead of duplicated) and attachments. Results are in `data\project-sync.json`.
  - New health-dashboard check "Project sync into Open WebUI".
  - Tested 19/19 against a copy of the database on a test Open WebUI (port 3001) before going live; on this PC, three projects' folder settings and one pending memory were re-sent and verified.
- **Health dashboard reads Telegram/WhatsApp and the OpenClaw automations even when the OpenClaw CLI is slow** (`/status`, `/qstatus`, `suite_status`, `Test-QwenSuite.ps1`). The CLI calls were cut at 10 seconds, but the CLI takes about 3 seconds when warm, 6 seconds or more right after starting and over 10 seconds next to other checks, so these checks sometimes showed "could not check".
  - The calls now run in the background for up to 45 seconds, one at a time per check.
  - A report waits only until its own deadline (13 seconds; 30 seconds in the console). Otherwise it shows the last good answer with the time it was read (up to 20 minutes old), and a late answer is kept for the next report.
  - `server.py` reads both 90 seconds after it starts (`prewarm_openclaw`).
  - Tested 14/14 and compared with the live system.

### Changed
- **Ollama 0.32.15 to 0.40.15**, with `OllamaSetup.exe` from the GitHub release (size and SHA-256 matched the release digest).
  - Tested first in a sandbox (port 11435, sharing the model folder with `OLLAMA_NOPRUNE=1`): every model answered; `qwen3-embedding:4b` embeddings matched 0.32.15 (cosine 0.9991–0.9993), so the RAG index did not need rebuilding; no model file was changed or removed. 0.40.15 adds `manifests-v2` and `metadata` folders, which 0.32.15 ignores, so a rollback stays possible.
  - Ollama is still started by `start-ollama-server.ps1` / `Restart-QwenOllama.ps1`. The tray app that the installer opened was closed at once, and the installer added no autostart entry (Startup, Run keys and Task Scheduler were checked). The tray app must still not be opened.
  - 0.40.15 still sees the Quadro P620 through CUDA v12 (CUDA v13 does not support compute capability 6.1) and does not use the Intel UHD through Vulkan unless `OLLAMA_IGPU_ENABLE=1` is set.
  - The previous program was backed up to the external drive, with a rollback script.
- **MiMo-V2.6 9B now uses the official `mimo-v2.6:9b`** (6.55 GB, MIT; completion, vision, tools and thinking; renderer `mimo-v2.6`) instead of the Hugging Face GGUF. On this PC it answered a Thai arithmetic question correctly (22.9 seconds including the load), made the tool call `get_weather({"city": "Bangkok"})` and read the test image ("Red"). The Open WebUI preset "MiMo-V2.6 9B" points to it.
- **Online installer 1.6.2 rebuilt** with the Project sync fix and the official MiMo. MiMo needs Ollama 0.40.15 or newer; on an older Ollama the installer skips it and explains how to update. The 8 November files were kept next to the new ones as `*.previous-<time>`.

### Added
- **Offline installer 1.6.2**, replacing Offline 1.6.1: the payload of Online 1.6.2 plus Qwen Suite Desktop and this PC's Ollama 0.40.15 runtime (`ollama.exe` and `lib`; size and CRC32 of every file checked against the installed copy). The install guide now covers Online and Offline 1.6.2.

### Removed
- The Hugging Face MiMo GGUF (`hf.co/bartowski/MiMo-V2.6-Distill-Qwen-9B-GGUF:Q4_K_M`, 6.76 GB) and its hidden Open WebUI preset, with the owner's approval. The model files used by the offline installer were checked to be untouched, and the Open WebUI database was backed up first.
- The stale `OllamaSetup.exe` in `%LOCALAPPDATA%\Ollama\updates_v2` (1.57 GB), with the owner's approval, after an identical copy (same SHA-256) was confirmed on the external drive.

## [1.6.2] - 2026-11-08

### Added
- **Three extra models**, picked directly in Open WebUI's model list. They are not routed by the Smart Model Router and the `qwen-*` profiles are unchanged. Their presets use the same system prompt, context 32,768, `keep_alive` 2h, `think=false` and Local Computer Fast tools as `qwen3.8:27b` Direct.
  - `qwen3.6:35b`: Qwen3.6 35B-A3B (mixture of experts, 3B active per token, so it is fast on a CPU).
  - `gemma4:26b-a4b-it-qat`: Gemma 4 26B-A4B (mixture of experts, 4B active, 4-bit QAT).
  - MiMo-V2.6 9B, at first as the Hugging Face GGUF with a vision projector, because the official `mimo-v2.6:9b` returned HTTP 412 "requires a newer version of Ollama" on Ollama 0.32.15 (replaced on 9 November).
  - Test results on this PC (CPU i7-10750H, `num_ctx` 32768, `think=false`, temperature 0; a Thai question, a `get_weather` tool call from a Thai question, and a red-circle image):

    | Model | Size | Capabilities (Ollama) | Thai answer | Speed | Tool call | Image | Memory when loaded |
    |---|---|---|---|---|---|---|---|
    | Qwen3.6 35B-A3B (`qwen3.6:35b`) | 22.62 GB | completion, vision, tools, thinking | correct | 6.96 tok/s (load 25.8 s) | `get_weather({"city": "Chiang Mai"})` | red | 21.03 GiB (0.89 GiB on the P620) |
    | Gemma 4 26B-A4B (`gemma4:26b-a4b-it-qat`) | 15.63 GB | completion, vision, tools, thinking | correct | 7.18 tok/s (load 16.9 s) | `get_weather({"city": "Chiang Mai"})` | Red | 14.97 GiB (1.19 GiB on the P620) |
    | MiMo-V2.6 9B (GGUF) | 6.76 GB | tools, thinking, completion, vision | correct | 6.33 tok/s (load 12.0 s) | `get_weather({"city": "เชียงใหม่"})` | Red | 7.02 GiB (1.55 GiB on the P620) |

    Memory is what `/api/ps` reported at `num_ctx` 32768; the part "on the P620" is what Ollama placed on the Quadro P620.
- **`generate_ai_video`** (`local-tools/video_gen.py`, MCP tool 72): 1–5 second AI clips, 1280×704 or 704×1280, 24 fps, no sound, made on the PC by the optional **Wan 2.2 pack** (Wan2.2 TI2V-5B on ComfyUI v0.43.0, Apache-2.0, commercial use allowed).
  - Needs an NVIDIA CUDA GPU (an eGPU works) with compute capability 7.5+ and 8 GB+ VRAM.
  - The pack is outside the main installer (`Wan-2.2-Optional-Pack-v1.0.zip`) and is enabled only by `optional\wan-2.2\install.json`. Without it the tool answers `status=not_installed` with install steps.
  - ComfyUI listens on `127.0.0.1:8190`, starts on first use and unloads the model from VRAM after every clip. Output goes to the project's `outputs`.
  - A clip takes several minutes: the tool returns `status=running` and a `task_id`; call it again with the same `task_id`. Permission category: media editing (`media_write`).
- **Qwen Suite Desktop** (`QwenSuiteDesktop.exe`, "Qwen Suite Desktop" icon on the desktop): one WebView2 window with buttons for chat, Permission Center, compute device, Video Studio and system status (Ctrl+1..5, F5).
  - Only `127.0.0.1:3000` and `:8765` open inside the app; other web links open in the default browser, and other schemes (`file:`, `ms-settings:` and so on) are blocked.
  - If Qwen Suite is not running, it offers a button to start Qwen All-in-One.
  - Separate WebView2 profile in `%LOCALAPPDATA%\QwenSuite\Desktop` (the first launch asks for the Open WebUI sign-in). URL-policy self-test: `QwenSuiteDesktop.exe --selftest out.txt`.
- **Online installer 1.6.2** (the 1.6.1 files were kept, not overwritten).

### Changed
- **Qwen-Image 2.1 weights (32.06 GiB) moved to the external drive** to free space on E:. SHA-256 of all 61 files was checked before the source was removed (the list is kept with the weights); the pack cannot run without a CUDA GPU anyway. `model-cache\Qwen-Image-2.1-MOVED-TO-G.txt` explains how to copy them back before running the pack installer, so nothing has to be downloaded again.
- Replaced files were backed up (`backups\pre-1.6.2-*`), and the changes were committed and pushed to the private GitHub repository once the work was done, as the owner approved.

## [1.6.1] - 2026-10-01 to 2026-10-07

### Added
- **Open WebUI requires a sign-in.** `start-webui.ps1` sets `WEBUI_AUTH='True'` and `ENABLE_SIGNUP='False'`. The default owner account `admin@localhost` (initial password in the install guide) is for the first sign-in only; change the password at once in Settings > Account.
- A **models check** on the health dashboard: every model tag named in the suite's code and config, compared with what Ollama has installed (7 October).
- The repository was prepared for a **private GitHub remote** with an allowlist `.gitignore`, the English front page `.github/README.md` and screenshots (7 October).

### Fixed
- The 1 October build stored the owner's e-mail as plain `admin`, which the web sign-in form rejects (`type="email"`); only the API accepted it. `Configure-Qwen-WebUI.py` now changes that e-mail to `admin@localhost` on every run without touching the password (6 October).
- `generate_image provider=auto` falls back from the local Qwen-Image pack to Runway (cloud, costs credits) only for requests with no `seed` and no local reference image; otherwise it reports the error and uploads nothing. `provider=local` never falls back (6 October).
- Video Studio: without a 24 GB card, Thai storyboards use the built-in template at once (measured: Granite got Thai narration grammar wrong about half the time). English storyboards use `granite4.2:8b` with a timeout of 30 seconds plus 40 seconds per scene (at most 600 seconds). If Ollama is busy with another model, the template is used too (6 October).

### Known issues (fixed in 1.6.2)
- `local-tools/project_store.py` still signed in to Open WebUI with the pre-1.6.1 credentials, so syncing project folders, memory and attachments into Open WebUI failed. Local files and `MEMORY.md` were still written.

## [1.6] - 2026-10-01

### Changed
- `qwen-agent` and `qwen-review` use `muse-glimmer:30b` for tools, agent workflows and Second Opinion.
- `qwen-fast` and `qwen-structured` use `granite4.2:8b` for short tasks, JSON/schema/extraction and RAG. Embeddings stay on `qwen3-embedding:4b`.
- If Muse or Granite is not installed, the router falls back to `qwen3.8:27b`, and the automatic Second Opinion stays off until `muse-glimmer:30b` is present.
- `generate_image` takes `provider=auto`: the local Qwen-Image 2.1 is used only when `optional\qwen-image-2.1\install.json` exists, which the optional pack writes after its CUDA check passes (weights alone do not count as installed); otherwise Runway as before.
- Qwen-Image 2.1 is not in the main installer. It needs the optional pack, acceptance of the Qwen Research License (non-commercial research and evaluation only; commercial use needs a separate licence), at least 50 GB of free space, an NVIDIA CUDA GPU and the CUDA build of PyTorch.
- Qwen Codex offers only `qwen3-coder:30b` (`muse-glimmer:30b` will be added after it is tested with Codex tool calls).

### Removed
- `gpt-oss:20b` and `qwen3:4b` were removed from the reference machine on 5 October 2026.

## [1.5.0] - 2026-09-30 (evening) to 2026-10-01

### Added
- **Codex jobs from chat (full access):** `start_codex_job` returns a job id at once; the job runs in the background when the PC is idle (usually after 15 quiet minutes) and reports on Telegram; `get_codex_job` shows the result. The model's commands run with the user's full rights but pass `policy_hook.py`, which applies the same rules as `run_command` (block, never ask). Jobs are refused in the suite folder, profile folders, AppData and drive roots.
- **Knowledge base search** over the owner's document collections: `search_knowledge` answers with file, page, sheet and row references using keyword search (no model, about 50 ms); the index lives in `data\knowledge\`, sources are read-only, and a project action rebuilds it.
- **Outlook read and draft:** agenda, mail search and drafts saved to Drafts only (the owner sends them). Needs a working default Outlook profile.
- **Morning brief at 07:00** on Telegram, with no model: date, today's meetings, unread mail, today's reminders and suite status (automation `qwen-morning-brief`).
- **Screenshots** (`capture_screen`) to the project's outputs, sent back in the chat on Telegram.
- **Image generation** with Runway (and later the local Qwen-Image pack).
- **More accurate transcription** with faster-whisper `small` (Thai test: 25% vs 38% word errors for `base`, about 3 times slower); long clips or a busy PC use `base` automatically (`QWEN_WHISPER_BUDGET_SECONDS`, `QWEN_WHISPER_MODEL=small|base`).
- **LINE tunnel watchdog fixed:** it used to hang for 27 hours and did not start if the PC booted without internet. The URL changes on every restart and is sent to Telegram automatically.
- **Git history** of the suite in `E:\localllm\.git` (see `GIT-README.txt`).
- Installer 1.5.0 readiness fixes.

## [1.4.0] - 2026-09-30

First git snapshot. It contains everything below.

### Added: Codex-style tools (30 September)
Compared with all 66 topics of OpenAI Codex and added only what pays off on this CPU (qwen3.8 at about 1.4 tokens/s). Most of these need no extra model call, and none adds a confirmation prompt.
- **Targeted file edits with undo:** `edit_text_file` sends only the old and new text (about 150 tokens instead of rewriting a whole file, which would take 20–30 minutes on the CPU), keeps the encoding, BOM and line endings, and backs the file up before every edit or overwrite; the Path Manager shows diffs and undoes changes.
- **Editing existing Word, Excel and PowerPoint files** into a new `<name>-edit-<time>` copy; Excel formulas are recalculated with LibreOffice and errors are reported; files with macros are refused.
- **PDF tools:** render pages as images (with zoom), merge, split, extract, rotate and fill forms (Thai form values are typeset in TH Sarabun New). Encrypted and XFA PDFs are not supported.
- **Templates and charts:** built-in Thai `memo-th` and `report-th` templates, templates saved from the owner's own letterhead files, and PNG charts with Thai labels.
- **Deep research to a Word report** with checked citations (described on the front page).
- **Notifications** when background work ends (a Telegram message and a toast for jobs started from Telegram, a toast for jobs started from WebUI) and a "finished jobs" inbox; jobs shorter than 60 seconds do not notify.
- **Reminders** delivered to Telegram on time without a model call: at most hourly, up to 365 days ahead, up to 50 pending; ambiguous times are refused with examples; a reminder due while the PC sleeps arrives when it wakes.
- **Suite status** (`/status`, Telegram `/qstatus`, `suite_status`, `Test-QwenSuite.ps1`): read-only, about 3–5 seconds, no restart and no model call.
- **Project actions** (up to 9 per project) runnable from chat or the Path Manager, checked by the command policy every time; **AGENTS.md generation** (`/init`) by scanning the folder without a model, never overwriting.
- **Slash prompts and skills** loaded only when used: `/plan`, `/review`, `/summary`, `/letter` (Word), `/minutes` (Word), `/drawing` (Excel), `/qstatus`; `/model` switches models on Telegram.
- **Batch reads** (`local_read` action `batch`, up to 5 items, each checked and audited separately).
- **Quick chat window and dictation:** Ctrl+Alt+Q opens Open WebUI in a small Edge window; Ctrl+Alt+D starts and stops dictation with local Whisper, puts the text on the clipboard and deletes the audio (up to 5 minutes).
- **Faster Open WebUI answers** by switching off unused built-in tools (about 8 KB per message).
- **Qwen Codex**, the Codex CLI talking to the local model: background jobs or an interactive mode from the Start Menu, reviews of git repositories to Markdown (and Word), JSON-schema jobs for nightly runs, `-ResumeLast`, notifications and a status query from chat. It cannot start from Telegram/WebUI at this stage, cannot write until `write_mode` is chosen, cannot reach the network (proxy off, only 127.0.0.1), uses its own `data\codex-home`, and its tool-calling quality with `qwen3-coder` is not yet proven. A job takes about 10–25 minutes and waits for 15 idle minutes.

### Added: security (30 September)
- **Parsed exec-policy** (`local-tools/command_policy.py`) instead of four block-list regexes, for `run_command`, `project_run_command`, `start_project_task` and project actions. It unwraps `powershell/pwsh -Command`, `-EncodedCommand` (decodes base64), `-File` (reads the script), `cmd /c` and `python -c`, strips quotes, backticks and carets, and maps aliases (rm/rd/del to Remove-Item, spps/kill to Stop-Process) before checking. This closed the bypass found on 22 September. Test: 51 dangerous commands blocked, 48 normal commands allowed.
- Owner rules in `data\permission-center\command-rules.json` (reloaded on change; defaults from `local-tools\default-command-rules.json`).
- **Hooks** that can only block: `data\permission-center\hooks.json`; a Pre script receives `{source, action, arguments}` on stdin and returns `{"decision":"block","reason":"..."}`; 5-second timeout, fail-open; Post scripts run in the background; about 60 ms per matching command.
- Commands that touch password stores (Credential Manager, DPAPI, browser `Login Data`, `.openclaw\credentials`) and disk or shadow-copy destruction are blocked; chat commands touching `.codex`, `.openclaw`, `.claude` or `codex-home` are blocked.
- Open WebUI accepts browser requests only from `http://127.0.0.1:3000` / `http://localhost:3000` (it used to accept `*`).
- Limitation: this is a fence, not a sandbox; a script that builds a dangerous command at run time can still get through.

### Added: Claude-style capabilities (30 September)
- Word, Excel, PowerPoint and PDF files from chat (WebUI and Telegram), saved to the project's `outputs` without overwriting, with TH Sarabun New; Telegram sends the file back.
- Web search on Telegram (already in WebUI), with numbered citations; search results are treated as untrusted.
- Code Interpreter button in WebUI (runs in the browser, not on the PC).
- Memory search on Telegram (`memory_search` / `memory_get`, trigram keyword search, nothing sent to OpenAI).
- Streaming partial replies on Telegram; automatic summary of long WebUI chats over 24,000 tokens with `qwen-fast`.
- Thai voice input in WebUI with local Whisper and silence trimming; Thai sample prompts on the new-chat page.
- Router: more keyword forms (bugs, debugging, python3, APIs, github, sqlite …), and follow-ups without task words stay on the model of the last three messages; chat titles use `qwen-fast`.
- OpenClaw heartbeat: an empty "monitor scratch" stops the every-30-minutes model run that searched for a missing HEARTBEAT.md; installer 1.4 sets `heartbeat.every = "0m"`.

### Added: eGPU support and compute selection (29 September)
- `Resolve-OllamaGpu.ps1` picks the GPU before every Ollama start (only an RTX 4090/4060 Ti-class eGPU if present, otherwise Ollama's own choice), `watch-ollama-gpu.ps1` restarts Ollama when a card is plugged in or removed, and `Test-QwenGpu.ps1` checks drivers, cards, profiles and logs.
- Measured: with no eGPU, letting the P620 help reads prompts 3.5 times faster than CPU only (21.2 vs 4.9–6.1 tokens/s) while answering about 15% slower (1.66 vs 1.9 tokens/s), so `OLLAMA_LLM_LIBRARY=cpu` is no longer set.
- Driver plan "Path B" chosen: the latest GeForce driver plus disabling the P620 (R580 is the last driver branch for Pascal), to be done by the owner.
- Choosing the compute device for the whole server (`/compute` page, `Select-QwenCompute.ps1`, or the chat tool `compute_device`) and pinning a model to the CPU (`Set-QwenModelPlacement.ps1`, undone byte for byte).

### Fixed: runner options (29 September)
- Ollama compares `num_ctx`, `num_thread`, `num_batch`, `num_gpu`, `main_gpu`, `use_mmap` and `shift` with the loaded runner and reloads the model when they differ (17 GB, plus re-reading a 20k-token OpenClaw prompt for 13–23 minutes on the CPU). Logs of 23–29 September showed 7 such reloads, 6 of them caused by `shift`.
- Every channel now sends the same options: the router removes `num_thread`, and `shift` for `qwen3.8` and `gpt-oss`; the Open WebUI presets no longer set `num_thread`; the internet gateway and video worker were aligned; `bench.ps1` uses the production options.

## 2026-09-22

### Changed
- More services were added around Ollama and Open WebUI, all bound to `127.0.0.1` and started with `start-webui.ps1`: the local tools (`:8765`), the two permission gateways (`:8766`, `:8767`), the router (`:8770`), the reranker (`:8771`), the internet gateway (`:8780`) and the OpenClaw gateway (`:18789`), plus RAG embeddings, Whisper speech-to-text, Thai TTS, OCR, Video Studio and the Permission Center.
- The Permission Center was set to allow every category for both sources, at the owner's request (19–20 September), and `run_command` defaults to `confirmed=True`, so commands from chat run without a prompt. A test showed the four block-list regexes could be bypassed with `powershell -Command "..."` (closed on 30 September by the parsed exec-policy).
- Chat context compaction re-enabled (threshold 24,000 tokens, 40% retention); `Configure-Qwen-WebUI.py` no longer turns it off.
- The tunnel watchdog checks cloudflared's `/ready` endpoint and repairs only when unhealthy, instead of re-running the whole start script every 2 minutes.
- `Start-Ollama.ps1` reads its settings from the registry, like `start-ollama-server.ps1`.
- Desktop shortcuts use the Qwen Suite logo (24 September).

## 2026-09-12

### Changed
- Speed profile for the main model `qwen3.8:27b` (Q4_K_M, about 17 GB): loaded on the first request and kept for 2 hours, thinking off by default.
- The Qwen profiles use **Local Computer Fast**, which wraps 26 tools in 2 gateway tools: the tool schema shrank from 13,077 to 907 bytes (93.1%), and a tool round through Open WebUI from 141.8 to 104.0 seconds.
- Long Open WebUI chats are summarised at about 24,000 tokens.
- Benchmark after tuning: median 1.58 tokens/s and median 41.82 seconds for 64 tokens (3 warm runs, measured with the options of that time).

## Initial setup (September 2026)

- Ollama and Open WebUI on the Dell Precision 3551 (Intel i7-10750H, 64 GB DDR4-2933, Quadro P620 4 GB, NVMe; models on `E:\ollama-models`). Mixture-of-experts models were chosen because the GPU is too small but RAM is large: they read far fewer weights per token than dense models.
- First benchmark (historical; `gpt-oss:20b` and `qwen3:4b` were removed on 5 October 2026):

  | Model | Size | CPU only | CPU+GPU | Thai quality |
  |---|---|---|---|---|
  | `qwen3-coder:30b` | 18 GB | 11.0 tok/s | 8.7 tok/s | best: concise, follows instructions, finishes code |
  | `gpt-oss:20b` | 13 GB | 6.1 tok/s | 3.5 tok/s | fair, but often spent its tokens on reasoning and did not finish |
  | `qwen3:4b` | 2.5 GB | 8.8 tok/s | 2.7 tok/s | weak: kept reasoning in English instead of answering |

  The 30B mixture-of-experts model (3.3B active) was faster than the dense 4B model, and the P620 made every model slower (33–248%) because of the CPU/GPU traffic per token, so the GPU was switched off at the time (this changed on 29 September).
- Long inputs are slow on the CPU: about 2,800 tokens took 125 seconds to read with `qwen3-coder:30b`, and answers then ran at 3.5 tokens/s.
- Open WebUI trimmed for a CPU: code interpreter, tags, follow-ups and query generation were switched off and titles moved to a small model, so a one-line question went from a 6,673-token prompt (about 4 minutes before the first word) to 249 tokens.
- Ollama's tray app removed from Startup (it started its own server without the suite's settings and saw no models); a junction from `C:\Users\<user>\.ollama\models` to the model folder as a safety net; `stop-all.ps1` also stops the `llama-server` child that keeps a model in RAM; Open WebUI starts minimised, not hidden (hidden, it exits silently).
