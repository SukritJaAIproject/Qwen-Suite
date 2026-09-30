@echo off
rem Codex-Qwen bridge shim for batch files and Task Scheduler. Runs one job in the
rem foreground - same idle gate, lock, notification and exit codes as
rem Invoke-QwenCodex.ps1 - and returns its exit code. Arguments pass straight through:
rem   qwen-exec.cmd -Cwd E:\qwen-projects\demo\work -PromptFile E:\jobs\nightly.md -OutputSchema E:\jobs\summary.schema.json -JsonOut
rem   qwen-exec.cmd -Review -Cwd E:\qwen-projects\demo\repo
rem Put long or Thai prompts in a UTF-8 file (-PromptFile): cmd.exe treats & | % ^ " in
rem arguments as its own syntax.
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0Invoke-QwenCodex.ps1" %*
exit /b %ERRORLEVEL%
