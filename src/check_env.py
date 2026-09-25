"""check_env.py — SnipEq 便携包首次运行自检（办公机免命令行：双击 检查环境.bat，
或 python check_env.py）。逐项打印 [OK]/[FAIL]/[WARN]，全必要项通过则 exit 0。

检查：解释器/tkinter/PIL/latex2mathml/lxml/pystray/PySide6/paddle/paddlex/
模型文件/预览链路(node 或 katex_assets + Edge)/热键注册试探/Word 探测/
剪贴板三格式自测/%APPDATA% 可写。只读诊断，唯一副作用是一次剪贴板写入。
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RESULT: list[tuple[str, str]] = []  # (level, msg)


def ok(msg: str) -> None:
    print(f"[OK]   {msg}"); RESULT.append(("OK", msg))


def warn(msg: str) -> None:
    print(f"[WARN] {msg}"); RESULT.append(("WARN", msg))


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}"); RESULT.append(("FAIL", msg))


def main() -> int:
    print(f"=== SnipEq 环境自检 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"python: {sys.version.split()[0]} @ {sys.executable}")
    ok("python 3.12 运行中") if sys.version_info[:2] == (3, 12) else warn("非 3.12 解释器")

    # ---- 基础库
    for mod, need in (("tkinter", True), ("PIL", True), ("latex2mathml", True),
                      ("lxml", True), ("pystray", True)):
        try:
            __import__(mod)
            ok(f"import {mod}")
        except Exception as e:  # noqa: BLE001
            (fail if need else warn)(f"import {mod} 失败: {e}")

    try:
        import PySide6
        from PySide6 import QtWidgets  # noqa: F401
        ok(f"import PySide6 {PySide6.__version__}（浮卡组件）")
    except Exception as e:  # noqa: BLE001
        warn(f"PySide6 不可用: {e} —— 浮卡将降级为直写剪贴板模式")

    # ---- paddle / paddlex（首次 import 较慢）
    t0 = time.perf_counter()
    try:
        import paddle
        ok(f"import paddle {paddle.__version__}（{time.perf_counter()-t0:.1f}s）")
    except Exception as e:  # noqa: BLE001
        fail(f"import paddle 失败: {e}")

    t0 = time.perf_counter()
    try:
        import paddlex
        ok(f"import paddlex {getattr(paddlex, '__version__', '?')}（{time.perf_counter()-t0:.1f}s）")
    except Exception as e:  # noqa: BLE001
        fail(f"import paddlex 失败: {e}")

    # ---- 模型与 pipeline（禁止目标机联网下载，必须包内齐全）
    yaml_p = _HERE / "formula_pipeline_S.yaml"
    (ok if yaml_p.is_file() else fail)(f"pipeline 配置 {'存在' if yaml_p.is_file() else '缺失'}: {yaml_p}")
    try:
        import recognizer
        cache = recognizer.DEFAULT_CACHE_HOME
        model = Path(cache) / "official_models" / "PP-FormulaNet_plus-S"
        if model.is_dir():
            n = sum(f.stat().st_size for f in model.rglob("*") if f.is_file())
            ok(f"模型权重就位: {model}（{n/1e6:.0f} MB，cache={cache}）")
        else:
            fail(f"模型缺失: {model} —— 包不完整，请重新解压")
    except Exception as e:  # noqa: BLE001
        fail(f"recognizer 检查异常: {e}")

    # ---- 预览链路（两条都探）
    try:
        import preview
        node = __import__("shutil").which("node")
        assets = preview.KATEX_ASSETS
        if node:
            ok(f"node 可用（KaTeX 快速路线）: {node}")
        elif (assets / preview.KATEX_JS_NAME).is_file():
            ok("无 node，但 katex_assets 就位 → 浏览器内渲染路线")
        else:
            warn("无 node 且无 katex_assets → 预览降级为卡片显示 LaTeX 文本（识别不受影响）")
        edge = preview.find_edge()
        (ok if edge else warn)(f"Edge: {edge or '未找到（预览不可用，识别不受影响）'}")
    except Exception as e:  # noqa: BLE001
        warn(f"preview 探测异常: {e}")

    # ---- 热键注册试探（默认 Ctrl+Alt+A，成功即释放）
    try:
        import capture
        hm = capture.HotkeyManager("ctrl+alt+a", lambda: None)
        hm.start(); hm.stop()
        ok("全局热键 Ctrl+Alt+A 注册试探成功（若其他程序占用会 FAIL）")
    except Exception as e:  # noqa: BLE001
        fail(f"热键注册失败: {e} —— 可在 %APPDATA%\\SnipEq\\settings.json 改 hotkey 后重启")

    # ---- Word 探测（只影响粘贴体验，不影响运行）
    try:
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
            ok("检测到 Microsoft Word（Word.Application ProgID）")
        except OSError:
            warn("未检测到 Word —— 粘贴验证请在装有 Word 的账号会话进行")
    except Exception as e:  # noqa: BLE001
        warn(f"Word 探测异常: {e}")

    # ---- 剪贴板三格式自测（写入 → v2 三态读回；会覆盖当前剪贴板一次）
    try:
        from clipboard_writer import selftest_case
        probs = selftest_case({"latex": r"\frac{a}{b}"})
        (ok if not probs else fail)(f"剪贴板三格式自测 {'PASS' if not probs else probs}")
    except Exception as e:  # noqa: BLE001
        fail(f"剪贴板自测异常: {e}")

    # ---- %APPDATA% 可写（settings/history 落点）
    try:
        from settings import Settings
        s = Settings()
        s.save()
        ok(f"settings 落点可写: {s.path}")
    except Exception as e:  # noqa: BLE001
        fail(f"%APPDATA%\\SnipEq 不可写: {e}")

    fails = [m for lv, m in RESULT if lv == "FAIL"]
    print("\n=== 结论:", "环境就绪，双击『SnipEq启动.vbs』开始使用"
          if not fails else f"{len(fails)} 项必要检查未通过，见上方 [FAIL]")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
