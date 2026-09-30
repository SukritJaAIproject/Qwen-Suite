# Local Computer tools for Open WebUI

These localhost-only MCP services give the `qwen3.8:27b` workspace model access to:

- files under `C:\Users\EGAT` and `E:\` (credential folders and Open WebUI's database/secret are blocked),
- CMD, PowerShell, Conda, Python, Git, VS Code, Explorer, FFmpeg, FFprobe and FFplay (including approved GUI launch),
- video/audio inspection, compression, trimming, resizing, muting, audio extraction, GIF conversion and transcoding.
- isolated Open WebUI projects backed by `E:\qwen-projects\<project>`, each with its own `MEMORY.md`, documents, attachments, outputs and work folders.
- per-project path configuration with friendly aliases and `read_only` or `read_write` access. Project tools reject paths that are not configured for that project.
- persistent background-task monitoring for long-running Python, Conda, FFmpeg, Git, CMD and PowerShell jobs. Status, PID, elapsed time, progress, stdout and stderr are available per project.
- Qwen Video Studio one-click jobs: prompt to storyboard, designed scenes, TTS, original music, subtitles, MP4, previews and quality checks, with automatic checkpoint resume and up to three attempts.

Projects created by Qwen also appear as native Open WebUI folders. Their system prompt scopes work to the selected project, uploaded files become that folder's knowledge, and memory entries use the native path `projects/<project-slug>` in addition to the local `MEMORY.md` fallback.

Permission Center is the authoritative policy layer. The local owner's WebUI and configured messaging channels default to **Allow**, so normal project, program, media, and task actions run without repeated approval prompts. A small set of high-risk disk, recursive-delete, shutdown, and local-AI-killing commands remains blocked.

Full server URL (**32** direct tools, selectable when needed): `http://127.0.0.1:8765/mcp`
(counted 2026-09-22 with a direct `tools/list` JSON-RPC call; the file said 26, which was the count before the video and document tools were added. Note the `local-computer` connection is currently `enable=false` in Open WebUI, so this surface is reachable through `/gateway/dispatch` rather than through a model profile.)

Fast gateway URL (2 compact tools, attached to Qwen by default): `http://127.0.0.1:8766/mcp`

The fast gateway dispatches to the same full service, so it keeps every file, Project, memory, task-monitor, program, and video capability while shrinking the always-attached tool schema from 13,077 to 907 UTF-8 bytes (measured by `scripts\measure_mcp_schema.py`). What that is worth end to end: the request prompt fell from about 5,800 to 618 input tokens. The wall-clock figure quoted elsewhere (141.8 s -> 104.0 s) compared two runs that both went through this gateway, so treat it as indicative only - the dominant cost on this machine is CPU inference, not schema size. Its read dispatcher returns the action catalog on demand with `action=help`.

Project Path Manager: `http://127.0.0.1:8765/path-manager`

Video Studio: `http://127.0.0.1:8765/video-studio`

The Open WebUI launcher loads `qwen-project-paths.js` and `qwen-video-studio.js`, which add **Projects & Tasks** and **Video Studio** buttons inside the Qwen UI. Each project always has a built-in `project` alias for its isolated workspace. Additional locations such as `videos`, `documents`, `anaconda`, data folders, or external drives can be configured independently.

For a new video, call the compact gateway once with `action=generate_video`, then poll `get_video_job`. Manual multi-step FFmpeg/TTS chains should only be used for specialized editing of existing media.

Commands expected to take more than a few seconds should use `start_project_task`. The monitor keeps a persistent registry under `E:\qwen-projects\.task-monitor`, captures bounded log tails, and can safely stop only tasks it registered. Direct MCP stop requests require explicit confirmation; the UI also shows a confirmation dialog before stopping a task.

Logs:

- `E:\localllm\logs\local-tools.log`
- `E:\localllm\logs\local-tools-error.log`
- `E:\localllm\logs\local-tools-fast.log`
- `E:\localllm\logs\local-tools-fast-error.log`
