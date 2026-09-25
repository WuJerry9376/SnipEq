"""mathml.py — LaTeX → Presentation MathML 转换层。

vendor 自 spike/clipboard/clipboard_formula.py 的 latex_to_mathml 部分，M1 转正。
关键保留：**aligned/array 对齐符 `&` 实体转义修复** —— latex2mathml 3.81 对
对齐符输出裸 `<mi>&</mi>`（非法 XML，Word 端 MathML 解析必失败），此处把
未构成实体的裸 `&` 一律转义为 `&amp;`（spike selftest 22/22 通过的实现）。
"""
from __future__ import annotations

import re

import latex2mathml.converter

MATHML_NS = "http://www.w3.org/1998/Math/MathML"

_BAD_AMP_RE = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|amp|lt|gt|quot|apos);)")


def latex_to_mathml(latex: str) -> str:
    """LaTeX → 裸 Presentation MathML（保证根 <math> 带 xmlns、XML 转义修复）。"""
    mathml = latex2mathml.converter.convert(latex)
    # latex2mathml 3.x 已自带 xmlns；防御性补全（旧版/异常输出）
    m = re.match(r"(<math\b[^>]*>)", mathml)
    if m and "xmlns" not in m.group(1):
        mathml = m.group(1).replace("<math", f'<math xmlns="{MATHML_NS}"', 1) + mathml[m.end():]
    # 归一化修复: latex2mathml 对 aligned 对齐符 & 输出裸 <mi>&</mi>（非法 XML，
    # Word 端 MathML 解析必失败）。把未构成实体的 & 一律转义为 &amp;。
    mathml = _BAD_AMP_RE.sub("&amp;", mathml)
    return mathml
