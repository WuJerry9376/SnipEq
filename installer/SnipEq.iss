; SnipEq.iss — Inno Setup 6 per-user 安装器脚本（M3 尾批）
;
; 设计要点：
;  * 源 = package\SnipEq\（build.ps1 staging 产物：embedded python + site-packages +
;    paddle_cache + app + 入口文件）。先跑 build.ps1 -SkipZip 生成/刷新 staging，
;    再用 make_installer.ps1 编译本脚本。
;  * per-user 安装：PrivilegesRequired=lowest + PrivilegesRequiredOverrides=allowed
;    （允许用户自愿提权，但默认目录固定 {localappdata}\Programs\SnipEq，不碰系统盘）。
;  * 快捷方式直启 pythonw.exe + tray_app.py（不走 vbs：安装版路径固定，vbs 是便携包
;    的零依赖入口，安装版保留它会形成两套入口易混淆）。便携包内的 vbs/bat 不随安装版
;    分发（便携 ZIP 仍含，互不影响）。
;  * 开机自启：安装器**不写** Run 键——与 settings.py::autostart_command 同口径，
;    由应用内托盘开关管理（其指向 sys.executable 目录 pythonw，安装版即安装路径，
;    天然无漂移）。
;  * 卸载：默认保留 %APPDATA%\SnipEq（设置/历史），交互确认后才会删除；
;    静默卸载一律保留。
;  * 语言：官方 Inno Setup 6.7.3 发行包不含 ChineseSimplified.isl（官方仅默认英语 +
;    29 种社区语言，简体中文属"非官方翻译包"范畴）→ 本脚本回退英文界面（Default.isl）。
;    若日后放入 installer\Languages\ChineseSimplified.isl，本脚本自动启用（见 ISPP 条件）。
;  * AppVersion 由 make_installer.ps1 从 src\version.py 读取并以 /DAppVersion=x.y.z 传入；
;    直接双击编译时用 fallback 值。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define StagingDir AddBackslash(SourcePath) + "..\package\SnipEq"
; ISPP: 存在本地中文语言文件（installer\Languages\ChineseSimplified.isl）则启用中文，
; 官方 Inno Setup 6.7.3 发行不含简体中文 isl → 本机走英文回退。
#if FileExists(AddBackslash(SourcePath) + "Languages\ChineseSimplified.isl")
  #define UseZh "yes"
#else
  #define UseZh "no"
#endif

[Setup]
AppId=SnipEq
AppName=SnipEq（公式快贴）
AppVersion={#AppVersion}
AppVerName=SnipEq {#AppVersion}
VersionInfoVersion={#AppVersion}.0
DefaultDirName={localappdata}\Programs\SnipEq
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=lowest
; 指令全名注意：6.7.3 为 PrivilegesRequiredOverridesAllowed（dialog=安装向导让用户
; 选是否提权；静默默认按当前用户=per-user 装 {localappdata}，符合任务口径）
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\python\pythonw.exe
UninstallDisplayName=SnipEq（公式快贴）
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=
OutputDir=dist
OutputBaseFilename=SnipEq-Setup-{#AppVersion}
LicenseFile=LICENSE.txt
; 中文语言包开关（本机无文件 → 英文）
#if UseZh == "yes"
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=yes
#else
LanguageDetectionMethod=none
#endif

[languages]
#if UseZh == "yes"
Name: "zh"; MessagesFile: "Languages\ChineseSimplified.isl"
#else
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Files]
; 全量 staging：python 运行时 / app 源码与测试 / 模型缓存 / 使用说明 / 入口 bat
Source: "{#StagingDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StagingDir}\app\*";    DestDir: "{app}\app";    Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StagingDir}\paddle_cache\*"; DestDir: "{app}\paddle_cache"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#StagingDir}\使用说明.txt";    DestDir: "{app}";  Flags: ignoreversion
Source: "{#StagingDir}\检查环境.bat";    DestDir: "{app}";  Flags: ignoreversion
Source: "{#StagingDir}\SnipEq启动-调试.bat"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; 运行期 preview/temp 由 %TEMP% 承担，安装目录无用户写入需求；不预建

[Icons]
Name: "{autoprograms}\SnipEq\SnipEq（公式快贴）"; Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\app\tray_app.py"""; WorkingDir: "{app}\app"; \
  Comment: "启动托盘：Ctrl+Alt+A 截图识别公式（设置见 %APPDATA%\SnipEq）"
Name: "{autoprograms}\SnipEq\检查环境"; Filename: "{app}\检查环境.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\SnipEq\卸载 SnipEq"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SnipEq（公式快贴）"; Filename: "{app}\python\pythonw.exe"; \
  Parameters: """{app}\app\tray_app.py"""; WorkingDir: "{app}\app"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional tasks:"; Flags: unchecked

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\tray_app.py"""; \
  WorkingDir: "{app}\app"; Description: "Launch SnipEq tray"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载连根清理安装目录（含运行期可能生成的 __pycache__）；用户数据在 %APPDATA%，
; 由下方 [Code] 问询决定去留
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    { Silent uninstall: ALWAYS keep user settings/history. }
    if UninstallSilent then
      Exit;
    if MsgBox('Delete SnipEq settings & recognition history (%APPDATA%\SnipEq)?' + #13#10 +
              'Default: Keep (No).', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\SnipEq'), True, True, True);
  end;
end;
