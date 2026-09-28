# SnipEq 安装器冒烟测试留档（2026-09-28，本机构建与验证）

环境：VMware Windows 11 x64（session 1，交互桌面），Inno Setup **6.7.3**
（`winget install JRSoftware.InnoSetup` 一次成功，per-user 装至
`%LOCALAPPDATA%\Programs\Inno Setup 6`）。staging=package\SnipEq（v0.1.2 构建，
`-SkipStaging` 复用，**未重打便携 zip、未动已发布 Release**）。

## 产物

```
installer\dist\SnipEq-Setup-0.1.2.exe
  size  = 351,567,018 bytes (335.3 MB)
  sha256= 3AA2C5C76FFE9DE902CF1C7B6F02FD5BB3699941A9C77FB796B5142798DAF860
  编译: ISCC /DAppVersion=0.1.2，Successful compile (383.9s)
```

注：`[Setup]` 指令实名 `PrivilegesRequiredOverridesAllowed`（任务书写作
"PrivilegesRequiredOverrides"，6.7.3 探针核实该名不存在，按官方文档纠正为
`dialog`——静默安装按当前用户 per-user 装，不弹提权选择）。
官方 Inno 6.7.3 发行不含简体中文 isl → 界面按预案回退**英文**。

## 四验

### ① 安装（/VERYSILENT /CURRENTUSER）
- `install rc=0`，默认位置 `C:\Users\OpenClaw\AppData\Local\Programs\SnipEq\`
- 安装树关键文件全 True：`python\pythonw.exe`、`python\python312._pth`、
  `python\Lib\tkinter\__init__.py`、`python\Lib\site-packages\PySide6\QtCore.pyd`、
  `python\tcl\{tcl8.6,tk8.6}\*.tcl`、`app\tray_app.py`、`app\ui\formula_card.py`、
  `app\version.py`(0.1.2)、`paddle_cache\official_models\PP-FormulaNet_plus-S\inference.pdiparams`、
  `使用说明.txt`、`检查环境.bat`、`unins000.exe`
- 开始菜单 3 项：`SnipEq（公式快贴）.lnk` / `检查环境.lnk` / `卸载 SnipEq.lnk`
- 便携包专用 `SnipEq启动.vbs` 不随安装版分发（预期 False）
- 卸载注册表 `HKCU\...\Uninstall\SnipEq_is1`：DisplayVersion=0.1.2，
  InstallLocation=安装目录 ✓

### ② 安装版静默运行冒烟
| 用例 | 结果 |
|---|---|
| pythonw.exe tray_app.py --smoke 8 | 4s 存活驻留，**退出码 0** |
| python.exe tray_app.py --once-image aligned.png --smoke 25（同树，取日志） | **rc=0，SMOKE OK，无 Traceback** |
| pythonw.exe tray_app.py --once-image aligned.png --smoke 40 | 6s 存活，**退出码 0** |

附加观察：并发第二实例时日志显示
`RegisterHotKey('ctrl+alt+a') 失败 err=1409`（热键被另一实例占用）→
"仅菜单可用"降级路径按设计工作（1409=ERROR_HOTKEY_ALREADY_REGISTERED）。

### ③ 卸载（unins000.exe /VERYSILENT /SUPPRESSMSGBOXES）
- `uninstall rc=0`
- 安装目录已清理（Test-Path False）；`[UninstallDelete] {app}` 连运行期
  `__pycache__` 一并清除
- 开始菜单目录已清理
- 卸载注册表项已移除

### ④ 残留检查
- `%APPDATA%\SnipEq`（settings.json + history.jsonl）**保留** ✓（静默卸载一律
  保留；交互卸载弹 Yes/No 询问，默认 No=Keep）
- `HKCU\...\Run` 无 `SnipEq` 值残留（安装器全程未写 Run 键，自启归应用管）✓
- 无 SnipEq 目录下 python/pythonw 进程残留 ✓（环境恢复干净）

## 结论

装/运行/卸载/残留 四验全过。安装器与便携包共享同一 staging 与
settings.py 的 HKCU Run 自启口径（autostart_command 取 sys.executable 同目录
pythonw + settings.py 同目录 tray_app → 安装版自启指向安装路径，无解压位置漂移）。
