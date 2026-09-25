r"""run_m3a_fixes.py — M3a 回流小修回归（复读熔断 + N12 boldsymbol 并轨）。

1) 熔断正例 ×3：real060 真实形态构造串 / 全周期重复串 / 低 token 去重比串（B 分支）；
   负例：4 张 manifest 真式、构造合法长式（>300 且 token 多样）、N11 悬空 { 短例；
2) recognize() 接线：FakePipe 注入触发串 → RecognitionError 含"复读循环"文案；
   正常式不误伤；
3) N12：\boldsymbol 转换正确（含 M3a real138 GT 真实串）、既有 \mathbf 不误触、
   \boldsymbolx 长宏防误触、转换后 MathML 符号不丢（theta 保留 + mathvariant=bold）。

运行：& ..\\.venv\\Scripts\\python.exe run_m3a_fixes.py
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from recognizer import (  # noqa: E402
    FormulaRecognizer, RecognitionError, _looks_repetitive,
)
from normalize import normalize  # noqa: E402
from mathml import latex_to_mathml  # noqa: E402

FAILURES: list[str] = []


def check(name: str, problems: list[str]) -> None:
    if problems:
        FAILURES.append(name)
        print(f"  [FAIL] {name}")
        for p in problems:
            print(f"         !! {p}")
    else:
        print(f"  [PASS] {name}")


# ---------------------------------------------------------------- 1) 熔断判定

# real060 pred 真实形态（spike/model/results_v2.json rows，只读引用其结构）
LOOP_REAL060 = "\\begin array}{{c}{\\" + ("\\ " * 150) + "}"
LOOP_FRAC = r"\frac{1}{2}" * 40  # 全周期重复
_rnd = random.Random(11)
LOOP_TOKENS = "".join(_rnd.choice("abcdefgh") + " " for _ in range(180))  # 360 字符：窗口多样(A 漏)但 token 词表仅 8(B 命中)

NEG_MANIFEST = json.loads((HERE / "images" / "manifest.json").read_text(encoding="utf-8"))
# 合法长式：8 行 array，逐行编号不同（>300 字符、token 多样）
LONG_LEGIT = ("\\begin{array}{ll}" +
              "\\\\".join(f"a_{{{i}}} &= {i} x^{{{i}}} + b_{{{i}}}$ & i={i}"
                          for i in range(8)) +
              "\\end{array}")
N11_CASE = r"f(x)=\left\{\begin{cases}{x^{2}, & x\geq0,\\ {-x, & x<0."  # M1 N11 证据形


def t_breaker() -> None:
    print("\n== 1) 熔断判定 _looks_repetitive")
    problems = []
    hits = {"real060形态": LOOP_REAL060, "周期重复": LOOP_FRAC, "token低去重": LOOP_TOKENS}
    for tag, s in hits.items():
        reason = _looks_repetitive(s)
        if reason is None:
            problems.append(f"{tag} 未触发（len={len(s)}）")
        else:
            print(f"    触发[{tag}]: {reason[:70]}")
    # B 分支专项：随机字符串 A 窗口应漏网、由"去重比"分支兜住
    rb = _looks_repetitive(LOOP_TOKENS)
    if not rb or "token" not in rb:
        problems.append(f"B 分支未独立命中: {rb}")
    else:
        print(f"    B 分支专项: {rb[:60]}")
    for tag, s in {**{f"manifest:{k}": v for k, v in NEG_MANIFEST.items()},
                   "合法长式": LONG_LEGIT, "N11短例": N11_CASE}.items():
        reason = _looks_repetitive(s)
        if reason is not None:
            problems.append(f"误伤 {tag}: {reason}")
    check("熔断判定（3 正例触发 / 6 负例不误伤）", problems)


# ---------------------------------------------------------------- 2) recognize 接线

class _FakePipe:
    """免 paddlex 加载的假 pipeline：predict 恒返回给定公式串。"""

    def __init__(self, formula: str):
        self.formula = formula

    def predict(self, image, batch_size=1):
        # 真实 PaddleX 口径：每张图 yield 一个结果 dict
        yield {"formula_res_list": [{"rec_formula": self.formula}]}


def t_recognize_hook() -> None:
    print("\n== 2) recognize() 低质量报错路径")
    img = str(HERE / "images" / "frac_sum.png")
    problems = []
    r = FormulaRecognizer()
    r._pipe = _FakePipe(LOOP_REAL060)
    try:
        r.recognize(img)
        problems.append("复读串未抛 RecognitionError")
    except RecognitionError as e:
        msg = str(e)
        if "复读循环" not in msg:
            problems.append(f"文案缺『复读循环』: {msg}")
        if "裁剪" not in msg or "v1.1" not in msg:
            problems.append(f"文案缺处置建议（裁剪/v1.1 升档）: {msg}")
        else:
            print(f"    报错文案: {msg[:80]}…")
    r2 = FormulaRecognizer()
    r2._pipe = _FakePipe(NEG_MANIFEST["frac_sum"])
    try:
        latex, _ = r2.recognize(img)
        if "frac" not in latex:
            problems.append(f"正常式返回异常: {latex}")
    except RecognitionError as e:
        problems.append(f"正常式被误熔断: {e}")
    check("recognize 熔断接线（触发+放行）", problems)


# ---------------------------------------------------------------- 3) N12

REAL138_GT = (r"\widetilde {\boldsymbol {\theta}} = h _ {\phi} (\boldsymbol {\zeta})"
              r" \quad \text {with} \quad \boldsymbol {\zeta} \sim p (\boldsymbol {\zeta})"
              r" \tag {19}")


def t_n12() -> None:
    print("\n== 3) N12 \\boldsymbol→\\mathbf")
    problems = []
    # 基本转换（两种参数形态）
    out, applied = normalize(r"\boldsymbol{\alpha}+\boldsymbol {\Gamma}")
    if out != r"\mathbf{\alpha}+\mathbf {\Gamma}":
        problems.append(f"转换错误: {out}")
    if "N12" not in applied:
        problems.append(f"applied 缺 N12: {applied}")
    # real138 GT：N12 命中、无残留、tag 由 N2 剥、MathML 符号不丢
    out138, ap138 = normalize(REAL138_GT)
    if "boldsymbol" in out138:
        problems.append(f"real138 残留 boldsymbol: {out138}")
    for rid in ("N2", "N12"):
        if rid not in ap138:
            problems.append(f"real138 applied 缺 {rid}: {ap138}")
    if "tag" in out138 or "19" in out138:
        problems.append(f"real138 \\tag 未剥净: {out138}")
    try:
        mml = latex_to_mathml(out138)
        if "#x003B8" not in mml and "\u03b8" not in mml:
            problems.append(f"MathML theta 丢失: {mml[:120]}")
        if 'mathvariant="bold"' not in mml:
            problems.append(f"MathML 无 bold 变体: {mml[:120]}")
        print(f"    real138 → {out138[:64]}…  ({','.join(ap138)})")
    except Exception as e:  # noqa: BLE001
        problems.append(f"real138 MathML 转换失败: {e}")
    # 不误触既有 \mathbf / \text；长宏防误触
    src_noop = r"\mathbf{E}=\mathbf{J}"
    out2, ap2 = normalize(src_noop)
    if out2 != src_noop or "N12" in ap2:
        problems.append(f"mathbf 误触: {out2} {ap2}")
    src_guard = r"\boldsymbolx{a}"
    out3, ap3 = normalize(src_guard)
    if out3 != src_guard:
        problems.append(f"长宏 \\boldsymbolx 被改写: {out3}")
    check("N12 转换/real138 回归/不误触", problems)


def main() -> int:
    t_breaker()
    t_recognize_hook()
    t_n12()
    print("\n==========================")
    if FAILURES:
        print(f"M3A FIXES FAIL: {len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("M3A FIXES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
