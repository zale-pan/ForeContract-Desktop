# ForeContract Desktop

[简体中文](README.zh-CN.md)

A Windows Electron client with a local Python host for conversations, approvals, run history and evidence. This source setup starts a fresh offline workspace. Prerequisites: Windows x64, PowerShell, Git, Python 3.12 and internet access for dependencies. Place the two source checkouts side by side, named ForeContract and ForeContract-Desktop. Run both blocks in the same PowerShell session from the Desktop checkout. The framework checkout must contain the exact commit in framework.lock.json. Git exports that commit even if your framework working tree has edits.

```powershell
$desktop = (Resolve-Path .).Path
$fw = (Resolve-Path ..\ForeContract).Path
$lock = Get-Content "$desktop\framework.lock.json" -Raw | ConvertFrom-Json
$run = Join-Path $env:LOCALAPPDATA ("FC-Desktop-" + [guid]::NewGuid())
New-Item -ItemType Directory $run | Out-Null
git -C $fw archive --format=zip -o "$run\framework.zip" $lock.commit
if ($LASTEXITCODE) { throw 'Framework revision unavailable' }
Expand-Archive "$run\framework.zip" "$run\source"
py -3.12 -m venv "$run\venv"
$env:FC_DESKTOP_PYTHON = "$run\venv\Scripts\python.exe"
$python = $env:FC_DESKTOP_PYTHON
$env:PYTHONPATH = ''
& $python -m pip install setuptools==84.0.0
if ($LASTEXITCODE) { throw 'Build dependency installation failed' }
& $python -m pip install --no-build-isolation -r "$desktop\apps\local\requirements.txt" "$run\source\packages\core" "$run\source\packages\planners\llm" "$run\source\packages\adapters\coding"
if ($LASTEXITCODE) { throw 'Desktop dependency installation failed' }
```

```powershell
$zip = "$run\electron-v45.4.1-win32-x64.zip"
Invoke-WebRequest 'https://github.com/electron/electron/releases/download/v45.4.1/electron-v45.4.1-win32-x64.zip' -OutFile $zip
if ((Get-FileHash $zip).Hash -ne '2ba6223afa9121f42831fefe92c434334907540165294fc56697449d12e7cfe9') { throw 'Electron SHA-256 mismatch' }
Expand-Archive $zip "$run\electron"
$env:FC_DESKTOP_ROOT = "$run\profile-root"
$env:FC_DESKTOP_PROJECT = "$run\profile-root\project"
$env:FC_DESKTOP_LIVE_CHAT = '0'
$env:PYTHONPATH = "$desktop\apps\local"
& "$run\electron\electron.exe" "$desktop\apps\desktop"
```

Close the window with × to hide it. Click the tray icon to reopen. Right-click the tray icon and select **退出 ForeContract** to end the client and its host. To keep using the same workspace, reuse the same profile root and repeat the launch command. The bundled interface is currently Chinese. No model key is needed for this walkthrough. Overlapping cancellation and shutdown remains a known limitation; general live-model plan quality is experimental.

Copyright 2026 Zale Pan · Apache-2.0. [LICENSE](LICENSE) · [Third-party notices / 第三方声明](THIRD_PARTY_NOTICES.md).
