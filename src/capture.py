"""capture.py — 全局热键 + 全屏冻结 + 区域框选（SnipEq M2 工程壳）。

组成：
  ① HotkeyManager —— ctypes RegisterHotKey（message-only 窗口线程），零第三方依赖；
     默认 ``Ctrl+Alt+A``，spec 字符串可配（"ctrl+alt+a"、"ctrl+shift+x"、"f9"…）。
  ② grab_fullscreen() —— PIL ImageGrab.grab(all_screens=True) 多屏冻结，
     返回 (frozen_image, (vx, vy))，vx/vy 为虚拟屏左上角物理坐标（负值合法）。
  ③ TkOverlay —— tkinter 无边框置顶遮罩：显示冻结画面（即"全屏冻结"效果）、
     拖动橡胶筋选区（stipple 半透明黑蒙版）、Esc/右键取消、Enter/松开确认。
  ④ capture_region(root=None, overlay_factory=None) —— ②+③ 组合，返回
     (选区 PIL 图 | None, 选区物理矩形 (x,y,w,h) | None)。

overlay 为可替换接口（des-1 集成时可能换 PySide6 版）：
  overlay_factory(frozen: PIL.Image, offset: tuple[int,int],
                  parent: tk.Tk | None) -> RegionOverlay
  RegionOverlay.run() -> tuple[int,int,int,int] | None   # 阻塞至完成
      返回冻结图坐标系下的 (x0,y0,x1,y1)，取消返回 None。
  默认实现 TkOverlay 即遵守此协议（类本身可直接作为 factory 传入）。

线程约束：overlay 必须运行在 **创建 Tk root 的主线程**；热键回调在监听线程，
调用方（tray_app）经队列转投主线程。DPI：进程需先 ensure_dpi_aware()。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

# ---------------------------------------------------------------- Win32 基础

_user32 = ctypes.windll.user32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77

# RegisterHotKey 修饰键
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WS_POPUP = 0x80000000
HWND_MESSAGE = ctypes.c_void_p(-3)

# 64 位 LPARAM/WPARAM 必须按声明宽度传递（否则 DefWindowProcW 溢出）
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_long
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]

_NAMED_VK = {
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "printscreen": 0x2C, "prtsc": 0x2C,
}


def ensure_dpi_aware() -> None:
    """Per-Monitor v2 → 退 System DPI → 再退 SetProcessDPIAware（多屏坐标口径统一）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def virtual_screen_origin_size() -> tuple[int, int, int, int]:
    """虚拟屏 (x, y, w, h)（物理像素；主屏左上非原点时 x/y 可为负）。"""
    vx = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    # GetSystemMetrics 对虚拟屏指标用 int 足够（restype 默认 c_int）
    vw = _user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    vh = _user32.GetSystemMetrics(79)      # SM_CYVIRTUALSCREEN
    return vx, vy, vw, vh


# ---------------------------------------------------------------- ① 全局热键

def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+a' → (modifiers, vk)。失败抛 ValueError。"""
    parts = [p.strip().lower() for p in spec.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("空热键规格")
    mods = MOD_NOREPEAT
    key = parts[-1]
    for m in parts[:-1]:
        if m in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif m == "alt":
            mods |= MOD_ALT
        elif m == "shift":
            mods |= MOD_SHIFT
        elif m in ("win", "meta", "super"):
            mods |= MOD_WIN
        else:
            raise ValueError(f"未知修饰键: {m}")
    if key in _NAMED_VK:
        vk = _NAMED_VK[key]
    elif len(key) == 1:
        vk = ord(key.upper())
    else:
        raise ValueError(f"未知主键: {key}")
    return mods, vk


class HotkeyManager:
    """message-only 窗口线程上的 RegisterHotKey。start() 后回调在监听线程触发。"""

    def __init__(self, spec: str, callback):
        self.spec = spec
        self.callback = callback
        self._mods, self._vk = parse_hotkey(spec)
        self._hwnd = None
        self._tid = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._wndproc_cfunc = None  # 持引用防 GC

    # -- 内部窗口过程（监听线程）
    def _thread_main(self) -> None:
        try:
            user32 = _user32

            WNDPROCTYPE = ctypes.WINFUNCTYPE(
                ctypes.c_long, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM,
            )

            def _wnd_proc(hwnd, msg, wparam, lparam):
                if msg == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:  # noqa: BLE001 回调异常不能杀死消息环
                        import traceback
                        traceback.print_exc()
                    return 0
                if msg == WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc_cfunc = WNDPROCTYPE(_wnd_proc)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROCTYPE),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            hmod = ctypes.windll.kernel32.GetModuleHandleW(None)
            # 类名必须每实例唯一：若固定复用，后建实例的 CreateWindow 会挂到
            # 先建实例（可能已被 GC）的 wndproc 指针上 → 0xC0000409 悬垂回调崩溃
            class_name = f"SnipEqHotkeyWnd-{id(self):X}-{int(time.time() * 1000) % 100000:X}"
            wc = WNDCLASSW(
                style=0, lpfnWndProc=self._wndproc_cfunc,
                cbClsExtra=0, cbWndExtra=0,
                hInstance=hmod, hIcon=None, hCursor=None, hbrBackground=None,
                lpszMenuName=None, lpszClassName=class_name,
            )
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self._error = (
                    f"RegisterClassW 失败 (err={ctypes.windll.kernel32.GetLastError()})")
                self._ready.set()
                return
            hwnd = user32.CreateWindowExW(
                0, class_name, "SnipEq", WS_POPUP,
                0, 0, 0, 0, HWND_MESSAGE, None, hmod, None,
            )
            if not hwnd:
                self._error = (
                    f"CreateWindowExW(message-only) 失败 (err={ctypes.windll.kernel32.GetLastError()})")
                self._ready.set()
                return
            self._hwnd = hwnd
            self._tid = kernel32_GetCurrentThreadId()
            if not user32.RegisterHotKey(wintypes.HWND(hwnd), 1, self._mods, self._vk):
                self._error = (
                    f"RegisterHotKey({self.spec!r}) 失败"
                    f"（err={ctypes.windll.kernel32.GetLastError()}）— 可能与其他程序占用冲突")
                user32.DestroyWindow(hwnd)
                self._ready.set()
                return
            self._error = None
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            user32.UnregisterHotKey(wintypes.HWND(hwnd), 1)
            user32.UnregisterClassW(class_name, hmod)
        except Exception as e:  # noqa: BLE001
            self._error = f"热键线程异常: {e}"
            self._ready.set()

    # -- 公共口
    def start(self, timeout: float = 3.0) -> None:
        if self._thread is not None:
            raise RuntimeError("HotkeyManager 已启动")
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name="snipeq-hotkey")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("热键线程初始化超时")
        if self._error:
            self._thread = None
            raise RuntimeError(self._error)

    def stop(self) -> None:
        if self._hwnd:
            _user32.PostMessageW(wintypes.HWND(self._hwnd), WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None
        self._hwnd = None

    __enter__ = lambda self: (self.start(), self)[1]  # noqa: E731

    def __exit__(self, *exc) -> None:
        self.stop()


def kernel32_GetCurrentThreadId() -> int:
    return ctypes.windll.kernel32.GetCurrentThreadId()


# ---------------------------------------------------------------- ② 全屏冻结

def grab_fullscreen() -> tuple["object", tuple[int, int]]:
    """抓取整个虚拟屏（多屏拼接）。返回 (PIL.Image, (vx, vy))。"""
    from PIL import ImageGrab

    ensure_dpi_aware()
    img = ImageGrab.grab(all_screens=True)
    vx, vy, _, _ = virtual_screen_origin_size()
    return img, (vx, vy)


# ---------------------------------------------------------------- ③ tkinter 遮罩

class TkOverlay:
    """tkinter 无边框置顶遮罩（默认 overlay 实现，可被 overlay_factory 替换）。

    协议：构造 (frozen, offset, parent=None)；run() 阻塞至完成，
    返回冻结图坐标 (x0, y0, x1, y1) 或 None（取消）。
    """

    MIN_SIZE = 4  # 选区最小边长（像素）

    def __init__(self, frozen, offset: tuple[int, int], parent=None):
        self.frozen = frozen
        self.offset = offset
        self._parent = parent
        self._box: tuple[int, int, int, int] | None = None
        self._start: tuple[int, int] | None = None
        self._cur: tuple[int, int] | None = None
        self._done = False

    def run(self):
        import tkinter as tk
        from PIL import ImageTk

        own_root = self._parent is None
        root = self._parent if not own_root else tk.Tk()
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        w, h = self.frozen.size
        vx, vy = self.offset
        top.geometry(f"{w}x{h}+{vx}+{vy}")
        canvas = tk.Canvas(top, width=w, height=h, highlightthickness=0,
                           cursor="crosshair", bg="black")
        canvas.pack(fill="both", expand=True)
        photo = ImageTk.PhotoImage(self.frozen)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.image_ref = photo  # 持引用防 GC 白屏

        def redraw(*_):
            canvas.delete("rubber")
            if not (self._start and self._cur):
                return
            x0, y0 = self._start
            x1, y1 = self._cur
            x0, x1 = min(x0, x1), max(x0, x1)
            y0, y1 = min(y0, y1), max(y0, y1)
            # 选区外 4 块半透明黑蒙版（stipple 模拟 alpha）
            for rx0, ry0, rx1, ry1 in (
                (0, 0, w, y0), (0, y1, w, h),
                (0, y0, x0, y1), (x1, y0, w, y1),
            ):
                if rx1 > rx0 and ry1 > ry0:
                    canvas.create_rectangle(rx0, ry0, rx1, ry1, fill="black",
                                            stipple="gray50", outline="", tags="rubber")
            canvas.create_rectangle(x0, y0, x1, y1, outline="#ff3355", width=2,
                                    tags="rubber")

        def press(e):
            self._start, self._cur = (e.x, e.y), (e.x, e.y)
            redraw()

        def drag(e):
            self._cur = (e.x, e.y)
            redraw()

        def finish_box() -> tuple[int, int, int, int] | None:
            if not (self._start and self._cur):
                return None
            x0, x1 = sorted((self._start[0], self._cur[0]))
            y0, y1 = sorted((self._start[1], self._cur[1]))
            if x1 - x0 < self.MIN_SIZE or y1 - y0 < self.MIN_SIZE:
                return None
            return (x0, y0, x1, y1)

        def release(_e):
            # 松开即显示选区，Enter 确认（SnipPaste 式两段操作）；过小视为误触
            if finish_box() is None:
                self._start = self._cur = None
                redraw()

        def confirm(_e=None):
            box = finish_box()
            if box is not None:
                self._box = box
                self._done = True

        def cancel(_e=None):
            self._box = None
            self._done = True

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        canvas.bind("<ButtonRelease-1>", release)
        top.bind("<Return>", confirm)
        top.bind("<Escape>", cancel)
        canvas.bind("<ButtonPress-3>", cancel)  # 右键取消
        canvas.bind("<Double-Button-1>", confirm)  # 双击快速确认

        top.focus_force()
        if own_root:
            while not self._done:
                root.update_idletasks()
                root.update()
                time.sleep(0.01)
            root.destroy()
        else:
            root.wait_window(top)
        return self._box


# ---------------------------------------------------------------- ④ 组合口

def capture_region(root=None, overlay_factory=None):
    """全屏冻结 → 遮罩选区。返回 (选区 PIL.Image | None, 物理矩形 (x,y,w,h) | None)。

    overlay_factory(frozen, offset, parent=root) -> overlay（.run() 返回
    冻结图坐标 (x0,y0,x1,y1) 或 None）。缺省 TkOverlay；des-1 换 PySide6 遮罩时
    只需替换此工厂。测试/零交互注入：传 full_image_factory 即取全图。
    """
    frozen, offset = grab_fullscreen()
    overlay = (overlay_factory or TkOverlay)(frozen, offset, root)
    box = overlay.run()
    if box is None:
        return None, None
    x0, y0, x1, y1 = box
    img = frozen.crop((x0, y0, x1, y1))
    vx, vy = offset
    return img, (vx + x0, vy + y0, x1 - x0, y1 - y0)


def full_image_factory(frozen, offset, parent=None):
    """注入用 overlay 工厂：跳过人工框选，直接返回整幅冻结图的选区框。"""

    class _All:
        def run(self):
            return (0, 0, frozen.size[0], frozen.size[1])

    return _All()


# ---------------------------------------------------------------- CLI 冒烟

if __name__ == "__main__":
    import sys

    if "--hotkey-test" in sys.argv:
        # 注册默认热键 10 秒，按到打印 HIT
        fired = []
        hm = HotkeyManager("ctrl+alt+a", lambda: fired.append(time.time()))
        hm.start()
        print("按下 Ctrl+Alt+A（10s 内）…")
        t0 = time.time()
        while time.time() - t0 < 10 and not fired:
            time.sleep(0.1)
        hm.stop()
        print("HIT" if fired else "MISS")
        sys.exit(0 if fired else 1)
    # 默认：弹遮罩选区，保存选区图（人工冒烟）
    img, rect = capture_region()
    if img is None:
        print("已取消")
    else:
        out = "capture_out.png"
        img.save(out)
        print(f"saved {out} rect={rect}")
