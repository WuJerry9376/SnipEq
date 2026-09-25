"""run_e2e.py — SnipEq M1 端到端回归：每张测试图走全链路并读回校验。

链路：图片 → PP-FormulaNet_plus-S 识别 → 归一化 → latex2mathml → 剪贴板三格式写入
校验（本机无 Word，端到端口径 = 写入后读回）：
  ① CF_HTML 偏移独立重算（verify_cf_html，UTF-8 字节口径）
  ② MathML 自定义格式可读回、XML 可解析、与写入一致
  ③ CF_UNICODETEXT == $归一化latex$
全部通过打印 "E2E PASS"，退出码 0；否则列出问题、退出码 1。
另按任务要求打印每张图 预处理/推理/归一化+转换/剪贴板 各段耗时与推理 P50。

运行：& ..\\.venv\\Scripts\\python.exe run_e2e.py
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))

from clipboard_writer import (  # noqa: E402
    read_back, verify_cf_html, verify_mathml_xml, write_clipboard_formula,
)
from mathml import latex_to_mathml  # noqa: E402
from normalize import normalize  # noqa: E402
from recognizer import FormulaRecognizer, RecognitionError  # noqa: E402

IMAGES = sorted((HERE / "images").glob("*.png"))


def run_one(rec: FormulaRecognizer, img: Path) -> tuple[list[str], dict[str, float], str]:
    problems: list[str] = []
    timings: dict[str, float] = {}

    # 预处理（M1 口径：文件存在性/大小检查；PaddleX 内部 resize 计入推理段）
    t0 = time.perf_counter()
    assert img.is_file() and img.stat().st_size > 0
    timings["preprocess"] = time.perf_counter() - t0

    # 识别
    try:
        raw_latex, infer_s = rec.recognize(str(img))
    except RecognitionError as e:
        return [f"recognize: {e}"], timings, ""
    timings["inference"] = infer_s

    # 归一化 + 转换
    t0 = time.perf_counter()
    latex, applied = normalize(raw_latex)
    try:
        mathml = latex_to_mathml(latex)
    except Exception as e:  # noqa: BLE001
        return [f"latex_to_mathml: {e}"], timings, latex
    timings["normalize_convert"] = time.perf_counter() - t0

    # 剪贴板写入 + v2 三态读回校验
    t0 = time.perf_counter()
    info = write_clipboard_formula(latex)
    timings["clipboard"] = time.perf_counter() - t0
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
    else:
        got = r["data"].decode("utf-16-le").rstrip("\x00")
        if got != f"${latex}$":
            problems.append(f"CF_UNICODETEXT 不匹配: {got!r}")

    r = back["mathml"]
    if r["state"] != "ok":
        problems.append(f"MathML 格式读回 {r['state']}: {r['detail']}")
    elif r["data"].decode("utf-8") != mathml:
        problems.append("MathML 格式与写入不一致")
    else:
        problems += [f"[mathml] {p}" for p in verify_mathml_xml(r["data"].decode("utf-8"))]

    return problems, timings, latex


def main() -> int:
    if not IMAGES:
        print("no test images under images/ (先运行 gen_test_images.py)")
        return 1
    rec = FormulaRecognizer()
    infer_times: list[float] = []
    failed = 0
    for img in IMAGES:
        problems, timings, latex = run_one(rec, img)
        status = "PASS" if not problems else "FAIL"
        print(f"\n=== [{img.name}] {status}")
        print(f"    latex: {latex}")
        print("    耗时:  " + "  ".join(f"{k}={v:.3f}s" for k, v in timings.items())
              + (f"  (load={rec.load_seconds:.1f}s 一次性)" if rec.load_seconds else ""))
        infer_times.append(timings.get("inference", float("nan")))
        for p in problems:
            print(f"    !! {p}")
        failed += bool(problems)

    print("\n---- 推理耗时（含 PaddleX 内部预处理，冷启动第 1 张偏高）----")
    for img, t in zip(IMAGES, infer_times):
        print(f"  {img.name:16s} {t:.3f}s")
    if len(infer_times) >= 2:
        print(f"  P50(全量)   {statistics.median(infer_times):.3f}s")
        print(f"  P50(去冷启) {statistics.median(infer_times[1:]):.3f}s")

    if failed:
        print(f"\nE2E FAIL ({failed}/{len(IMAGES)})")
        return 1
    print(f"\nE2E PASS ({len(IMAGES)}/{len(IMAGES)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
