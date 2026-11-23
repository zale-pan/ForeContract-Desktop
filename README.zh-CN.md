# ForeContract 桌面客户端

[English](README.md)

Windows Electron 客户端与本地 Python 宿主提供会话、审批、运行历史和证据查看。下面从空目录启动离线工作区。准备 Windows x64、PowerShell、Git 和 Python 3.12，安装依赖时需要联网。两个源码仓库分别命名为 ForeContract 和 ForeContract-Desktop，放在同一父目录下。从桌面仓库根目录开始，在同一 PowerShell 窗口依次运行两段命令。框架仓库需要包含 framework.lock.json 指定的提交；安装会直接从该提交导出源码，工作区中尚未提交的修改不会进入安装。

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

点窗口 × 会收起到托盘，点击托盘图标可以恢复。右键图标选择 **退出 ForeContract**，客户端与宿主都会退出。再次使用同一工作区时，沿用相同的数据根并重新执行启动命令。界面文字目前为中文，离线体验无需模型密钥。取消清理与宿主关停重叠仍为已知限制，真实模型自主提出计划的可靠性仍在探索。

## 恢复中断的 Electron 下载

如果下载在解压前中断，保留原 PowerShell 窗口，继续使用已有的 `$run` 和 Python 环境。在浏览器中打开 [Electron v45.4.1 官方发布页](https://github.com/electron/electron/releases/tag/v45.4.1)，下载 `electron-v45.4.1-win32-x64.zip`；也可以换一个网络下载。将完整包和已中断的文件分开保存。出现提示时粘贴完整包的绝对路径，两端不用加引号。

```powershell
$zip = (Get-Item -LiteralPath (Read-Host 'Full path to the complete Electron ZIP')).FullName
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne '2ba6223afa9121f42831fefe92c434334907540165294fc56697449d12e7cfe9') { throw 'Electron SHA-256 mismatch' }
Expand-Archive -LiteralPath $zip -DestinationPath "$run\electron" -ErrorAction Stop
```

解压成功后，从启动代码块的 `$env:FC_DESKTOP_ROOT = ...` 那一行继续，执行全部余下命令。若摘要不匹配，请先重新获取完整压缩包。

Copyright 2026 Zale Pan · Apache-2.0. [LICENSE](LICENSE) · [Third-party notices / 第三方声明](THIRD_PARTY_NOTICES.md).
