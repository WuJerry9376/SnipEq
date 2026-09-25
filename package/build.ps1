# build.ps1 — SnipEq 便携包组装（embedded Python 3.12.10 + venv site-packages 裁剪搬运）
# 用法: pwsh -File package\build.ps1   （幂等，robocopy /MIR 可重跑）
$ErrorActionPreference = "Stop"
$root   = (Resolve-Path "$PSScriptRoot\..").Path        # 仓库根（脚本相对，无个人路径）
$pkg    = "$root\package"
$stage  = "$pkg\SnipEq"
$work   = "$pkg\_work"
$embed  = "$work\embed"                 # 已解压的 python-3.12.10-embed-amd64
# 系统 Python 3.12（tkinter 组件来源）；机器差异用环境变量 SNIPEQ_SYS_PY 覆盖
$sysPy  = if ($env:SNIPEQ_SYS_PY) { $env:SNIPEQ_SYS_PY } else { "$env:LOCALAPPDATA\Programs\Python\Python312" }
$venvsp = "$root\src\.venv\Lib\site-packages"
$src    = "$root\src"
$spike  = "$root\spike"

function RC($args_) {
  $quoted = $args_ | ForEach-Object { if ("$_" -match '\s') { '"' + $_ + '"' } else { "$_" } }
  $p = Start-Process robocopy -ArgumentList $quoted -PassThru -Wait -NoNewWindow
  if ($p.ExitCode -ge 8) { throw "robocopy failed rc=$($p.ExitCode): $quoted" }
}

"=== 1. 骨架清理/建目录"
foreach ($d in @("$stage\python", "$stage\app", "$stage\paddle_cache")) {
  if (Test-Path $d) { Remove-Item $d -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}

"=== 2. python\ = embed + tkinter + ._pth"
Copy-Item "$embed\*" "$stage\python\" -Recurse -Force
Copy-Item "$sysPy\DLLs\_tkinter.pyd" "$stage\python\" -Force
Copy-Item "$sysPy\DLLs\tcl86t.dll"   "$stage\python\" -Force
Copy-Item "$sysPy\DLLs\tk86t.dll"    "$stage\python\" -Force
Copy-Item "$sysPy\DLLs\zlib1.dll"    "$stage\python\" -Force   # installer 版 tcl86t 依赖外置 zlib，embed 无此 dll
New-Item -ItemType Directory -Force -Path "$stage\python\Lib" | Out-Null
RC @("$sysPy\Lib\tkinter", "$stage\python\Lib\tkinter", "/MIR", "/NFL", "/NDL", "/NJH", "/NJS")
New-Item -ItemType Directory -Force -Path "$stage\python\tcl" | Out-Null
RC @("$sysPy\tcl\tcl8.6", "$stage\python\tcl\tcl8.6", "/MIR", "/NFL", "/NDL", "/NJH", "/NJS")
RC @("$sysPy\tcl\tk8.6",  "$stage\python\tcl\tk8.6",  "/MIR", "/NFL", "/NDL", "/NJH", "/NJS")
@"
python312.zip
.
Lib
Lib\site-packages

# enable site-packages (pip-style layout)
import site
"@ | Set-Content "$stage\python\python312._pth" -Encoding ascii

"=== 3. site-packages 搬运（排除 pycache/tests/二进制杂物）"
New-Item -ItemType Directory -Force -Path "$stage\python\Lib\site-packages" | Out-Null
RC @($venvsp, "$stage\python\Lib\site-packages", "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/MT:16",
     "/XD", "__pycache__", "tests", "test", "docs", "doc", "sphinxcontrib",
     "pip", "wheel", "pkg_resources",
     "/XF", "*.pdb", "*.lib", "*.h", "*.hpp", "*.c", "*.pyx")

"=== 4. PySide6 裁剪（只留 QtCore/QtGui/QtWidgets 运行时）"
$ps6 = "$stage\python\Lib\site-packages\PySide6"
$keepPyd = @("QtCore.pyd","QtGui.pyd","QtWidgets.pyd","QtSvg.pyd")
Get-ChildItem "$ps6\*.pyd" | Where-Object { $keepPyd -notcontains $_.Name } | Remove-Item -Force
$keepDll = @("Qt6Core.dll","Qt6Gui.dll","Qt6Widgets.dll","Qt6Network.dll","Qt6Svg.dll",
             "opengl32sw.dll","d3dcompiler_47.dll","libEGL.dll","libGLESv2.dll","pyside6.abi3.dll",
             "concrt140.dll","msvcp140*.dll","vcruntime140*.dll","vccorlib140.dll","vcomp140.dll","vcamp140.dll")
Get-ChildItem "$ps6\*.dll" | Where-Object { $keepDll -notcontains $_.Name } | Remove-Item -Force
foreach ($d in @("qml","resources","metatypes","include","lib","glue","typesystems","support",
                 "QtAsyncio","scripts","translations","plugins\sqldrivers","plugins\networkinformation",
                 "plugins\tls","plugins\virtualkeyboard","plugins\platforminputcontexts",
                 "plugins\sceneparsers","plugins\geoservices","plugins\renderplugins",
                 "plugins\texticonengine")) {
  if (Test-Path "$ps6\$d") { Remove-Item "$ps6\$d" -Recurse -Force }
}
Get-ChildItem "$ps6" -Filter "*.pyi" -File | Remove-Item -Force   # 顶层存根（模块导入不依赖）
if (Test-Path "$ps6\doc") { Remove-Item "$ps6\doc" -Recurse -Force }
# cv2 的 ffmpeg 视频后端 dll（公式识别只用 imread/resize，不需要 VideoCapture）
Get-ChildItem "$stage\python\Lib\site-packages\cv2" -Filter "opencv_videoio_ffmpeg*.dll" -ErrorAction SilentlyContinue | Remove-Item -Force

"=== 5. paddle_cache 模型（禁止目标机联网下载）"
RC @("$spike\model\paddlex_cache\official_models", "$stage\paddle_cache\official_models",
     "/E", "/MIR", "/NFL", "/NDL", "/NJH", "/NJS")

"=== 6. app\ = src 代码 + 测试 + katex_assets"
$pyFiles = @("snipeq.py","recognizer.py","normalize.py","mathml.py","clipboard_writer.py",
             "preview.py","capture.py","tray_app.py","settings.py","check_env.py",
             "version.py","update.py",
             "formula_pipeline_S.yaml")
foreach ($f in $pyFiles) { Copy-Item "$src\$f" "$stage\app\" -Force }
Copy-Item "$src\preview_katex.js" "$stage\app\" -Force   # node 快速路线子进程端（缺失时 preview 自动走浏览器路线）
Copy-Item "$src\preview_katex.js" "$stage\app\" -Force
New-Item -ItemType Directory -Force -Path "$stage\app\ui" | Out-Null
Copy-Item "$src\ui\*.py" "$stage\app\ui\" -Force
RC @("$src\tests", "$stage\app\tests", "/MIR", "/NFL", "/NDL", "/NJH", "/NJS",
     "/XD", "_work", "/XF", "*.tmp")
New-Item -ItemType Directory -Force -Path "$stage\app\katex_assets" | Out-Null
Copy-Item "$spike\model\node_modules\katex\dist\katex.min.js"  "$stage\app\katex_assets\" -Force
Copy-Item "$spike\model\node_modules\katex\dist\katex.min.css" "$stage\app\katex_assets\" -Force
Copy-Item "$spike\model\node_modules\katex\dist\fonts" "$stage\app\katex_assets\fonts" -Recurse -Force
# 开发机 node 快速路线的 katex 模块也搬入（<10MB，有 node 环境更快；无 node 自动走浏览器路线）
New-Item -ItemType Directory -Force -Path "$stage\app\katex_node_modules" | Out-Null
Copy-Item "$spike\model\node_modules\katex" "$stage\app\katex_node_modules\katex" -Recurse -Force
RC @("$spike\model\node_modules", "$stage\app\katex_node_modules", "/E", "/MIR", "/NFL", "/NDL",
     "/NJH", "/NJS", "/XD", "katex\node_modules\.bin", "__pycache__")

"=== 7. 入口脚本与说明（M3b 热修：vbs 语法 End If + 全部 GBK936/CRLF 落盘，bat 路径加引号，txt 用 UTF-8 BOM）"
# wscript/cmd 原生按 ANSI(办公机=936) 读脚本；PS7 默认 UTF-8 无 BOM 会致 VBScript
# 编译器把中文当乱码/语法错（800A03F6 根因之一），且 here-string 源行尾是裸 LF。
function Write-Gbk([string]$path, [string]$text) {
  $crlf = ($text -split "`r?`n") -join "`r`n"
  [IO.File]::WriteAllText($path, $crlf, [Text.Encoding]::GetEncoding(936))
}
function Write-Utf8Bom([string]$path, [string]$text) {
  $crlf = ($text -split "`r?`n") -join "`r`n"
  [IO.File]::WriteAllText($path, $crlf, (New-Object Text.UTF8Encoding($true)))
}
$vbs = @'
' SnipEq 静默启动（无控制台窗口）：托盘出现即成功，Ctrl+Alt+A 开始截图
Option Explicit
Dim fso, shell, root, pyw, script
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = root & "\python\pythonw.exe"
script = root & "\app\tray_app.py"
If Not fso.FileExists(pyw) Then
  MsgBox "找不到 python\pythonw.exe，包不完整？", 16, "SnipEq"
  WScript.Quit 1
End If
If Not fso.FileExists(script) Then
  MsgBox "找不到 app\tray_app.py，包不完整？", 16, "SnipEq"
  WScript.Quit 1
End If
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root & "\app"
shell.Run """" & pyw & """ """ & script & """", 0, False
' 托盘图标在右下角（可能被折叠进 ^ 溢出区）
'@
Write-Gbk "$stage\SnipEq启动.vbs" $vbs
Write-Gbk "$stage\SnipEq启动-调试.bat" @"
@echo off
rem 带控制台启动（看日志用）
cd /d "%~dp0app"
"%~dp0python\python.exe" tray_app.py
pause
"@
Write-Gbk "$stage\检查环境.bat" @"
@echo off
cd /d "%~dp0app"
"%~dp0python\python.exe" check_env.py
pause
"@
if (Test-Path "$pkg\使用说明.txt") { Copy-Item "$pkg\使用说明.txt" "$stage\使用说明.txt" -Force }

$sizeMB = [math]::Round((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum/1MB,0)
"=== 完成: $stage  ($sizeMB MB)"

"=== 8. 清理 __pycache__（验证纪律：构建阶段产物必须无字节码缓存）"
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | ForEach-Object {
  Remove-Item $_.FullName -Recurse -Force -EA SilentlyContinue
}
$left = @(Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__").Count
if ($left -gt 0) { throw "stage 内仍有 $left 个 __pycache__，拒绝打包" }
".pyc 残留: " + @(Get-ChildItem $stage -Recurse -File -Filter "*.pyc" -EA SilentlyContinue).Count

"=== 9. 打 zip 并二次断言（zip entry 不得含 __pycache__）"
Add-Type -AssemblyName System.IO.Compression
$zipPath = "$pkg\SnipEq-portable.zip"
Remove-Item $zipPath -EA SilentlyContinue
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal, $true)
$z = [IO.Compression.ZipFile]::OpenRead($zipPath)
$bad = @($z.Entries | Where-Object { $_.FullName -match "__pycache__|\.pyc$" })
$entryN = $z.Entries.Count
$z.Dispose()
if ($bad.Count -gt 0) { throw "zip 混入 $($bad.Count) 个 pycache 条目: $($bad[0].FullName) …" }
$zi = Get-Item $zipPath
"zip OK: $entryN entries, {0:N0} bytes, SHA256={1}" -f $zi.Length,
    (Get-FileHash $zipPath -Algorithm SHA256).Hash
