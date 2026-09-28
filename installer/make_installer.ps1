# make_installer.ps1 — SnipEq 安装器构建：staging（build.ps1 -SkipZip）→ ISCC 编译
# 用法: pwsh -File installer\make_installer.ps1 [-SkipStaging] [-RebuildPortable]
#   -SkipStaging     直接用现有 package\SnipEq staging（默认行为=重跑 staging；
#                    发布面冻结期建议加本参数，连 staging 都不动）
#   -RebuildPortable 完整 build.ps1（会重打便携 zip——发布纪律内默认禁用）
param([switch]$SkipStaging, [switch]$RebuildPortable)
$ErrorActionPreference = "Stop"

$root  = (Resolve-Path "$PSScriptRoot\..").Path
$ver   = ((Select-String -Path "$root\src\version.py" -Pattern '__version__\s*=\s*"([\d.]+)"').Matches[0].Groups[1].Value)
$ISCC = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $ISCC) { throw "未找到 ISCC.exe（Inno Setup 6）。先 winget install JRSoftware.InnoSetup" }
"ISCC   = $ISCC"
"Version= $ver"

if (-not $SkipStaging) {
  if ($RebuildPortable) {
    & pwsh -NoProfile -File "$root\package\build.ps1"
  } else {
    & pwsh -NoProfile -File "$root\package\build.ps1" -SkipZip
  }
  if ($LASTEXITCODE -ne 0) { throw "build.ps1 失败 rc=$LASTEXITCODE" }
} else {
  if (-not (Test-Path "$root\package\SnipEq\app\tray_app.py")) { throw "staging 不存在，去掉 -SkipStaging 先构建" }
  "跳过 staging（使用现有 package\SnipEq）"
}

# staging 版本一致性检查（防拿旧 staging 出错包）
$sv = (Select-String -Path "$root\package\SnipEq\app\version.py" -Pattern '__version__\s*=\s*"([\d.]+)"').Matches[0].Groups[1].Value
if ($sv -ne $ver) { throw "staging version=$sv 与 src version=$ver 不一致：先重跑 staging" }

$out = "$PSScriptRoot\dist"
New-Item -ItemType Directory -Force -Path $out | Out-Null
& $ISCC "/DAppVersion=$ver" "$PSScriptRoot\SnipEq.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败 rc=$LASTEXITCODE" }
$exe = Get-Item "$out\SnipEq-Setup-$ver.exe"
"SETUP OK: $($exe.FullName)"
"  size  = {0:N0} bytes ({1:N1} MB)" -f $exe.Length, ($exe.Length/1MB)
"  sha256= " + (Get-FileHash $exe.FullName -Algorithm SHA256).Hash
