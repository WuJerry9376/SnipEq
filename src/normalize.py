"""normalize.py — LaTeX 方言归一化规则引擎（N1–N12）。

vendor 自 spike/convert/normalize.py，M1 转正（N1–N9 内容与 spike 一致，验证依据：
spike/convert/report.md，归一化后两级结构 latex2mathml 通过率 99%）。
M1 增补：N10（环境末行尾随 \\\\ 剥离）、N11（行尾悬挂 `{` 补闭），系 spike 未覆盖的
PP-FormulaNet cases 真实输出形态，见各函数 docstring 证据。
M3b 增补：N12（\\boldsymbol→\\mathbf 并轨，见函数 docstring 的取舍说明）。
normalize(latex) 返回 (改写后 latex, 触发规则 id 列表)。
"""
import re

RULES_ORDER = ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "N9", "N10", "N11", "N12"]


def _n1_strip_wrappers(s):
    """N1: 剥离定界包裹 $$..$$ / $..$ / \\[..\\] / \\(..\\)。"""
    changed = False
    t = s.strip()
    for pat in (r"^\$\$(.*)\$\$$", r"^\$([^$].*)\$$", r"^\\\[(.*)\\\]$", r"^\\\((.*)\\\)$"):
        m = re.match(pat, t, flags=re.S)
        if m:
            t = m.group(1).strip()
            changed = True
            break
    return t, changed


def _n2_strip_numbering(s):
    r"""N2: 删除编号控制序列 \nonumber / \label{..} / \tag{..}。"""
    t = re.sub(r"\\nonumber\s*\.?", " ", s)
    t = re.sub(r"\\(label|tag)\s*\{[^}]*\}", " ", t)
    return t, t != s


def _n3_frac_alias(s):
    r"""N3: \dfrac/\tfrac/\cfrac -> \frac。"""
    t = re.sub(r"\\[dtc]frac", r"\\frac", s)
    return t, t != s


_ALIGNED_RE = re.compile(
    r"\\begin\{aligned\}(.*?)\\end\{aligned\}", flags=re.S)


def _n4_aligned_to_array(s):
    """N4: aligned 环境 -> array{rl..}（latex2mathml 对 aligned 不产 mtable，行结构静默丢失）。"""
    def repl(m):
        body = m.group(1)
        max_amp = 0
        for row in re.split(r"\\\\", body):
            max_amp = max(max_amp, row.count("&"))
        cols = "rl" if max_amp <= 1 else "r" + "l" * max_amp
        return "\\begin{array}{%s}%s\\end{array}" % (cols, body)
    t = _ALIGNED_RE.sub(repl, s)
    return t, t != s


def _n5_fill_empty_cells(s):
    """N5: array/cases 环境内空单元格补 {}（&& / 行首& / 行尾&）。仅处理 array 环境（N4 之后）。"""
    env_re = re.compile(r"(\\begin\{array\}(?:\{[^}]*\})?)(.*?)(\\end\{array\})", flags=re.S)

    def fix_body(m):
        head, body, tail = m.group(1), m.group(2), m.group(3)
        rows = re.split(r"(\\\\)", body)
        out = []
        for part in rows:
            if part == "\\\\":
                out.append(part)
                continue
            p = part
            p = re.sub(r"&&", "& {}&", p)
            p = re.sub(r"^(\s*)&", "\\1{}&", p)   # 行首 & -> 空单元格
            p = re.sub(r"&(\s*)$", "&{}\\1", p)   # 行尾 & -> 空单元格
            out.append(p)
        return head + "".join(out) + tail

    t = env_re.sub(fix_body, s)
    return t, t != s


def _n6_balance_fences(s):
    """N6: 删除孤儿 \\left / \\right（数量不平衡时去掉多余一侧）。"""
    n_left = len(re.findall(r"\\left(?=[\s(\[{|.\\])", s))
    n_right = len(re.findall(r"\\right(?=[\s)\]}|.\\])", s))
    t = s
    if n_right > n_left:
        # 从尾部删多余的 \right X
        excess = n_right - n_left
        toks = list(re.finditer(r"\\right\s*([.\\]?[^\s])", t))
        for m in reversed(toks[-excess:] if toks else []):
            t = t[:m.start()] + " " + m.group(1).replace(".", "") + t[m.end():]
    elif n_left > n_right:
        excess = n_left - n_right
        toks = list(re.finditer(r"\\left\s*([.\\]?[^\s])", t))
        for m in reversed(toks[-excess:] if toks else []):
            t = t[:m.start()] + " " + m.group(1).replace(".", "") + t[m.end():]
    return t, t != s


def _n7_ce_policy(s):
    r"""N7: mhchem \ce{...} 不做还原（保留原样交给回收/L2）。占位规则，不救回。"""
    return s, False


def _n8_rcases_to_cases(s):
    r"""N8: rcases -> cases（latex2mathml 不支持 rcases，& 泄漏产生非法 XML）。牺牲括号方向。"""
    t = s.replace(r"\begin{rcases}", r"\begin{cases}").replace(r"\end{rcases}", r"\end{cases}")
    return t, t != s


_FENCED_ENVS = {
    "pmatrix": (r"\left(", r"\right)"),
    "bmatrix": (r"\left[", r"\right]"),
    "vmatrix": (r"\left|", r"\right|"),
    "Bmatrix": (r"\left\{", r"\right\}"),
    "Vmatrix": (r"\left\|", r"\right\|"),
}
_SCRIPT = r"(?:[\^_](?:\{[^{}]*\}|\S))"
_FENCED_SCRIPT_RE = re.compile(
    r"\\begin\{(pmatrix|bmatrix|vmatrix|Bmatrix|Vmatrix)\}(.*?)\\end\{\1\}(%s)" % _SCRIPT,
    flags=re.S,
)


def _n9_matrix_script_wrap(s):
    r"""N9: 带上下标的围栏矩阵 \begin{pmatrix}..\end{pmatrix}^X -> \left(\begin{matrix}..\end{matrix}\right)^X。

    latex2mathml 对围栏环境+script 产出非法 msup（4 子元素，定界符未包 mrow），mathml2omml 崩溃。
    """
    def repl(m):
        env, body, script = m.group(1), m.group(2), m.group(3)
        open_d, close_d = _FENCED_ENVS[env]
        return "%s\\begin{matrix}%s\\end{matrix}%s%s" % (open_d, body, close_d, script)
    t = _FENCED_SCRIPT_RE.sub(repl, s)
    return t, t != s


_ENV_BODY_RE = re.compile(
    r"(\\begin\{(?:array|cases|aligned)\}(?:\{[^}]*\})?)(.*?)(\\end\{(?:array|cases|aligned)\})",
    flags=re.S,
)


def _n10_strip_trailing_rowsep(s):
    r"""N10: 环境末行尾随 ``\\`` 剥离。

    M1 新增（非 spike vendor 范围）。证据: src venv latex2mathml 3.81.1 实测——
    PP-FormulaNet 对 cases 图输出 `\end{cases}` 前带 `\\ `，convert 抛
    ExtraLeftOrMissingRightError（E 用例复现）；剥离后即可转换。
    """
    def fix_body(m):
        head, body, tail = m.group(1), m.group(2), m.group(3)
        stripped = re.sub(r"(\\\\\s*)+$", "", body)
        return head + stripped + tail
    t = _ENV_BODY_RE.sub(fix_body, s)
    return t, t != s


def _n11_close_braces_at_rowsep(s):
    r"""N11: 行尾悬挂 `{` 补闭（花括号配平修复）。

    M1 新增（非 spike vendor 范围）。证据: PP-FormulaNet 对 cases 图真实输出
    `{x\geq0,\\`（单元格开括号未闭合即换行），latex2mathml 3.81.1 抛
    ExtraLeftOrMissingRightError。逐字符扫描（\{ \} 字面括号不计深度），
    遇 `\\` 时把当前深度未闭合的 `{` 全部补 `}`。
    """
    depth = 0
    out = []
    i, n, changed = 0, len(s), False
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "\\":  # 行分隔 \\
                if depth > 0:
                    out.append("}" * depth)
                    depth = 0
                    changed = True
                out.append("\\\\")
                i += 2
                continue
            if nxt in "{}":  # \{ \} 字面括号，不参与配平
                out.append(s[i:i + 2])
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
        out.append(ch)
        i += 1
    return "".join(out), changed


_BOLDSYMBOL_RE = re.compile(r"\\boldsymbol(?![A-Za-z])")


def _n12_boldsymbol_to_mathbf(s):
    r"""N12: \boldsymbol → \mathbf（加粗并轨近似）。

    M3b 新增（依据 spike/model/report_v2.md 失败模式 C，real138 边缘案例）。
    实测说明（latex2mathml 3.81.1）：`\boldsymbol{\theta}` 基本式可转换、产
    `<mi mathvariant="bold-italic">` 不丢符号；风险在下游——Word/mml2omml 对
    bold-italic 变体渲染不稳（M3a 决策：输入侧统一并轨）。取舍：bold-italic →
    bold **直立**，牺牲斜体保"加粗向量/矩阵记号"语义；`\mathbf{...}` 参数结构
    与原样保留，只改名不重构。长宏防误触：`\boldsymbolx` 类不匹配。
    回归样例（real138 GT 只读引用）：
      \widetilde {\boldsymbol {\theta}} = h _ {\phi} (\boldsymbol {\zeta}) ...
      → \widetilde {\mathbf {\theta}} = h _ {\phi} (\mathbf {\zeta}) ...
    """
    t = _BOLDSYMBOL_RE.sub(r"\\mathbf", s)  # 替换串里 \\ = 字面反斜杠
    return t, t != s


_DISPATCH = {
    "N1": _n1_strip_wrappers,
    "N2": _n2_strip_numbering,
    "N3": _n3_frac_alias,
    "N4": _n4_aligned_to_array,
    "N5": _n5_fill_empty_cells,
    "N6": _n6_balance_fences,
    "N7": _n7_ce_policy,
    "N8": _n8_rcases_to_cases,
    "N9": _n9_matrix_script_wrap,
    "N10": _n10_strip_trailing_rowsep,
    "N11": _n11_close_braces_at_rowsep,
    "N12": _n12_boldsymbol_to_mathbf,
}


def normalize(latex):
    t = latex
    applied = []
    for rid in RULES_ORDER:
        t_new, hit = _DISPATCH[rid](t)
        if hit:
            applied.append(rid)
        t = t_new
    return t, applied
