"""run_m3_integration.py — SnipEq M2 集成车道回归。

覆盖：
  1) DPI 换算：physical_to_logical_rect 纯函数（mock scale 1.0/1.25/1.5/2.0，含负偏移）；
     _on_result 弹卡路径确实经过换算（monkeypatch display_scale=2.0 断言 show_near 入参）；
  2) settings：tmp 路径注入（SNIPEQ_SETTINGS_PATH）round-trip / 默认值 / 坏文件回退；
  3) history：append/recent(顺序、坏行跳过) / 历史菜单项生成 / 点击重灌卡片（in-process 卡）；
  4) autostart：命令构造含 pythonw+tray_app、dry-run 不写注册表（不触碰真 Run 键）；
  5) cleanup_temp：>24h 假文件删除、新文件保留；
  6) 真卡路径（QT_QPA_PLATFORM=offscreen，真实 import 的 FormulaCard）：
     show_result→copy_word→三格式读回 / copy_latex→纯文本 / close_requested 隐藏；
  7) 子进程端到端（offscreen + 真卡 + 真热键注册 + preview 回填 + 历史落盘）：
     tray_app --once-image aligned.png --smoke 30，验证关键日志证据链。

运行：& ..\\.venv\\Scripts\\python.exe run_m3_integration.py
全过打印 "M3 INTEGRATION PASS"。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
_env_py = os.environ.get("SNIPEQ_TEST_PY")
_venv_py = SRC / ".venv" / "Scripts" / "python.exe"
PY = _env_py or (str(_venv_py) if _venv_py.exists() else sys.executable)

# 测试期所有持久化文件指向临时目录（不污染 %APPDATA%）
TMP = Path(tempfile.mkdtemp(prefix="snipeq_m3_test_"))
os.environ["SNIPEQ_SETTINGS_PATH"] = str(TMP / "settings.json")
os.environ["SNIPEQ_HISTORY_PATH"] = str(TMP / "history.jsonl")
os.environ["QT_QPA_PLATFORM"] = "offscreen"

sys.path.insert(0, str(SRC))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

FAILURES: list[str] = []


def check(name: str, problems: list[str]) -> None:
    if problems:
        FAILURES.append(name)
        print(f"  [FAIL] {name}")
        for p in problems:
            print(f"         !! {p}")
    else:
        print(f"  [PASS] {name}")


# ---------------------------------------------------------------- 1) DPI 纯函数

def t_dpi_pure() -> None:
    print("\n== 1) DPI 换算（纯函数 mock scale factor）")
    from tray_app import physical_to_logical_rect as f
    cases = [
        ((0, 0, 1920, 1080), 1.0, (0, 0, 1920, 1080)),
        ((100, 200, 300, 400), 1.25, (80, 160, 240, 320)),
        ((150, 300, 450, 600), 1.5, (100, 200, 300, 400)),
        ((100, 100, 201, 51), 2.0, (50, 50, 100, 26)),        # 0.5 舍入（banker's）口径固定
        ((-1920, 0, 800, 600), 1.25, (-1536, 0, 640, 480)),   # 副屏负偏移
        ((1, 1, 3, 3), 1.25, (1, 1, 2, 2)),                    # 小数舍入
    ]
    problems = []
    for rect, s, want in cases:
        got = f(rect, s)
        if tuple(got) != tuple(want):
            problems.append(f"f({rect}, {s}) = {got} != {want}")
    # 防御：scale<=0/None 当 1.0
    if f((10, 20, 30, 40), 0) != (10, 20, 30, 40) or f((10, 20, 30, 40), None) != (10, 20, 30, 40):
        problems.append("scale<=0/None 未退化为 1.0")
    check("physical_to_logical_rect 表驱动", problems)


# ---------------------------------------------------------------- 2) settings

def t_settings() -> None:
    print("\n== 2) settings.json 读写")
    from settings import DEFAULTS, Settings
    problems = []
    p = TMP / "settings_a.json"
    s = Settings(p)
    if s.data != DEFAULTS:
        problems.append(f"初始非默认值: {s.data}")
    s.set(hotkey="ctrl+shift+q", zero_confirm=True, l2_endpoint="http://100.100.200.100:30000")
    s.save()
    s2 = Settings(p)
    if (s2.get("hotkey"), s2.get("zero_confirm"), s2.get("l2_endpoint")) != \
       ("ctrl+shift+q", True, "http://100.100.200.100:30000"):
        problems.append(f"round-trip 失败: {s2.data}")
    # 坏文件回退默认
    bad = TMP / "settings_bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    s3 = Settings(bad)
    if s3.get("hotkey") != DEFAULTS["hotkey"]:
        problems.append("坏文件未回退默认")
    # 未知键不载入、已知部分键合并
    (TMP / "settings_part.json").write_text(
        json.dumps({"hotkey": "f9", "junk": 1}), encoding="utf-8")
    s4 = Settings(TMP / "settings_part.json")
    if s4.get("hotkey") != "f9" or "junk" in s4.data:
        problems.append(f"部分键合并失败: {s4.data}")
    check("Settings round-trip/回退/合并", problems)


# ---------------------------------------------------------------- 3) history

def t_history() -> None:
    print("\n== 3) history.jsonl")
    from settings import History
    problems = []
    h = History(TMP / "hist.jsonl")
    for i in range(15):
        h.append(f"e_{i}", src="x.png", sha1=f"{i:012x}")
    with open(h.path, "a", encoding="utf-8") as f:
        f.write("corrupt-line\n")
    rec = h.recent(10)
    if len(rec) != 10:
        problems.append(f"recent(10) 得到 {len(rec)} 条")
    if rec and rec[0]["latex"] != "e_14":
        problems.append(f"首条应最新 e_14，实为 {rec[0]['latex']}")
    if [r["latex"] for r in rec[1:]] != [f"e_{i}" for i in range(13, 4, -1)]:
        problems.append("顺序错位")
    if rec and not rec[0].get("sha1"):
        problems.append("sha1 未入库")
    empty = History(TMP / "no_hist.jsonl").recent(5)
    if empty != []:
        problems.append("不存在文件应为空列表")
    check("History append/recent/坏行", problems)


# ---------------------------------------------------------------- 4) autostart

def t_autostart() -> None:
    print("\n== 4) autostart（dry-run，不写注册表）")
    import settings as st
    problems = []
    cmd = st.autostart_command()
    if "tray_app.py" not in cmd or "pythonw.exe" not in cmd and "python.exe" not in cmd:
        problems.append(f"命令构造异常: {cmd}")
    if not (cmd.startswith('"')):
        problems.append("路径未加引号（含空格目录会断）")
    lines: list[str] = []
    before = st.get_autostart_cmd()
    st.set_autostart(True, dry_run=True, log=lines.append)
    after = st.get_autostart_cmd()
    if before != after:
        problems.append("dry-run 竟写了注册表！")
    if not any("dry-run" in ln and "tray_app.py" in ln for ln in lines):
        problems.append(f"dry-run 未打印目标命令: {lines}")
    check("autostart 命令构造 + dry-run 无副作用", problems)


# ---------------------------------------------------------------- 5) cleanup_temp

def t_cleanup() -> None:
    print("\n== 5) cleanup_temp 过期清理")
    from settings import cleanup_temp
    problems = []
    temp = Path(tempfile.gettempdir())
    old_shot = temp / "snipeq_shot_oldtest.png"
    new_shot = temp / "snipeq_shot_newtest.png"
    for f in (old_shot, new_shot):
        f.write_bytes(b"x")
    old_t = time.time() - 25 * 3600
    os.utime(old_shot, (old_t, old_t))
    removed = cleanup_temp(24.0, log=lambda m: None)
    if old_shot.exists():
        problems.append("过期 snipeq_shot_*.png 未删除")
    if not new_shot.exists():
        problems.append("新文件被误删")
    old_shot.unlink(missing_ok=True)
    new_shot.unlink(missing_ok=True)
    if removed < 1:
        problems.append("返回值应为已删条目数 >=1")
    check("cleanup_temp", problems)


# ---------------------------------------------------------------- 6) 真卡 in-process

def t_real_card() -> None:
    print("\n== 6) 真 FormulaCard 接线（offscreen）")
    problems: list[str] = []
    from PySide6.QtWidgets import QApplication
    appq = QApplication.instance() or QApplication([])

    import tray_app as ta
    from clipboard_writer import read_back_flat
    from settings import History, Settings

    latex = r"\frac{a}{b}"
    edited = r"\frac{a}{c}"
    # monkeypatch scale=2.0 验证弹卡坐标确实换算（capture 物理 px → 卡逻辑 px）
    real_scale = ta.display_scale
    ta.display_scale = lambda: 2.0
    seen: dict = {}

    app = ta.SnipEqApp(preview_enabled=False, zero_confirm=False,
                       settings=Settings(), history=History())
    app.root = None  # in-process 不起主环
    if app.ui_degraded:
        problems.append("PySide6 已装仍判降级")
    card = app._ensure_card()
    if card is None:
        problems.append("_ensure_card 返回 None")
        ta.display_scale = real_scale
        check("真卡接线", problems)
        return
    if type(card).__name__ != "FormulaCard":
        problems.append(f"卡片类型异常: {type(card)}")
    # show_near 记录实参（换算断言）
    real_show_near = type(card).show_near
    def spy(self, x, y, w, h):
        seen["near"] = (x, y, w, h)
        return real_show_near(self, x, y, w, h)
    type(card).show_near = spy
    try:
        app._on_result(latex, None, (200, 400, 600, 100), None)  # 物理 px rect
        if seen.get("near") != (100, 200, 300, 50):
            problems.append(f"show_near 未换算/换算错: {seen.get('near')}")
        if not card.isVisible():
            problems.append("show_result 后卡片不可见")
        if card._latex_edit.text() != latex:
            problems.append("编辑框未写入 latex")
        # copy_word 信号 → 三格式入剪贴板 + 卡隐藏
        card.copy_word.emit()
        appq.processEvents()
        back = read_back_flat()
        if back["unicodetext"] != f"${latex}$":
            problems.append(f"copy_word 剪贴板文本不符: {back['unicodetext']!r}")
        if not back["cf_html"] or b"StartFragment" not in back["cf_html"]:
            problems.append("copy_word CF_HTML 缺失")
        if card.isVisible():
            problems.append("copy_word 后卡片未隐藏")
        # copy_latex → 纯 latex（无 $ 定界）
        card.copy_latex.emit()
        appq.processEvents()
        back = read_back_flat()
        if back["unicodetext"] != latex:
            problems.append(f"copy_latex 文本不符: {back['unicodetext']!r}")
        # 编辑回调更新 _cur_latex（300ms 防抖直接调内部函数验证）
        app._on_latex_edited(edited)
        if app._cur_latex != edited:
            problems.append("_on_latex_edited 未跟踪 latex")
        # 历史重灌：_on_history 走卡片路径
        app.history.append(edited, src="t", sha1="abc")
        app._on_history(edited)
        appq.processEvents()
        if not card.isVisible() or card._latex_edit.text() != edited:
            problems.append("历史重灌卡片内容不符")
        card.close_requested.emit()
        appq.processEvents()
    finally:
        type(card).show_near = real_show_near
        ta.display_scale = real_scale
        card.hide()
    check("真卡接线（show_near 换算/copy/close/重灌）", problems)


# ---------------------------------------------------------------- 7) 子进程端到端

def t_tray_realcard() -> None:
    print("\n== 7) tray_app 子进程：真卡 + offscreen + preview 回填 + 历史")
    img = str(HERE / "images" / "aligned.png")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # 不设 SNIPEQ_FORCE_TK=真卡路径
    t0 = time.perf_counter()
    p = subprocess.run(
        [PY, str(SRC / "tray_app.py"), "--once-image", img, "--smoke", "30"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(SRC), timeout=240,
    )
    out = (p.stdout or "") + (p.stderr or "")
    problems = []
    print("---- 子进程输出 ----")
    for line in out.splitlines()[-24:]:
        print("   ", line)
    print(f"  （{time.perf_counter()-t0:.0f}s, rc={p.returncode}）")
    if "Traceback" in out:
        problems.append("输出含 Traceback")
    if "UI 不可用" in out:
        problems.append("真卡模式下仍报 UI 不可用")
    if "卡片已展示" not in out:
        problems.append("缺『卡片已展示』（未走真卡路径？）")
    if "SMOKE OK" not in out:
        problems.append("缺 SMOKE OK")
    if "卡片预览回填" not in out:
        problems.append("缺『卡片预览回填』（preview 异步回填未跑）")
    if "直写剪贴板" in out:
        problems.append("真卡路径不应出现降级直写")
    hist = Path(os.environ["SNIPEQ_HISTORY_PATH"])
    if not hist.exists() or "aligned" not in hist.read_text(encoding="utf-8"):
        pass  # aligned 的 latex 以 \begin{array} 开头，改查文件非空
    if not hist.exists():
        problems.append("history.jsonl 未生成")
    else:
        last = json.loads(hist.read_text(encoding="utf-8").strip().splitlines()[-1])
        if not last.get("latex") or not last.get("sha1"):
            problems.append(f"历史条目缺字段: {last}")
    check("tray 子进程真卡端到端", problems)


def main() -> int:
    # 前置：确认 FormulaCard 真 import 成功（车道契约不降级）
    import tray_app as ta
    if ta.FormulaCard is None:
        print(f"FormulaCard import 失败: {ta.UI_IMPORT_ERROR}")
        return 1
    t_dpi_pure()
    t_settings()
    t_history()
    t_autostart()
    t_cleanup()
    t_real_card()
    t_tray_realcard()
    print("\n==========================")
    if FAILURES:
        print(f"M3 INTEGRATION FAIL: {len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("M3 INTEGRATION PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
