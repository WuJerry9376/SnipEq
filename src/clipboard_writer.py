"""clipboard_writer.py — 剪贴板三格式写入/读回/校验（ctypes，零 pywin32 依赖）。

vendor 自 spike/clipboard/clipboard_formula.py，M1 转正；**M2 同步 v2 加固**：
  - ctypes.WinDLL(..., use_last_error=True)：所有 Win32 失败携带 GetLastError
    数值 + FormatMessage 文本（真机 selftest 20/22 瞬时故障的定位盲区根因）；
  - OpenClipboard 指数退避重试（20ms 起 ×2、单次上限 500ms、共 10 次 ≈ 最大 ~4.5s）；
  - 三格式隔离写入：单格式失败不中断其余，记入 warnings —— 仅 CF_HTML 失败致命
    （抛 ClipboardError），CF_UNICODETEXT / MathML 失败为降级损失；
  - 读回三态区分：not_registered / not_on_clipboard / get_failed / lock_failed /
    empty_data / ok（read_back 返回各格式 state dict，cf_html 缺失整体重试 3 轮）。

写入格式：
  ① "HTML Format" (CF_HTML): UTF-8 字节偏移严格正确的头 + 内嵌裸 Presentation MathML
  ② CF_UNICODETEXT: ``$latex$`` 定界文本（新版 M365 自动转换 / Alt+= 手动降级）
  ③ "MathML" 自定义格式: 裸 Presentation MathML（选择性粘贴第三格式路径）
已知风险点：CF_HTML 四个偏移必须按 **UTF-8 字节**（非字符数）计；写入端与
verify_cf_html 校验端各自独立计算一次（回归口径）。
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes

from mathml import MATHML_NS, latex_to_mathml

# ---------------------------------------------------------------- Win32 常量

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

FRAG_START = "<!--StartFragment-->"
FRAG_END = "<!--EndFragment-->"

# OpenClipboard 指数退避参数（失败时打印出来供现场诊断）
OPEN_RETRY_MAX = 10
OPEN_RETRY_BASE_S = 0.020
OPEN_RETRY_CAP_S = 0.500

# ---------------------------------------------------------------- ctypes 声明
# use_last_error=True: 必须，否则 GetLastError 取到的是无关线程错误码

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.CountClipboardFormats.restype = ctypes.c_int
# 句柄必须以 c_void_p 承接，64 位下用默认 int 会截断
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
_user32.SetClipboardData.restype = ctypes.c_void_p

_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
_kernel32.GlobalFree.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
_kernel32.GlobalSize.restype = ctypes.c_size_t
_kernel32.FormatMessageW.argtypes = [
    wintypes.UINT, ctypes.c_void_p, wintypes.UINT, wintypes.DWORD,
    wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
]
_kernel32.FormatMessageW.restype = wintypes.DWORD


class ClipboardError(RuntimeError):
    pass


def err_text(code: int) -> str:
    """GetLastError 数值 → 'code=N (FormatMessage 文本)'。"""
    buf = ctypes.create_unicode_buffer(512)
    # FORMAT_MESSAGE_FROM_SYSTEM(0x1000) | FORMAT_MESSAGE_IGNORE_INSERTS(0x200)
    n = _kernel32.FormatMessageW(0x1200, None, code, 0, buf, 512, None)
    text = (buf.value or "").strip().rstrip(".") if n else ""
    return f"code={code}" + (f" ({text})" if text else "")


def last_err_text(func: str) -> str:
    """取当前线程 GetLastError 并格式化（必须在失败调用后立刻取，后续 API 会覆盖）。"""
    return f"{func} failed: {err_text(ctypes.get_last_error())}"


# ---------------------------------------------------------------- 格式注册

def _format_id(name: str) -> int:
    fmt = _user32.RegisterClipboardFormatW(name)
    if not fmt:
        raise ClipboardError(last_err_text(f"RegisterClipboardFormatW({name!r})"))
    return fmt


_CF_HTML_ID: int | None = None
_MATHML_ID: int | None = None


def cf_html_format_id() -> int:
    global _CF_HTML_ID
    if _CF_HTML_ID is None:
        _CF_HTML_ID = _format_id("HTML Format")
    return _CF_HTML_ID


def mathml_format_id() -> int:
    global _MATHML_ID
    if _MATHML_ID is None:
        _MATHML_ID = _format_id("MathML")
    return _MATHML_ID


def _safe_id(getter):
    try:
        return getter()
    except ClipboardError:
        return None


# ---------------------------------------------------------------- 剪贴板事务

def _open_clipboard(purpose: str = "write") -> int:
    """指数退避打开剪贴板。返回实际尝试次数；全失败抛 ClipboardError（含错误详情）。

    其他进程（剪贴板管理器/同步工具/RDP 钩子）占用时 OpenClipboard 短暂失败是常态，
    退避策略: 20ms 起每次×2、上限 500ms、共 OPEN_RETRY_MAX 次。
    """
    delay = OPEN_RETRY_BASE_S
    detail = ""
    for attempt in range(1, OPEN_RETRY_MAX + 1):
        if _user32.OpenClipboard(None):
            if attempt > 1:
                print(
                    f"[clipboard] OpenClipboard[{purpose}] 第 {attempt}/{OPEN_RETRY_MAX} 次成功"
                    f"（指数退避 base={OPEN_RETRY_BASE_S*1000:.0f}ms ×2 cap={OPEN_RETRY_CAP_S*1000:.0f}ms），"
                    f"此前: {detail}；有进程占用剪贴板",
                    file=sys.stderr,
                )
            return attempt
        detail = last_err_text("OpenClipboard")
        if attempt < OPEN_RETRY_MAX:
            time.sleep(delay)
            delay = min(delay * 2, OPEN_RETRY_CAP_S)
    raise ClipboardError(
        f"OpenClipboard[{purpose}] {OPEN_RETRY_MAX} 次退避重试后仍失败: {detail} — "
        f"有进程长期霸占剪贴板（剪贴板管理器/远程桌面），关闭后重试"
    )


def _close_clipboard() -> None:
    if not _user32.CloseClipboard():
        print(f"[clipboard] 警告: {last_err_text('CloseClipboard')}", file=sys.stderr)


def _set_clipboard_bytes(fmt: int, fmt_name: str, data: bytes) -> None:
    """把 data 以 fmt 格式放入剪贴板（须在 OpenClipboard/EmptyClipboard 之间调用）。

    句柄生命周期: SetClipboardData 成功 = 所有权移交系统，绝不再碰；
    失败 = 所有权仍在调用方，先抓 GetLastError 再 GlobalFree（GlobalFree 会覆盖错误码）。
    """
    hmem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not hmem:
        raise ClipboardError(last_err_text(f"GlobalAlloc({len(data)}B) for {fmt_name}"))
    ptr = _kernel32.GlobalLock(hmem)
    if not ptr:
        detail = last_err_text(f"GlobalLock({fmt_name})")
        _kernel32.GlobalFree(hmem)
        raise ClipboardError(detail)
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        _kernel32.GlobalUnlock(hmem)
    if not _user32.SetClipboardData(fmt, hmem):
        # 关键: 先取错误码再释放（失败时所有权未移交，free 是正确路径，无 double-free）
        detail = last_err_text(f"SetClipboardData({fmt_name}/fmt={fmt})")
        _kernel32.GlobalFree(hmem)
        raise ClipboardError(detail)


# ---------------------------------------------------------------- CF_HTML 组装

def build_cf_html(mathml: str) -> bytes:
    """组装合法 CF_HTML 块。偏移一律按整块 UTF-8 编码后的字节计。"""
    doc_open = "<html><head><meta charset='utf-8'></head><body>"
    doc_close = "</body></html>"
    body = doc_open + FRAG_START + mathml + FRAG_END + doc_close
    head_fmt = (
        "Version:0.9\r\n"
        "StartHTML:%010d\r\n"
        "EndHTML:%010d\r\n"
        "StartFragment:%010d\r\n"
        "EndFragment:%010d\r\n"
    )
    # %010d 恒为 10 位数（偏移 < 10^10），故头部字节长度与填充值无关，可先算
    head_len = len((head_fmt % (0, 0, 0, 0)).encode("utf-8"))
    start_html = head_len
    end_html = head_len + len(body.encode("utf-8"))
    start_fragment = head_len + len((doc_open + FRAG_START).encode("utf-8"))
    end_fragment = start_fragment + len(mathml.encode("utf-8"))
    header = head_fmt % (start_html, end_html, start_fragment, end_fragment)
    return (header + body).encode("utf-8")


# ---------------------------------------------------------------- 写入口（三格式隔离）

def write_clipboard_text(text: str) -> None:
    """仅写 CF_UNICODETEXT（浮卡〔复制 LaTeX〕动作用；LaTeX 无 >127 之外特殊要求）。"""
    data = (text + "\x00").encode("utf-16-le")
    _open_clipboard("write_text")
    try:
        if not _user32.EmptyClipboard():
            raise ClipboardError(last_err_text("EmptyClipboard"))
        _set_clipboard_bytes(CF_UNICODETEXT, "CF_UNICODETEXT", data)
    finally:
        _close_clipboard()


def write_clipboard_formula(latex: str) -> dict:
    """LaTeX（已归一化）→ 三格式一次性写入剪贴板（隔离写入: 单格式失败不阻断其余）。

    返回 {"latex","mathml","cf_html","unicodetext","warnings":[str,...]}。
    warnings 非空表示某些格式未入板（CF_UNICODETEXT / MathML 降级损失）；
    **CF_HTML 失败为致命** —— 关闭剪贴板后抛 ClipboardError（Word 主链路不可用）。
    """
    mathml = latex_to_mathml(latex)
    cf_html = build_cf_html(mathml)
    text_bytes = (f"${latex}$" + "\x00").encode("utf-16-le")  # CF_UNICODETEXT: NUL 结尾
    mathml_bytes = mathml.encode("utf-8")

    warnings: list[str] = []

    # 逐格式注册（注册失败只影响该格式，不中断整体）
    specs: list[tuple[str, int | None, bytes]] = []
    try:
        specs.append(("cf_html", cf_html_format_id(), cf_html))
    except ClipboardError as e:
        warnings.append(f"[write/cf_html] 格式注册失败: {e}")
        specs.append(("cf_html", None, cf_html))
    specs.append(("CF_UNICODETEXT", CF_UNICODETEXT, text_bytes))
    try:
        specs.append(("MathML", mathml_format_id(), mathml_bytes))
    except ClipboardError as e:
        warnings.append(f"[write/MathML] 格式注册失败: {e}")
        specs.append(("MathML", None, mathml_bytes))

    _open_clipboard("write")
    try:
        if not _user32.EmptyClipboard():
            raise ClipboardError(last_err_text("EmptyClipboard"))
        for fmt_name, fmt, data in specs:
            if fmt is None:
                continue  # 注册失败已记 warning
            # 单格式最多尝试 2 次（同一开启事务内瞬时错误概率极低，但成本可忽略）
            for attempt in (1, 2):
                try:
                    _set_clipboard_bytes(fmt, fmt_name, data)
                    break
                except ClipboardError as e:
                    if attempt == 2:
                        warnings.append(f"[write/{fmt_name}] {e}")
                    else:
                        time.sleep(0.02)
    finally:
        _close_clipboard()

    # CF_HTML 失败 = 致命（主链路），其余格式失败留在 warnings
    if any(w.startswith("[write/cf_html]") for w in warnings):
        raise ClipboardError("CF_HTML（Word 主链路）写入失败: " + "; ".join(
            w for w in warnings if w.startswith("[write/cf_html]")))

    return {
        "latex": latex,
        "mathml": mathml,
        "cf_html": cf_html,
        "unicodetext": f"${latex}$",
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 读回与校验（三态区分）

def _read_one(fmt_name: str, fmt: int | None) -> dict:
    """读回单个格式。返回 {"state","data","detail"}；state 取值:
    ok / not_registered / not_on_clipboard / get_failed(含延迟渲染失败或被抢占) /
    lock_failed / empty_data
    """
    if fmt is None:
        return {"state": "not_registered", "data": None, "detail": "RegisterClipboardFormat 未成功"}
    avail = _user32.IsClipboardFormatAvailable(fmt)
    h = _user32.GetClipboardData(fmt)
    if not h:
        detail = last_err_text("GetClipboardData")
        if avail:
            # 在板却拿不到句柄：写入方若用延迟渲染且已死/拒绝渲染（本模块不用延迟渲染，
            # 出现即指向第三方进程抢占/篡改）。read_back 外层还会整体重试兜底。
            return {
                "state": "get_failed",
                "data": None,
                "detail": f"IsFormatAvailable=True 但 {detail}（延迟渲染失败或写入后被抢占）",
            }
        return {"state": "not_on_clipboard", "data": None, "detail": detail}
    size = _kernel32.GlobalSize(h)
    ptr = _kernel32.GlobalLock(h)
    if not ptr:
        return {
            "state": "lock_failed",
            "data": None,
            "detail": last_err_text("GlobalLock") + f"（格式在板上, GlobalSize={size}）",
        }
    try:
        data = ctypes.string_at(ptr, size) if size else ctypes.string_at(ptr)
    finally:
        _kernel32.GlobalUnlock(h)
    if not data:
        return {"state": "empty_data", "data": None, "detail": f"GlobalSize={size}, 内容为空"}
    return {"state": "ok", "data": data, "detail": ""}


def read_back() -> dict:
    """v2 三态读回。返回 {"cf_html","unicodetext","mathml"} → 各为 _read_one 结果 dict
    （{"state","data","detail"}，data 为原始 bytes 或 None）。

    若 cf_html 缺失（主链路），整体重试 3 轮（50/100ms 退避）——覆盖写入后被抢占的窗口。
    """
    results: dict = {}
    for round_no in (1, 2, 3):
        _open_clipboard("read")
        try:
            results = {
                "cf_html": _read_one("cf_html", _safe_id(cf_html_format_id)),
                "unicodetext": _read_one("CF_UNICODETEXT", CF_UNICODETEXT),
                "mathml": _read_one("MathML", _safe_id(mathml_format_id)),
            }
        finally:
            _close_clipboard()
        if results["cf_html"]["state"] == "ok" or round_no == 3:
            break
        time.sleep(0.05 * (2 ** (round_no - 1)))
    return results


def read_back_flat() -> dict:
    """M1 兼容读回：{"cf_html": bytes|None, "unicodetext": str|None, "mathml": str|None}。

    基于三态 read_back 拍平，供旧调用方使用；诊断场景请直接用 read_back()。
    """
    r = read_back()
    utext = r["unicodetext"]["data"]
    mathml = r["mathml"]["data"]
    return {
        "cf_html": r["cf_html"]["data"],
        "unicodetext": utext.decode("utf-16-le").rstrip("\x00") if utext else None,
        "mathml": mathml.decode("utf-8") if mathml else None,
    }


_CF_HTML_HEADER_RE = re.compile(
    r"^Version:(?P<version>[\d.]+)\s*\r\n"
    r"StartHTML:\s*(?P<start_html>\d+)\s*\r\n"
    r"EndHTML:\s*(?P<end_html>\d+)\s*\r\n"
    r"StartFragment:\s*(?P<start_frag>\d+)\s*\r\n"
    r"EndFragment:\s*(?P<end_frag>\d+)\s*\r\n",
    re.DOTALL,
)


def verify_cf_html(block: bytes, expected_mathml: str) -> list[str]:
    """独立重算并校验 CF_HTML 偏移，返回问题列表（空 = 通过）。

    已知风险点回归检查：偏移若按字符而非 UTF-8 字节计，
    含非 ASCII 的 MathML（希腊字母等）会让 Word 截错 fragment。
    """
    problems: list[str] = []
    try:
        header_text = block[: block.index(FRAG_START.encode("utf-8"))].decode("ascii")
    except Exception as e:  # noqa: BLE001
        return [f"CF_HTML 头部解析失败: {e}"]
    m = _CF_HTML_HEADER_RE.match(header_text)
    if not m:
        return ["CF_HTML 头不合规（Version/StartHTML/EndHTML/StartFragment/EndFragment）"]
    if m.group("version") != "0.9":
        problems.append(f"Version 非 0.9: {m.group('version')}")

    start_html = int(m.group("start_html"))
    end_html = int(m.group("end_html"))
    start_frag = int(m.group("start_frag"))
    end_frag = int(m.group("end_frag"))

    total = len(block)
    if start_frag <= start_html:
        problems.append("StartFragment 应大于 StartHTML")
    if not (start_html <= start_frag <= end_frag <= end_html == total):
        problems.append(
            f"偏移区间不闭合: start_html={start_html} start_frag={start_frag} "
            f"end_frag={end_frag} end_html={end_html} total={total}"
        )
        return problems

    # 标记字节级定位
    fs = block.find(FRAG_START.encode("utf-8"))
    fe = block.find(FRAG_END.encode("utf-8"))
    if fs < 0 or fe < 0:
        problems.append("缺少 StartFragment/EndFragment 注释标记")
        return problems
    if start_frag != fs + len(FRAG_START.encode("utf-8")):
        problems.append(
            f"StartFragment 偏移错位: 声明 {start_frag} vs 实际 {fs + len(FRAG_START.encode('utf-8'))} "
            f"(差 {start_frag - (fs + len(FRAG_START.encode('utf-8')))} — 疑似按字符而非字节计)"
        )
    if end_frag != fe:
        problems.append(f"EndFragment 偏移错位: 声明 {end_frag} vs 实际 {fe}")

    frag = block[start_frag:end_frag]
    try:
        frag_text = frag.decode("utf-8")
    except UnicodeDecodeError as e:
        problems.append(f"fragment 切片不是合法 UTF-8（偏移错位）: {e}")
        return problems
    if frag_text != expected_mathml:
        problems.append(
            f"fragment 内容与期望 MathML 不一致 (切片 {len(frag_text)} 字符 vs 期望 {len(expected_mathml)} 字符)"
        )
    return problems


def verify_mathml_xml(mathml: str) -> list[str]:
    """MathML 须为可解析 XML、根节点 <math> 且带命名空间。"""
    problems: list[str] = []
    try:
        root = ET.fromstring(mathml)
    except ET.ParseError as e:
        return [f"MathML XML 解析失败: {e}"]
    tag = root.tag.rsplit("}", 1)[-1]
    if tag != "math":
        problems.append(f"根节点应为 math，实为 {tag}")
    if root.tag != f"{{{MATHML_NS}}}math" and MATHML_NS not in mathml[:200]:
        problems.append("根 <math> 缺少 MathML 命名空间 xmlns")
    return problems


def selftest_case(case: dict) -> list[str]:
    """单条用例闭环：写入 → 三态读回 → 校验三格式。返回问题列表（空 = PASS）。

    读回状态按三态报告: not_registered / not_on_clipboard / get_failed /
    lock_failed / empty_data，便于在真机区分代码 bug 与环境抢占。
    （v2 selftest 口径，tests/run_e2e.py 与冒烟脚本复用。）
    """
    latex = case["latex"]
    problems: list[str] = []
    info = write_clipboard_formula(latex)
    expected_mathml = info["mathml"]
    problems += [f"[write] {w}" for w in info["warnings"]]

    back = read_back()

    # --- CF_HTML ---
    r = back["cf_html"]
    if r["state"] != "ok":
        problems.append(f"[cf_html] 读回 {r['state']}: {r['detail']}")
    else:
        problems += [f"[cf_html] {p}" for p in verify_cf_html(r["data"], expected_mathml)]

    # --- CF_UNICODETEXT ---
    r = back["unicodetext"]
    if r["state"] != "ok":
        problems.append(f"[CF_UNICODETEXT] 读回 {r['state']}: {r['detail']}")
    else:
        got = r["data"].decode("utf-16-le").rstrip("\x00")
        if got != f"${latex}$":
            problems.append(f"[CF_UNICODETEXT] 不匹配: {got!r}")

    # --- MathML 自定义格式 ---
    r = back["mathml"]
    if r["state"] != "ok":
        # 若写入侧已记该格式失败 warning，则不重复报读回
        if not any("write/MathML" in p or "write/None" in p for p in problems):
            problems.append(f"[MathML] 读回 {r['state']}: {r['detail']}")
    else:
        got = r["data"].decode("utf-8")
        if got != expected_mathml:
            problems.append("[MathML] 自定义格式内容与写入不一致")
        else:
            problems += [f"[mathml] {p}" for p in verify_mathml_xml(got)]

    return problems
