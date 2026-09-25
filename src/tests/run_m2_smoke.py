"""run_m2_smoke.py — SnipEq M2 车道 A 冒烟（无真机 Word，口径=读回校验+进程无崩溃）。

覆盖任务 6 的 b/c/d 三项（a 项回归直接跑 run_e2e.py）：
  b) capture 管线注入（full_image_factory 跳人工框选验选区 plumbing）
     + 4 张现成测试图 模拟完整管线：recognizer → normalize → 三格式写剪贴板 → v2 三态读回；
  c) preview.py 对 4 张测试图对应 LaTeX 渲染 PNG + 计时（含常驻 node 二次热渲染）；
  d) tray_app.py 子进程 --once-image + --smoke 起壳：热键注册、降级直写、SMOKE OK、无 Traceback。

运行：& ..\\.venv\\Scripts\\python.exe run_m2_smoke.py [--skip-tray]
全部通过打印 "M2 SMOKE PASS"，退出码 0。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
# 便携包布局无 .venv：SNIPEQ_TEST_PY > venv(开发机) > 当前解释器
import os as _os
_env_py = _os.environ.get("SNIPEQ_TEST_PY")
_venv_py = SRC / ".venv" / "Scripts" / "python.exe"
PY = _env_py or (str(_venv_py) if _venv_py.exists() else sys.executable)
sys.path.insert(0, str(SRC))
try:  # GBK 控制台打印诊断输出不炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from clipboard_writer import (  # noqa: E402
    read_back, verify_cf_html, verify_mathml_xml, write_clipboard_formula,
)
from normalize import normalize  # noqa: E402

IMAGES = sorted((HERE / "images").glob("*.png"))
MANIFEST = json.loads((HERE / "images" / "manifest.json").read_text(encoding="utf-8"))

FAILURES: list[str] = []


def check(name: str, problems: list[str]) -> None:
    if problems:
        FAILURES.append(name)
        print(f"  [FAIL] {name}")
        for p in problems:
            print(f"         !! {p}")
    else:
        print(f"  [PASS] {name}")


def three_format_readback(latex: str, mathml: str) -> list[str]:
    """写剪贴板 → v2 三态读回校验（同 run_e2e 口径）。"""
    problems: list[str] = []
    info = write_clipboard_formula(latex)
    problems += [f"[write] {w}" for w in info["warnings"]]
    back = read_back()
    r = back["cf_html"]
    if r["state"] != "ok":
        problems.append(f"CF_HTML 读回 {r['state']}: {r['detail']}")
    else:
        problems += [f"[cf_html] {p}" for p in verify_cf_html(r["data"], mathml)]
    r = back["unicodetext"]
    if r["state"] != "ok":
        problems.append(f"CF_UNICODETEXT 读回 {r['state']}: {r['detail']}")
    elif r["data"].decode("utf-16-le").rstrip("\x00") != f"${latex}$":
        problems.append("CF_UNICODETEXT 不匹配")
    r = back["mathml"]
    if r["state"] != "ok":
        problems.append(f"MathML 读回 {r['state']}: {r['detail']}")
    elif r["data"].decode("utf-8") != mathml:
        problems.append("MathML 与写入不一致")
    else:
        problems += [f"[mathml] {p}" for p in verify_mathml_xml(r["data"].decode("utf-8"))]
    return problems


# ---------------------------------------------------------------- b) capture plumbing

def smoke_capture() -> None:
    print("\n== b0) capture：全屏冻结 + 全图注入选区（无遮罩 UI）")
    import capture

    problems: list[str] = []
    try:
        img, rect = capture.capture_region(overlay_factory=capture.full_image_factory)
        if img is None:
            problems.append("capture_region 返回 None（full_image_factory 不应取消）")
        else:
            vx, vy, vw, vh = capture.virtual_screen_origin_size()
            if img.size != (vw, vh):
                problems.append(f"选区图 {img.size} != 虚拟屏 {(vw, vh)}")
            if rect != (vx, vy, vw, vh):
                problems.append(f"物理矩形 {rect} != {((vx, vy, vw, vh))}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"异常: {e}")
    check("capture 全图注入", problems)


# ---------------------------------------------------------------- b) 管线 + 剪贴板

def smoke_pipeline() -> None:
    print("\n== b) 4 图完整管线（注入全图选区口径）→ 写剪贴板 → 三格式读回")
    from recognizer import FormulaRecognizer

    rec = FormulaRecognizer()
    for img in IMAGES:
        t0 = time.perf_counter()
        raw, infer_s = rec.recognize(str(img))
        latex, _ = normalize(raw)
        from mathml import latex_to_mathml
        mathml = latex_to_mathml(latex)
        problems = three_format_readback(latex, mathml)
        el = time.perf_counter() - t0
        print(f"  {img.name:16s} infer={infer_s:.2f}s total={el:.2f}s  {latex[:52]}")
        check(f"pipeline+clipboard[{img.stem}]", problems)


# ---------------------------------------------------------------- c) preview

def smoke_preview() -> None:
    print("\n== c) preview.py：LaTeX→PNG 渲染计时（目标 <500ms/张，热身后口径）")
    from preview import PreviewRenderer

    with PreviewRenderer() as pv:
        for name, latex in MANIFEST.items():
            problems: list[str] = []
            t0 = time.perf_counter()
            png = pv.render(latex)
            el = time.perf_counter() - t0
            if png is None:
                problems.append(f"render 返回 None: {pv.last_error}")
            elif not Path(png).is_file():
                problems.append(f"PNG 不存在: {png}")
            note = "  <-- 含 node 冷启动" if name == next(iter(MANIFEST)) else ""
            print(f"  {name:16s} {el*1000:7.0f}ms -> {png}{note}")
            check(f"preview[{name}]", problems)
        # 热路径重复渲染（常驻 node 复用）
        latex = MANIFEST["frac_sum"]
        for i in range(3):
            t0 = time.perf_counter()
            png = pv.render(latex)
            el = time.perf_counter() - t0
            print(f"  warm#{i+1:<14} {el*1000:7.0f}ms -> {png}")
            if png is None:
                check(f"preview warm#{i+1}", [f"None: {pv.last_error}"])
        # 坏 LaTeX 必须返回 None 而非抛异常
        bad = pv.render(r"\frac{1}{")
        check("preview 坏输入返回 None", [] if bad is None else [f"返回了 {bad}"])


# ---------------------------------------------------------------- d) tray 壳

def smoke_tray() -> None:
    print("\n== d) tray_app 子进程（SNIPEQ_FORCE_TK=1 降级口径）：--once-image + --smoke 35s")
    img = str(IMAGES[0])
    t0 = time.perf_counter()
    import os
    env = dict(os.environ, PYTHONIOENCODING="utf-8", SNIPEQ_FORCE_TK="1")
    p = subprocess.run(
        [PY, str(SRC / "tray_app.py"), "--once-image", img, "--smoke", "35"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(SRC), timeout=240,
    )
    el = time.perf_counter() - t0
    out = (p.stdout or "") + (p.stderr or "")
    problems: list[str] = []
    if "Traceback" in out:
        problems.append("输出含 Traceback")
    if "SMOKE OK" not in out:
        problems.append(f"缺 SMOKE OK（rc={p.returncode}）")
    if not re.search(r"直写剪贴板|已复制 Word 公式", out):
        problems.append("未见降级直写证据（直写剪贴板/已复制）")
    if not re.search(r"全局热键已注册|热键注册失败", out):
        problems.append("未见热键注册尝试")
    print("---- tray_app 输出（尾部）----")
    for line in out.splitlines()[-20:]:
        print("   ", line)
    print(f"  （进程运行 {el:.0f}s，rc={p.returncode}）")
    check("tray_app 35s 冒烟", problems)


def main() -> int:
    skip_tray = "--skip-tray" in sys.argv
    if not IMAGES:
        print("缺测试图（先跑 gen_test_images.py）")
        return 1
    smoke_capture()
    smoke_pipeline()
    smoke_preview()
    if skip_tray:
        print("（--skip-tray 跳过 d）")
    else:
        smoke_tray()

    print("\n==========================")
    if FAILURES:
        print(f"M2 SMOKE FAIL: {len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("M2 SMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
