# ForeContract Desktop

[简体中文](README.zh-CN.md)

“Can you find out why this is wrong? Hold off on changing anything.”

That’s how I want to be able to start a coding session. An addition function in your project returns -1 for 2 and 3. For now you want an explanation: which line did the assistant read, and what do the existing tests cover? Once you decide it’s worth changing, you can follow up with “Go ahead and fix it, then run a test.” The files you just discussed and the questions you asked belong to the same conversation.

ForeContract Desktop is a coding assistant for Windows. When a discussion turns into an edit, the workbench shows you which files the model wants to change and how it proposes to verify the fix. Editing requires approval of the write scope and verification plan. The actual local command comes back to you for separate approval. There’s a practical reason for that pause: you might be happy to change one line of arithmetic and still want to see the command that’s about to run on your machine.

**One thing I especially want this interface to preserve is work that only partly succeeded.** Suppose the line was fixed, but the test failed because the path was wrong. Open the run’s diff to see whether the minus sign became a plus. Open its command result to see how far the test got. Both facts matter. When you return to the project later, the change and the failure are still attached to that run. Model conversations require an online model configuration. Generated plans still benefit from a human looking at whether the chosen test exercises the change.

### Why a separate desktop project?

At the desktop, what matters is being able to talk through a problem and let the assistant act when you’re ready. Underneath is a different question: how can a model state its expectation before acting, and how can a runtime check the outcome afterwards? I’ve kept that work in a separate Python framework. Its README describes the **OFCEVR Loop**, what led to it and where I’ve found its limits. [ForeContract framework](https://github.com/zale-pan/ForeContract)

### Trying it on Windows

**The Windows x64 installer is still under evaluation.** The MSI bundles Electron, Python and the required Git tools for direct installation and launch from the Start menu. Installation, launch and tray exit have been checked on a development machine. Session readback after relaunch, data retention after uninstall and a clean Windows installation test are pending. There is no release download yet.

The workbench currently uses Chinese labels. Closing the window sends it to the notification area and lets the host keep running. Open it from the tray to restore the window, or choose “退出 ForeContract” to quit the client and host. Cancellation cleanup overlapping host shutdown remains a known limitation for local trials.

The source instructions below are for people who want to run or develop the application themselves. They open the offline workbench, where projects, sessions and history can be inspected. The model conversation in the opening example requires online mode.

## Run from source (contributors)

Prerequisites: Windows x64, PowerShell, Git, Python 3.12 and internet access for dependencies. Place the two source checkouts side by side, named ForeContract and ForeContract-Desktop. Run both blocks in the same PowerShell session from the Desktop checkout. The framework checkout must contain the exact commit in framework.lock.json. Git exports that commit even if your framework working tree has edits.

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


## Recover an interrupted Electron download

If the download stops before extraction, keep the same PowerShell session so `$run` and the installed Python environment remain available. Download `electron-v45.4.1-win32-x64.zip` from the [official Electron v45.4.1 release](https://github.com/electron/electron/releases/tag/v45.4.1) using your browser or another connection. Save the complete archive separately from the partial file. Paste its full path without surrounding quotes when prompted.

```powershell
$zip = (Get-Item -LiteralPath (Read-Host 'Full path to the complete Electron ZIP')).FullName
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne '2ba6223afa9121f42831fefe92c434334907540165294fc56697449d12e7cfe9') { throw 'Electron SHA-256 mismatch' }
Expand-Archive -LiteralPath $zip -DestinationPath "$run\electron" -ErrorAction Stop
```

After extraction, resume the launch block at `$env:FC_DESKTOP_ROOT = ...` and run every remaining line. A checksum error means you must obtain a fresh copy before extracting.

## A note from the author

I built this app because I wanted to use the framework myself and see how it felt in everyday coding. I want to share it so I can learn from other people’s experience with it, too.

I’m still finding my feet with frontend, backend and desktop development. If a screen made you hesitate, or you found yourself wishing something worked differently, I’d love to hear about it. You can [open an issue](https://github.com/zale-pan/ForeContract-Desktop/issues) or email me directly.

Zale Pan · <hashuuki@gmail.com>

Copyright 2026 Zale Pan · Apache-2.0. [LICENSE](LICENSE) · [Third-party notices / 第三方声明](THIRD_PARTY_NOTICES.md).
