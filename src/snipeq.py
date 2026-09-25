"""snipeq.py — SnipEq CLI 入口（M1 骨架）。

链路：取图 → PP-FormulaNet_plus-S 识别(CPU) → 归一化 → latex2mathml → MathML
      → 剪贴板三格式写入（CF_HTML / CF_UNICODETEXT($latex$) / MathML）。

用法：
  py snipeq.py image <path>        识别指定图片文件，成功后写剪贴板
  py snipeq.py clip                读系统剪贴板里的图片（Win+Shift+S 截屏后）
  py snipeq.py ... --json          只输出 {latex, mathml_len, timings}，不写剪贴板

退出码：0 成功；2 识别失败/低质量；3 转换或剪贴板失败；4 剪贴板无图片。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize import normalize  # noqa: E402
from version import __version__  # noqa: E402


def _grab_clipboard_image() -> str:
    """Pillow 读剪贴板图片，存临时 PNG，返回路径。无图抛 RuntimeError。"""
    from PIL import ImageGrab

    img = ImageGrab.grabclipboard()
    if img is None:
        raise RuntimeError("剪贴板中没有图片（先用 Win+Shift+S 截屏或复制图片）")
    if isinstance(img, list):  # 资源管理器复制的文件列表
        pics = [p for p in img if Path(p).suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff")]
        if not pics:
            raise RuntimeError(f"剪贴板文件列表中无图片: {img}")
        return str(pics[0])
    fd = str(Path(tempfile.gettempdir()) / "snipeq_clip.png")
    img.save(fd)
    return fd


def run(image_source: "tuple[str, str | None]", as_json: bool) -> int:
    """全链路执行。image_source: ('path', p) 或 ('clip', None)。返回退出码。"""
    timings: dict[str, float] = {}
    kind, p = image_source

    # ---- 取图（预处理段：剪贴板取图/落盘计时）
    t0 = time.perf_counter()
    if kind == "clip":
        try:
            image_path = _grab_clipboard_image()
        except RuntimeError as e:
            print(f"[clip] {e}", file=sys.stderr)
            return 4
    else:
        image_path = str(p)
    timings["preprocess"] = time.perf_counter() - t0

    # ---- 识别（含一次性模型加载，加载耗时单独报）
    from recognizer import FormulaRecognizer, RecognitionError

    try:
        rec = FormulaRecognizer()
        raw_latex, infer_s = rec.recognize(image_path)
        timings["model_load"] = rec.load_seconds or 0.0
        timings["inference"] = infer_s
    except RecognitionError as e:
        print(f"[recognize] 识别失败: {e}", file=sys.stderr)
        return 2
    except Exception:
        print("[recognize] 识别异常:", file=sys.stderr)
        traceback.print_exc()
        return 2

    # ---- 归一化 + LaTeX→MathML 转换
    t0 = time.perf_counter()
    from mathml import latex_to_mathml
    from clipboard_writer import ClipboardError, write_clipboard_formula

    try:
        latex, applied_rules = normalize(raw_latex)
        mathml = latex_to_mathml(latex)
    except Exception as e:
        print(f"[convert] 归一化/转换失败: {e}", file=sys.stderr)
        return 3
    timings["normalize_convert"] = time.perf_counter() - t0

    if as_json:
        print(json.dumps({
            "latex": latex,
            "mathml_len": len(mathml),
            "timings": {k: round(v, 3) for k, v in timings.items()},
        }, ensure_ascii=False, indent=2))
        return 0

    # ---- 剪贴板三格式写入
    t0 = time.perf_counter()
    try:
        write_clipboard_formula(latex)
    except ClipboardError as e:
        print(f"[clipboard] 写入失败: {e}", file=sys.stderr)
        return 3
    timings["clipboard"] = time.perf_counter() - t0

    print(f"识别: {latex}")
    if applied_rules:
        print(f"归一化规则: {','.join(applied_rules)}")
    print("  ".join(f"{k}={v:.3f}s" for k, v in timings.items()))
    print("已写入剪贴板三格式（CF_HTML / CF_UNICODETEXT / MathML），到 Word/WPS 中 Ctrl+V。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="snipeq", description="公式截图 → 原生 Word 公式（剪贴板直贴）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_image = sub.add_parser("image", help="识别指定图片文件")
    ap_image.add_argument("path")
    ap_image.add_argument("--json", action="store_true", help="只输出 JSON，不写剪贴板")
    ap_clip = sub.add_parser("clip", help="读剪贴板图片识别（Win+Shift+S 后）")
    ap_clip.add_argument("--json", action="store_true", help="只输出 JSON，不写剪贴板")
    args = ap.parse_args(argv)

    if args.cmd == "image":
        return run(("path", args.path), args.json)
    return run(("clip", None), args.json)


if __name__ == "__main__":
    sys.exit(main())
