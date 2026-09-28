# SnipEq src — M1 骨架 + M2 集成（托盘/热键/预览/浮卡）

链路：取图 → **PP-FormulaNet_plus-S**（PaddleX, CPU）识别 → **归一化**（N1–N11）→
**latex2mathml** → Presentation MathML → **剪贴板三格式写入**（CF_HTML /
CF_UNICODETEXT `$latex$` / MathML 自定义格式）。本机无 Word，端到端验证口径 =
写入后读回三格式校验（`tests/run_e2e.py`）。

## 环境

- 解释器：`src\.venv`（Python 3.12.10），依赖见 `requirements.txt`
  （paddlepaddle 3.3.1 / paddlex[ocr] 3.7.2 与 spike venv 精确对齐）。
- 装包镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`。
- **模型不重新下载**：`recognizer.py` 在 import paddlex 前把
  `PADDLE_PDX_CACHE_HOME` 指向 `../spike/model/paddlex_cache`
  （参数名核实自 paddlex `utils/cache.py:29`，用户显式设置的环境变量优先）。
  权重即 spike 已下载的 `official_models/PP-FormulaNet_plus-S/`（约 245MB，只读引用）。

## 命令速查

```powershell
$PY = "src\.venv\Scripts\python.exe"

# 识别图片文件 → 写剪贴板（成功后 Word/WPS 里 Ctrl+V）
& $PY src\snipeq.py image path\to\formula.png

# 读系统剪贴板图片（Win+Shift+S 截屏后直接用）
& $PY src\snipeq.py clip

# 只输出 JSON {latex, mathml_len, timings}，不写剪贴板
& $PY src\snipeq.py image path\to\formula.png --json

# 端到端回归（4 张合成测试图，全链路+三格式读回校验，通过打印 E2E PASS）
& $PY src\tests\run_e2e.py

# M2 冒烟（capture 注入/管线+三态读回/preview 计时/托盘降级壳 SNIPEQ_FORCE_TK）
& $PY src\tests\run_m2_smoke.py

# M2 集成回归（DPI/设置/历史/自启 dry-run/真卡 offscreen/子进程端到端）
& $PY src\tests\run_m3_integration.py

# 托盘主程序（热键截图→识别→浮卡；--once-image 跳框选、--smoke N 秒自退）
& $PY src\tray_app.py

# 重新生成测试图（需本机 node+KaTeX(spike 内只读引用) 与 Edge，一般不用重跑）
& $PY src\tests\gen_test_images.py
```

退出码：`0` 成功；`2` 识别失败/低质量（空结果、超长串 >600 字符）；
`3` 归一化/转换/剪贴板失败；`4` 剪贴板无图片。

## 模块与 vendor 来源映射

| src 文件 | 来源 | 说明 |
|---|---|---|
| `snipeq.py` | M1 新写 | CLI 入口，分段时间统计 |
| `recognizer.py` | 用法源自 `spike/model/smoke_test.py` | PaddleX pipeline 封装 + 低质量判定；**M3b 复读循环熔断** |
| `formula_pipeline_S.yaml` | vendor `spike/model/formula_pipeline_S.yaml` | 内容一致 |
| `normalize.py` | vendor `spike/convert/normalize.py` | N1–N9 一致；**M1 增补 N10/N11、M3b 增补 N12**（见下） |
| `mathml.py` | vendor `spike/clipboard/clipboard_formula.py`（latex_to_mathml 部分） | **aligned `&` 实体转义修复保留**（裸 `&`→`&amp;`） |
| `clipboard_writer.py` | vendor `spike/clipboard/clipboard_formula.py` | **M2 同步 v2 加固**：错误码透出/指数退避/三格式隔离写入（CF_HTML 失败才致命）/三态读回 |
| `preview.py` + `preview_katex.js` | M2 新写 | LaTeX→PNG 预览桥：常驻 node(katex, stdio 行协议) + headless Edge 截图 + 紧裁；失败返回 None 降级纯文本 |
| `capture.py` | M2 新写 | 全局热键(ctypes RegisterHotKey) + 多屏全屏冻结 + TkOverlay 橡胶筋遮罩（overlay_factory 可替换 PySide6 版） |
| `tray_app.py` | M2 新写 | 托盘主程序：Qt 主环默认（QTimer 泵 Tk）+ pystray 菜单 + 卡片/降级双路径 + DPI 物理→逻辑换算 + 历史/自启/清理 |
| `settings.py` | M2 新写 | settings.json（热键/零确认/卸载/自启/L2 占位/历史开关）+ history.jsonl + HKCU Run 自启（写前打印命令）+ 24h 临时清理 |
| `ui/formula_card.py` | des-1 车道 | PySide6 结果浮卡（契约：show_result/show_near/信号 copy_word, copy_latex, cloud_enhance, zero_confirm_toggled, close_requested） |
| `tests/run_m2_smoke.py` | M2 新写 | 冒烟 b/c/d（FORCE_TK 降级口径） |
| `tests/run_m3_integration.py` | M2 集成新写 | DPI 换算/设置/历史/自启 dry-run/cleanup/真卡 offscreen/子进程端到端 |
| `tests/images/*.png` | `gen_test_images.py` 生成（KaTeX+Edge，同 spike/model/make_synth.py 方案） | frac_sum/cases/matrix/aligned |
| `tests/run_e2e.py` | M1 新写 | 全链路回归 + 读回校验 + 推理 P50 |

## M1 已知修正与增补（相对 spike）

- **N10**：环境（array/cases/aligned）末行尾随 `\\` 剥离——PP-FormulaNet 真实输出带
  尾行分隔符，latex2mathml 3.81.1 抛 `ExtraLeftOrMissingRightError`。
- **N11**：行尾悬挂 `{` 补闭——模型对 cases 输出 `{x\geq0,\\`（单元格花括号未闭合）。
- 两条均有函数 docstring 内实测证据；spike 的 N1–N9 规则未改动。
- **N12（M3b）**：`\boldsymbol`→`\mathbf` 并轨近似（real138 依据，斜体牺牲换 Word
  侧渲染稳定；`\boldsymbolx` 长宏不匹配、既有 `\mathbf` 不误触，回归见
  `tests/run_m3a_fixes.py`）。
- **复读熔断（M3b）**：识别输出滑窗子串 ≥10 字符重复 ≥8 次，或 >300 字符且去重
  token 比 <0.35 → 判低质量（real060 型解码循环，阈值在 recognizer.py REP_* 常量）。

## 性能记录（2026-09-25，本机 8C16G CPU 无 GPU，run_e2e 实测）

| 图 | 推理耗时(s) |
|---|---|
| aligned（冷启动首张） | 1.067 |
| cases | 0.706 |
| frac_sum | 0.863 |
| matrix | 0.849 |

推理 P50 ≈ **0.85s**（含 PaddleX 内部 resize 预处理；去冷启动样本）。
模型加载一次性 ~5–8s。归一化+转换 ≤0.03s、剪贴板写入 ~0.002s 可忽略。
推理段口径与车道②（spike/model 50 张跑批）对账时注意：本表为单进程连跑、
batch_size=1、CPU 默认线程数。

## 当前边界（M1）

- **真机 Word 粘贴验证未做**（本机无 Word/WPS）：验收在用户办公机按
  `spike/clipboard/checklist.md` 流程执行（CF_HTML→原生公式、`$LaTeX$` 自动转换等）。
- 识别质量门限 M1 只做粗检（空/超长），PaddleX 公式管线不输出 token 置信度，
  置信度路由（L1→L2）留 M2/v1.1。
- 混排截图、托盘 UI、热键截图均不在 M1 范围（规划书 §6 MVP 划分）。
- `tests/_work/` 为测试图生成的中间产物（HTML/Edge profile），可删。

## M2 集成（托盘 + 浮卡 + 预览，2026-09-25）

启动托盘：`& $PY src\tray_app.py`（Ctrl+Alt+A 截图 → 框选 → 识别 → 选区旁浮卡，
Enter/主按钮=复制为 Word 公式，Esc 取消；菜单含零确认/空闲卸载/开机自启/最近历史）。
设置持久化在 `%APPDATA%\SnipEq\settings.json`，历史在 `history.jsonl`（测试期可用
`SNIPEQ_SETTINGS_PATH`/`SNIPEQ_HISTORY_PATH` env 重定向；`SNIPEQ_FORCE_TK=1` 强制降级口径）。

### 已知边界（M2 口径）

- **preview 单张 <500ms 未达成**：KaTeX 环节热态 ~5ms（常驻 node），但 **msedge
  进程启动 ~1s** 为硬成本（virtual-time-budget/window-size 调参实测无效）。现状以
  "卡片先文本降级、PNG 异步回填"缓解（不阻塞出卡与剪贴板）。**严格 <500ms 留
  v1.1 常驻 Edge/CDP captureScreenshot 改造**；打包形态也可评估 CEF/QtWebEngine
  直接内嵌渲染（浮卡本就是 Qt，可 HTML 渲染免截图）。
- **多屏混合 DPI 近似**：`show_near` 坐标按**主屏 scale** 由物理像素换算
  （`physical_to_logical_rect`，单测 mock 1.0/1.25/1.5/2.0 已验）；从属屏缩放
  因子不同时选区位置会有偏差，真机矩阵（M3）验证。
- **开机自启**：写 `HKCU\...\Run\SnipEq`（pythonw + tray_app 绝对路径），
  开启前日志打印将写入的命令；打包（Inno Setup）后此项应改指 exe，见 M3。
- **L2 云端增强为占位**：settings.json `l2_endpoint` 字段已留（4090 地址），
  卡片按钮禁用，路由逻辑 v1.1 接入。
- 真卡冒烟以 `QT_QPA_PLATFORM=offscreen` 跑通（无头证据链：卡片展示/预览回填/
  copy 信号入剪贴板）；TkOverlay 人工框选交互（橡胶筋/Esc/Enter）仍需 dogfooding 验证。
  （**M3e 已由办公机实测**：Enter 键确认链路存在 P0 卡死，见下节修复。）

## M3e P0 修复（截图遮罩卡死 + 驻留内存增长，v0.1.1）

办公机 dogfooding 实锤两条 P0，均已定位根因并结构性修复：

- **P0-A 确认后遮罩不消失、整机假死**。根因链（采证见 `tests/repro_capture.py`）：
  1. `TkOverlay.run()` 的 tray 复用分支用 `root.wait_window(top)` 等待，而
     confirm/cancel 只设 `_done` 标志从不 destroy `top` → wait_window 永不返回
     （M2 冒烟走注入工厂绕过了 run() 交互路径，故未暴露）；
  2. 键盘绑定只在 top 上；托盘后台进程被 OS 前台锁拦 `SetForegroundWindow` 时
     Enter/Esc 送达不到遮罩（`overrideredirect` 窗口 focus_force 不可靠）；
  3. Qt 主环下 `QTimer _tick` + `root.after _tick` **双源泵**互相复利：repro 实测
     `_pump` 调用 340→1102→1549 次/秒（单源稳态应 ≈66），root 销毁后仍残留数百
     个 `invalid command name _tick` 定时器 → 越跑越卡直至假死、内存缓涨。
  修复：① run() 统一为**自检 `_done` 的轮询环 + finally 强制 destroy/解绑/释放图像**，
  任何异常路径遮罩必撤；② `bind_all` Return/Escape + AttachThreadInput 强抢前台；
  ③ **线程所有权**：Qt 模式下 tkinter 全部对象（遮罩/热键对话框）迁入专属
  `TkUiThread` 自持 mainloop，主线程只经队列收发（`region_ready` 等事件），
  **跨线程零触碰 Tk**；QTimer 单源泵 events 队列，`_pump` 不再 update Tk；
  ④ 逃生三保险：遮罩内 **120s 无操作看门狗**（自动取消）、**Ctrl+Alt+Q 全局中止
  热键**（不依赖键盘焦点，置 threading.Event 由轮询环 100ms 内撤罩）、截图防重入
  + 150s 主环兜底解锁。repro 实测：watchdog 0.64s 自撤、abort 0.45s 自撤。
- **P0-B 驻留内存增长**。头号嫌疑逐一排除（`tests/soak_capture.py`，30 轮完整
  "截图→识别→剪贴板→预览"）：① Edge 截图子进程改为**显式 Popen 跟踪 + wait 收尸
  + 超时 kill + close() 统一清扫**，末测残留进程 0；② 全屏冻结图（每张几十 MB）
  改**即抛路径**：overlay finally `del photo/frozen + gc`、capture_region 裁剪后即
  `del frozen`、tray 落盘后 `del img`；③ worker 任务级异常也保证结果回投（错误
  可见不静默）。soak 实测：RSS 增幅 **+4MB**（预算 <50MB）、句柄 658→655 稳定。

### VM 注入限制定论（2026-09-26 诊断更新，替代早前"B 级天花板"说法）

早前 repro 报"SendInput 被系统拒绝(injected=0)，降级 B 级"是**误诊**，真凶两处：
1. **探针结构体错**：自定义 INPUT 只放 KEYBDINPUT（sizeof=32 ≠ 官方 40——union
   须被 32B 的 MOUSEINPUT 撑起）→ SendInput 一律 `err=87 ERROR_INVALID_PARAMETER`
   全数拒收，被误读为"环境拒绝注入"。修：`repro_capture._input_structs()` 官方
   布局 + `sizeof==40` 断言。
2. **capture._steal_foreground 64 位句柄截断**：`GetForegroundWindow` 未设 restype
   （默认 c_int 截 HWND）+ `GetWindowThreadProcessId` 挂错 kernel32 且无 argtypes →
   AttachThreadInput 拿错 tid、抢前台恒失败 → E1"遮罩未获前台"。修后
   SetForegroundWindow=True、前台=遮罩根窗口（**此为 v0.1.1 键盘路径的落地修复**）。

环境侧复测（重启 VM + 退出 ToDesk 客户端）：三桌面名一致（WinSta0/Default/Default）、
GetCursorInfo flags=0、SendInput 拖拽 injected=4、Enter injected=2、GetAsyncKeyState
响应正常；ToDesk_Service 残留（Stopped）与注入失败无因果。ToDesk 运行期"光标不动
假接受"是真实的远控接管现象，退出+重启后即恢复。**结论：本机 VM 恢复 A 级验证能力**。
repro_capture 现为 A/B 自适应（OS 注入探针成功走真实拖拽+Enter，被拒自动退回画布
合成事件并如实标注等级）。2026-09-26 A 级实测：`level: A`、真实拖拽+Enter 关遮罩
（run 返回 box=(300,200,500,320)）、watchdog 0.65s / abort 0.46s 双逃生复测过、
全套回归（e2e/m2/m3i/m3a/m3c/soak）绿。办公机真机拖拽仍是最终验收口径。

## M3c 功能（修改快捷键 + 检查更新，2026-09-25）

- **版本单一事实源**：`src\version.py` 的 `__version__`（snipeq/tray/update 共用；
  GitHub release tag 与之对应，`v` 前缀可选）。
- **修改快捷键**（托盘菜单「修改快捷键…」）：tkinter 模态对话框捕获单次
  key-down（Ctrl/Alt/Shift/Win + 主键或 F1–F12，实时回显，裸 Esc 取消、确定/取消按钮）；
  校验链 = `capture.parse_hotkey` 即时校验 → **先注销旧键、试注册新键，
  冲突/非法自动回滚旧键并弹错**（`SnipEqApp._swap_hotkey`）；成功后写
  settings.json `hotkey` 键并**热生效**（无需重启）。Win 键组合可能被系统快捷键
  拦截、部分办公机无独立 Win 键位，冲突时按提示改组合即可。
- **检查更新**（托盘菜单「检查更新（vX.Y.Z）」）：`src\update.py` urllib GET
  GitHub Releases latest（UA 头、8s 超时、系统代理），semver 三元组比较；
  反馈三态文案区分：`new_version`（对话框"发现新版本 vX（当前 vY）"+ 打开下载页）、
  `up_to_date`（气泡）、`error`（网络失败 / 仓库未就绪 404 分别文案，气泡）。
  **仅菜单点击才联网**，平时零打扰零上报。仓库位置常量集中在 update.py
  `GITHUB_OWNER/GITHUB_REPO/LATEST_API`，换名只改一处。
- 测试：`tests/run_m3c_features.py`（semver 表驱动 / check_latest 本地 fixture
  四态 / keysym+validate 纯逻辑 / **_swap_hotkey 真注册+冲突回滚+落盘 round-trip**）。
- 顺带修复 `capture.HotkeyManager` 多实例缺陷：窗口类名原为固定值，热键换键时
  新实例复用旧实例（已释放）的 wndproc 指针 → 0xC0000409 崩溃；现每实例唯一类名
  + 线程退出时 UnregisterClassW。

## 便携包快照漂移记录（M3b 小修回流，2026-09-25）

§12.2 决策：M3a 两项即修"**小修回流 src、暂不重建便携包**"。当前
`package\SnipEq\app\` 为 02:31 构建快照，与开发树差异如下（等 M3 尾批统一重跑
`package\build.ps1` 同步即可；**build.ps1 无需改动**，其复制清单已覆盖下列文件）：

| 文件 | 漂移内容 | 旧包用户影响 |
|---|---|---|
| `recognizer.py` | M3b 复读循环熔断：`_looks_repetitive`（A 滑窗子串 `REP_WINDOW=10`×`REP_MAX_REPEAT=8`；B `len>REP_TOKEN_GUARD_LEN=300` 且去重 token 比 `<REP_MIN_UNIQUE_RATIO=0.35`），命中走 RecognitionError→snipeq exit 2/托盘气泡（real060 依据） | 无熔断：955×54 长条类选区仍 3s+ 出垃圾（不崩，仅质量差） |
| `normalize.py` | M3b 新增 N12：`\boldsymbol`→`\mathbf`（real138 依据；3.81.1 实测 boldsymbol 基本式不丢符号，改并轨动机是 Word 侧 bold-italic 变体渲染风险，见函数 docstring） | 含 `\boldsymbol` 输出按 bold-italic 原样进 MathML |
| `tests/run_m3a_fixes.py` | 新增回归套件（熔断 3 正 6 负 + FakePipe 接线 + N12/real138 GT） | 无 |

**M3c 追加漂移**（本车道按指令未动 package\，重投递由后续车道处理）：
`app\` 需新增 `version.py`、`update.py`（**build.ps1 §6 `$pyFiles` 清单必须加入这两个文件**，
否则包内 tray_app import 即崩），更新 `tray_app.py`（修改快捷键/检查更新/_tick 心跳重构）、
`capture.py`（HotkeyManager 唯一类名+UnregisterClass 修复）；`app\tests` 随 /MIR 自动带上
`run_m3c_features.py`。
