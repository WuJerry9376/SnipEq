"""tray_app.py — SnipEq 托盘主程序（M2 集成车道）。

形态（规划书 §6.5-B 为主 + A 开关）：
  pystray 托盘（菜单：立即截图 / 零确认模式 / 空闲卸载模型 / 开机自启 /
  最近历史(≤10 条点击重灌) / 退出）
  + 全局热键（settings.json 可配，默认 Ctrl+Alt+A，capture.HotkeyManager）
  + 管线：capture 框选 → recognizer(PaddleX) → normalize → preview(PNG)
          → ui.formula_card.FormulaCard（des-1 契约：show_result(image_png|None, latex)
            / show_near(x,y,w,h)[逻辑像素] / 信号 copy_word, copy_latex,
            cloud_enhance, zero_confirm_toggled(bool), close_requested /
            构造 on_latex_edited 防抖回调）
  降级：import ui 失败 或 env SNIPEQ_FORCE_TK=1 → 识别后直接
        write_clipboard_formula + 托盘气泡；零确认模式同样直写不弹卡。

坐标口径：capture 输出**物理像素**矩形；FormulaCard.show_near 契约为**逻辑像素**，
主线程弹卡前经 physical_to_logical_rect(主屏 scale) 换算（多屏混合 DPI 从属屏
按主屏因子近似，真值留 M3 矩阵）。

事件主环选择：
  - FormulaCard 可用 → **Qt 主环为默认**（QApplication.exec + QTimer 30ms 泵送
    tkinter root.update()，截图遮罩仍是 Tk overlay，可经 overlay_factory 换 Qt 版）；
  - 降级/不可用 → tkinter mainloop 主环（fallback）。
  线程模型：热键线程 / pystray 菜单线程只向 queue 投递；Tk/Qt 对象只在主线程触碰；
  识别模型**懒加载**且只被 worker 线程持有，空闲 unload_idle_min 分钟自动释放（0=从不）。

用法：
  py tray_app.py [--hotkey S] [--zero-confirm] [--unload-idle-min N] [--no-preview]
                 [--once-image PATH] [--smoke SECONDS]
  未显式给 CLI 开关时取 settings.json（%APPDATA%\\SnipEq\\，env SNIPEQ_SETTINGS_PATH 覆盖）。
  --once-image：冒烟注入（跳过热键/框选，直接对该图跑管线 → 卡片/降级路径）。
  --smoke     ：启动完整壳 N 秒后自行退出并打印 SMOKE OK。
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

# FormulaCard 契约探测（des-1 并行开发；失败走降级，不影响本模块 import）
try:
    from ui.formula_card import FormulaCard  # type: ignore
    UI_IMPORT_ERROR: str | None = None
except Exception as e:  # noqa: BLE001  ImportError 或 PySide6 缺失都算不可用
    FormulaCard = None  # type: ignore[assignment]
    UI_IMPORT_ERROR = f"{type(e).__name__}: {e}"

import capture  # noqa: E402
from clipboard_writer import (  # noqa: E402
    ClipboardError, write_clipboard_formula, write_clipboard_text,
)
from normalize import normalize  # noqa: E402
from settings import (  # noqa: E402
    History, Settings, cleanup_temp, image_sha1, set_autostart,
)
from version import __version__  # noqa: E402


def log(msg: str) -> None:
    print(f"[snipeq {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- DPI 换算

def physical_to_logical_rect(rect, scale: float):
    """物理像素矩形 (x,y,w,h) → 逻辑像素（主屏 scale 口径，纯函数可单测）。"""
    x, y, w, h = rect
    s = float(scale) if scale and scale > 0 else 1.0
    return (int(round(x / s)), int(round(y / s)),
            int(round(w / s)), int(round(h / s)))


def display_scale() -> float:
    """主屏 DPI 缩放因子（物理px/逻辑px）。优先已存活的 Qt，其次 Win32 LOGPIXELSX。"""
    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is not None and app.primaryScreen() is not None:
            return float(app.primaryScreen().devicePixelRatio())
    except Exception:  # noqa: BLE001  无 Qt/无屏幕环境
        pass
    try:
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        dc = user32.GetDC(None)
        lpx = gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        user32.ReleaseDC(None, dc)
        return (lpx or 96) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def make_icon_image():
    """PIL 现画 64px 托盘图（深色圆底 + 白色 ∑）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(24, 24, 32, 255),
              outline=(255, 90, 120, 255), width=4)
    try:
        d.text((32, 30), "\u2211", fill=(255, 255, 255, 255), anchor="mm")
    except Exception:  # noqa: BLE001  老 Pillow 无 anchor 时退化画横线
        d.line((20, 18, 44, 18), fill=(255, 255, 255, 255), width=4)
        d.line((20, 44, 44, 44), fill=(255, 255, 255, 255), width=4)
    return img


# ---------------------------------------------------------------- 热键捕获对话框 / 消息框

_MOD_KEYS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
    "Win_L": "win", "Win_R": "win", "Super_L": "win", "Super_R": "win",
    "Meta_L": "win", "Meta_R": "win",
}
_MAIN_KEY_ALIAS = {
    "space": "space", "Escape": "esc", "Return": "enter", "KP_Enter": "enter",
    "Tab": "tab", "BackSpace": "backspace", "Delete": "delete",
    "Insert": "insert", "Home": "home", "End": "end",
    "Prior": "pageup", "Next": "pagedown",
}
_IGNORED_PREFIX = ("Control", "Alt", "Shift", "Super", "Meta", "Win", "Caps",
                   "Num", "Scroll", "Kana", "Hangul", "Hanja", "Menu", "Print",
                   "Pause")


def _keysym_to_main(ks: str) -> str | None:
    """keysym → parse_hotkey 可识别的主键 token；None=忽略该键（修饰/锁键/F13+）。"""
    if not ks or ks.startswith(_IGNORED_PREFIX):
        return None
    if ks in _MAIN_KEY_ALIAS:
        return _MAIN_KEY_ALIAS[ks]
    if len(ks) == 1:
        return ks.lower()
    if ks.startswith("F") and ks[1:].isdigit():
        n = int(ks[1:])
        return ks.lower() if 1 <= n <= 12 else "F_REJECT"
    return None


def _tk_msgbox(parent, kind: str, title: str, msg: str) -> bool:
    """tk 消息框（parent 可为隐藏 root）；返回 askokcancel 的布尔结果。"""
    try:
        from tkinter import messagebox
        kw = {"parent": parent, "title": title} if parent is not None else {"title": title}
        if kind == "okinfo":
            messagebox.showinfo(message=msg, **kw); return True
        if kind == "okcancel":
            return bool(messagebox.askokcancel(message=msg, **kw))
        messagebox.showerror(message=msg, **kw); return False
    except Exception:  # noqa: BLE001 无显示环境降级为日志
        log(f"[dialog:{title}] {msg}")
    return False


def hotkey_capture_dialog(root, current_spec: str) -> "str | None":
    """模态小对话框：按下新组合实时回显 → 确定返回 spec；取消/关窗返回 None。

    校验在确定时由调用方走 _swap_hotkey（真实注册+回滚），此处仅做即时 parse 反馈。
    """
    import tkinter as tk

    dlg = tk.Toplevel(root)
    dlg.title("修改快捷键")
    dlg.resizable(False, False)
    dlg.attributes("-topmost", True)
    tk.Label(dlg, text="请按下新的全局快捷键组合\n（修饰键 Ctrl/Alt/Shift/Win + 主键，或 F1–F12）",
             justify="left").pack(padx=16, pady=(14, 6))
    echo = tk.Label(dlg, text=f"当前：{current_spec}", font=("Consolas", 12, "bold"))
    echo.pack(padx=16, pady=4)
    hint = tk.Label(dlg, text="", fg="#a04040")
    hint.pack(padx=16)
    row = tk.Frame(dlg); row.pack(pady=10)
    state = {"mods": set(), "spec": None, "done": False, "result": None}

    def fmt(order):
        return "+".join(order) if order else "（等待按键…）"

    def on_press(e):
        ks = e.keysym
        if ks in _MOD_KEYS:
            state["mods"].add(_MOD_KEYS[ks])
            order = [m for m in ("win", "ctrl", "alt", "shift") if m in state["mods"]]
            echo.config(text=fmt(order + ["?"]))
            hint.config(text="")
            return
        if ks == "Escape" and not state["mods"]:
            close(); return
        main = _keysym_to_main(ks)
        if main is None:
            return
        if main == "F_REJECT":
            hint.config(text=f"不支持 {ks}（仅 F1–F12）")
            return
        order = [m for m in ("win", "ctrl", "alt", "shift") if m in state["mods"]]
        spec = "+".join(order + [main])
        try:
            capture.parse_hotkey(spec)
        except ValueError as ex:
            hint.config(text=str(ex)); state["spec"] = None
            return
        state["spec"] = spec
        echo.config(text=spec)
        hint.config(text="")

    def on_release(e):
        if e.keysym in _MOD_KEYS:
            state["mods"].discard(_MOD_KEYS[e.keysym])

    def ok():
        if state["spec"] is None:
            hint.config(text="请先按下一组合法快捷键")
            return
        state["result"] = state["spec"]
        state["done"] = True

    def cancel():
        state["result"] = None
        state["done"] = True

    def close():
        cancel()

    tk.Button(row, text="确定", width=8, command=ok).pack(side="left", padx=6)
    tk.Button(row, text="取消", width=8, command=cancel).pack(side="left", padx=6)
    dlg.bind("<KeyPress>", on_press)
    dlg.bind("<KeyRelease>", on_release)
    dlg.protocol("WM_DELETE_WINDOW", close)
    dlg.grab_set()
    dlg.focus_force()
    while not state["done"]:
        dlg.update_idletasks()
        dlg.update()
        time.sleep(0.02)
    dlg.grab_release()
    dlg.destroy()
    return state["result"]


class TkUiThread(threading.Thread):
    """Qt 模式专属 Tk 线程：拥有 root、截图遮罩与所有 tkinter 对话框。

    线程所有权纪律（M3e P0 修复核心）：
      - tkinter 非线程安全：**本线程之外无人可触碰 self.root 及任何 Tk 对象**；
      - 主线程（Qt）只经 jobs 队列下发工作、经 app.events 收结果；
      - mainloop 常驻本线程，overlay 的模态轮询环/对话框 wait 循环都跑在这里，
        Qt 心跳不再"代泵" Tk（旧双源 QTimer+after 泵是定时器风暴/假死根因之一）。
    """

    def __init__(self, app):
        super().__init__(daemon=True, name="snipeq-tkui")
        self.app = app
        self.root = None
        self.ready = threading.Event()
        self.jobs: queue.Queue = queue.Queue()

    def run(self) -> None:
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("SnipEq-TkUI")
        self.root.after(40, self._drain)
        self.ready.set()
        try:
            self.root.mainloop()
        except Exception:  # noqa: BLE001
            pass

    def _drain(self) -> None:
        try:
            while True:
                job = self.jobs.get_nowait()
                try:
                    self._exec(job)
                except Exception:  # noqa: BLE001 单 job 异常不断环
                    log(f"TkUI job {job[0]} 异常:\n" + traceback.format_exc())
                    if job[0] == "capture":
                        self.app.post("region_ready", None, None)
        except queue.Empty:
            pass
        if self.root is not None:
            try:
                self.root.after(40, self._drain)
            except Exception:  # noqa: BLE001
                pass

    def _exec(self, job) -> None:
        kind = job[0]
        app = self.app
        if kind == "capture":
            app._abort_capture.clear()
            factory = app.overlay_factory or capture.TkOverlay
            img, rect = capture.capture_region(root=self.root, overlay_factory=factory,
                                               abort_event=app._abort_capture)
            if img is None:
                app.post("region_ready", None, None)
                return
            path = os.path.join(tempfile.gettempdir(),
                                f"snipeq_shot_{int(time.time() * 1000)}.png")
            img.save(path)
            del img
            app.post("region_ready", path, rect)
        elif kind == "hotkey_dialog":
            spec = hotkey_capture_dialog(self.root, app.hotkey_spec)
            app.post("hotkey_dialog_done", spec)
        elif kind == "tk_quit":
            try:
                self.root.quit()
            except Exception:  # noqa: BLE001
                pass


class SnipEqApp:
    def __init__(
        self,
        hotkey: str | None = None,
        zero_confirm: bool | None = None,
        unload_idle_min: float | None = None,
        preview_enabled: bool | None = None,
        overlay_factory=None,
        settings: Settings | None = None,
        history: History | None = None,
    ):
        self.settings = settings or Settings()
        self.history = history if history is not None else History()
        # CLI 显式值 > settings.json > 内置默认
        self.hotkey_spec = hotkey or self.settings.get("hotkey") or "ctrl+alt+a"
        self.zero_confirm = (zero_confirm if zero_confirm is not None
                             else bool(self.settings.get("zero_confirm")))
        _idle = self.settings.get("unload_idle_min", 10.0)
        self.unload_idle_min = (unload_idle_min if unload_idle_min is not None
                                else float(10.0 if _idle is None else _idle))
        self.preview_enabled = (preview_enabled if preview_enabled is not None
                                else bool(self.settings.get("preview_enabled", True)))
        self.overlay_factory = overlay_factory  # None → capture.TkOverlay

        self.events: queue.Queue = queue.Queue()   # 任意线程 → 主线程
        self.tasks: queue.Queue = queue.Queue()    # 主线程 → worker
        self.rec = None                            # 仅 worker 线程触碰
        self.rec_last_use = 0.0
        self.preview = None                        # PreviewRenderer（内部锁，线程安全）
        self._preview_lock = threading.Lock()
        self.root = None                           # tk.Tk：tk 降级模式=主线程持有；
        self.tkui = None                           # Qt 模式=TkUiThread 持有（跨线程禁触）
        self._abort_capture = threading.Event()    # Ctrl+Alt+Q 中止遮罩（overlay 轮询读）
        self._capturing = False                    # 截图防重入
        self.icon = None                           # pystray.Icon
        self.hotkey_mgr = None
        self.card = None
        self._cur_latex: str | None = None         # 卡片当前 LaTeX（copy 信号无参，靠它）
        self.ui_degraded = FormulaCard is None or os.environ.get("SNIPEQ_FORCE_TK") == "1"
        if self.ui_degraded:
            why = UI_IMPORT_ERROR or "SNIPEQ_FORCE_TK=1"
            log(f"UI 不可用/强制降级，直写剪贴板模式（{why}）")
        self._stop = threading.Event()

    # ------------------------------------------------------------ 事件投递
    def post(self, kind: str, *args) -> None:
        self.events.put((kind, args))

    # ------------------------------------------------------------ 生命周期
    def run(self, smoke_seconds: float = 0.0, once_image: str | None = None) -> int:
        """主环启动。

        **P0 修复（M3e）线程所有权**：Qt 模式下 tkinter 全部对象（overlay/对话框）
        归**专属 Tk 线程**（TkUiThread）持有并自持 mainloop；主线程 QTimer 只消费
        events 队列、**绝不跨线程碰 Tk**——修掉旧结构里"QTimer _tick + root.after
        _tick 双源泵 + 主线程 root.update()"的定时器复利风暴（越跑越卡→假死+内存
        增长的第二根因）。tk 降级模式（无 PySide6/强制）保持旧单线程结构：
        root 属主线程，after 单源泵，overlay 同步跑（无 Qt 即无双源问题）。
        """
        if not self.ui_degraded:
            self.tkui = TkUiThread(self)
            self.tkui.start()
            if not self.tkui.ready.wait(5):
                log("Tk UI 线程启动超时，转降级模式")
                self.ui_degraded = True
                self.tkui = None

        if self.ui_degraded:
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
            try:
                self.root.title("SnipEq")
            except Exception:  # noqa: BLE001
                pass

        capture.ensure_dpi_aware()
        try:
            cleanup_temp(24.0, log=log)
        except Exception as e:  # noqa: BLE001  清理失败不阻塞启动
            log(f"temp 清理异常: {e}")

        self._start_tray()
        self._start_hotkey()
        self._start_abort_hotkey()

        if smoke_seconds > 0:
            self._timer(smoke_seconds, lambda: self.post("quit"))
        if once_image:
            self._timer(0.3, lambda: self._enqueue_pipeline(once_image, None))

        if not self.ui_degraded:
            self._run_qt_main()
        else:
            self._run_tk_main()
        self._shutdown()
        return 0

    def _timer(self, seconds: float, fn) -> None:
        """后台倒计时触发（回调只做 post，线程安全）。"""
        def _later():
            time.sleep(seconds)
            if not self._stop.is_set():
                fn()
        threading.Thread(target=_later, daemon=True).start()

    def _run_tk_main(self) -> None:
        # 降级模式：root 属主线程，after 单源泵（无 Qt 即无双源问题）
        self.root.after(30, self._tick)
        self.root.mainloop()

    def _tick(self) -> None:
        """心跳。Qt 模式只由 QTimer 触发（单源，不重注册 after）；
        tk 模式由 after 链自注册。两模式各自只存在一条调度线。"""
        self._pump()
        if self.ui_degraded and self.root is not None and not self._stop.is_set():
            try:
                self.root.after(30, self._tick)
            except Exception:  # noqa: BLE001
                pass

    def _run_qt_main(self) -> None:
        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
        except Exception as e:  # noqa: BLE001
            log(f"PySide6 不可用（{e}），转 tk 主环 + 降级")
            self.ui_degraded = True
            self._run_tk_main()
            return
        app = QApplication.instance() or QApplication([])
        pump = QTimer()
        pump.setInterval(30)
        pump.timeout.connect(self._tick)
        pump.start()
        app.exec()

    def _pump(self) -> None:
        """主环心跳：只消费 events 队列。**绝不触碰 Tk 对象**——Qt 模式下 Tk 属
        TkUiThread（tkinter 非线程安全，跨线程 update()/after() 即 M3e 双泵根因）；
        tk 模式的事件调度由 mainloop 自身完成。"""
        try:
            while True:
                kind, args = self.events.get_nowait()
                try:
                    getattr(self, f"_on_{kind}")(*args)
                except Exception:  # noqa: BLE001 单事件异常不杀主环
                    log(f"事件 {kind} 处理异常:\n" + traceback.format_exc())
        except queue.Empty:
            pass

    def _shutdown(self) -> None:
        self._stop.set()
        self._abort_capture.set()
        self.tasks.put(("exit",))
        for mgr in (self.hotkey_mgr, getattr(self, "abort_mgr", None)):
            if mgr:
                try:
                    mgr.stop()
                except Exception:  # noqa: BLE001
                    pass
        if self.icon:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._preview_lock:
            if self.preview is not None:
                self.preview.close()
                self.preview = None

    def _unload_label(self) -> str:
        return (f"空闲 {self.unload_idle_min:.0f} 分钟卸载模型"
                if self.unload_idle_min > 0 else "从不空闲卸载")

    def _history_items(self) -> tuple:
        """动态子菜单：最近 10 条历史，点击重灌卡片。"""
        from pystray import MenuItem as Item
        if not self.settings.get("history_enabled", True):
            return (Item("（历史已关闭）", None, enabled=False),)
        entries = self.history.recent(10)
        if not entries:
            return (Item("（暂无历史）", None, enabled=False),)
        items = []
        for e in entries:
            label = e["latex"]
            short = label if len(label) <= 42 else label[:40] + "…"
            items.append(Item(f"{e.get('ts', '')[11:]}  {short}",
                              self._hist_cb(label)))
        return tuple(items)

    def _hist_cb(self, latex: str):
        return lambda icon, item: self.post("history", latex)

    def _start_tray(self) -> None:
        import pystray
        from pystray import Menu, MenuItem as Item

        def _m(fn):
            return lambda icon, item: fn()

        menu = Menu(
            Item("立即截图", _m(lambda: self.post("capture"))),
            Item("零确认模式", _m(lambda: self.post("toggle_zero_confirm")),
                 checked=lambda item: self.zero_confirm),
            Item(lambda item: self._unload_label(),
                 _m(lambda: self.post("toggle_unload")),
                 checked=lambda item: self.unload_idle_min > 0),
            Item("开机自启", _m(lambda: self.post("toggle_autostart")),
                 checked=lambda item: bool(self.settings.get("autostart"))),
            Item("修改快捷键…", _m(lambda: self.post("change_hotkey"))),
            Item(lambda item: f"检查更新（v{__version__}）",
                 _m(lambda: self.post("check_update"))),
            Item("最近历史", Menu(lambda: self._history_items())),
            pystray.Menu.SEPARATOR,
            Item("退出", _m(lambda: self.post("quit"))),
        )
        self.icon = pystray.Icon("SnipEq", make_icon_image(), "SnipEq 公式快贴", menu)
        self.icon.run_detached()

    def _show_msg(self, kind: str, title: str, msg: str) -> bool:
        """用户消息框。Qt 模式→QMessageBox（主线程合法）；
        降级模式→tk messagebox（root 属主线程）。返回 askokcancel 语义布尔。"""
        if not self.ui_degraded:
            try:
                from PySide6.QtWidgets import QMessageBox
                box = QMessageBox()
                if kind == "okcancel":
                    box.setIcon(QMessageBox.Question)
                    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
                    return box.exec() == QMessageBox.Ok
                box.setIcon(QMessageBox.Warning if kind == "error"
                            else QMessageBox.Information)
                box.setText(msg)
                box.setWindowTitle(title)
                box.exec()
                return True
            except Exception:  # noqa: BLE001
                pass
        return _tk_msgbox(self.root, kind, title, msg)

    def _make_hotkey(self, spec: str) -> "capture.HotkeyManager":
        mgr = capture.HotkeyManager(spec, lambda: self.post("capture"))
        mgr.start()
        return mgr

    def _start_hotkey(self) -> None:
        try:
            self.hotkey_mgr = self._make_hotkey(self.hotkey_spec)
            log(f"全局热键已注册: {self.hotkey_spec}")
        except Exception as e:  # noqa: BLE001
            self.hotkey_mgr = None
            log(f"热键注册失败（仅托盘菜单可用）: {e}")

    def _start_abort_hotkey(self) -> None:
        """P0 逃生：第二全局热键 Ctrl+Alt+Q 无条件中止截图遮罩
        （不依赖遮罩键盘焦点——前台锁场景下 Enter/Esc 可能进不了窗口）。"""
        try:
            self.abort_mgr = capture.HotkeyManager(
                "ctrl+alt+q", self._on_abort_hotkey)
            self.abort_mgr.start()
            log("中止热键已注册: ctrl+alt+q")
        except Exception as e:  # noqa: BLE001
            self.abort_mgr = None
            log(f"中止热键注册失败（看门狗 120s 仍兜底）: {e}")

    def _on_abort_hotkey(self) -> None:
        # 热键线程 → 仅置事件（overlay 轮询环 100ms 内自撤），不碰任何 UI 对象
        self._abort_capture.set()

    # ------------------------------------------------------------ 修改快捷键（M3c）

    def _on_change_hotkey(self) -> None:
        """修改快捷键：Qt 模式对话框下发 Tk 线程（结果经 hotkey_dialog_done 回），
        降级模式主线程直接弹。校验链在 _apply_new_hotkey。"""
        if not self.ui_degraded and self.tkui is not None:
            self.tkui.jobs.put(("hotkey_dialog",))
            return
        spec = hotkey_capture_dialog(self.root, self.hotkey_spec)
        self._apply_new_hotkey(spec)

    def _on_hotkey_dialog_done(self, spec) -> None:
        self._apply_new_hotkey(spec)

    def _apply_new_hotkey(self, spec) -> None:
        if spec is None or spec == self.hotkey_spec:
            return
        ok_, msg = self._swap_hotkey(spec)
        if ok_:
            try:
                self.settings.set(hotkey=spec)
                self.settings.save()
            except Exception as e:  # noqa: BLE001
                log(f"settings 保存失败: {e}")
            self._show_msg("okinfo", "快捷键已更新", msg)
            log(f"快捷键已热更新: {spec}")
        else:
            self._show_msg("error", "快捷键修改失败", msg)
            log(f"快捷键修改失败已回滚: {spec} → {msg}")

    def _swap_hotkey(self, new_spec: str) -> "tuple[bool, str]":
        """注销旧键 → 试注册新键；失败回滚旧键。返回 (成功?, 人话消息)。"""
        old_spec = self.hotkey_spec
        if self.hotkey_mgr is not None:
            try:
                self.hotkey_mgr.stop()
            except Exception:  # noqa: BLE001
                pass
            self.hotkey_mgr = None
        try:
            self.hotkey_mgr = self._make_hotkey(new_spec)
        except Exception as e:  # noqa: BLE001 冲突/非法
            rollback_err = None
            try:
                self.hotkey_mgr = self._make_hotkey(old_spec)
            except Exception as e2:  # noqa: BLE001
                rollback_err = str(e2)
                self.hotkey_mgr = None
            if rollback_err:
                return False, (f"新快捷键 {new_spec} 注册失败（{e}），"
                               f"且旧键 {old_spec} 回滚也失败（{rollback_err}），"
                               "托盘截图仅菜单可用，重启程序可恢复")
            return False, f"新快捷键 {new_spec} 与其他程序冲突或非法：{e}\n已恢复原快捷键 {old_spec}。"
        self.hotkey_spec = new_spec
        return True, f"截图快捷键已改为 {new_spec}（立即生效，已写入设置）。"

    @staticmethod
    def validate_hotkey_spec(spec: str) -> "tuple[bool, str]":
        """纯校验（无副作用）：parse 通过即合法。非法返回 (False, 文案)。"""
        try:
            capture.parse_hotkey(spec)
            return True, ""
        except ValueError as e:
            return False, f"快捷键「{spec}」不可用：{e}"

    # ------------------------------------------------------------ 检查更新（M3c）

    def _on_check_update(self) -> None:
        """菜单点击才联网（8s 超时在 worker 线程，不卡主环）；平时零打扰。"""
        def _task():
            import update
            self.post("update_result", update.check_latest(__version__))
        threading.Thread(target=_task, daemon=True, name="snipeq-update").start()

    def _on_update_result(self, info: dict) -> None:
        status = info.get("status")
        if status == "new_version":
            import webbrowser
            if self._show_msg("okcancel", "发现新版本",
                              f"发现新版本 {info.get('latest')}（当前 v{__version__}）\n\n"
                              f"{info.get('name', '')}\n\n打开浏览器前往下载页？"):
                webbrowser.open(info.get("url") or "")
        elif status == "up_to_date":
            self._notify("检查更新", f"已是最新版本（v{__version__}）")
        else:
            kind = info.get("kind")
            if kind == "not_ready":
                self._notify("检查更新", "发布仓库尚未就绪（还没有正式版）")
            else:
                self._notify("检查更新", info.get("message", "检查失败"))

    # ------------------------------------------------------------ 主线程事件
    def _on_quit(self) -> None:
        self._stop.set()
        self._abort_capture.set()          # 若遮罩在途，热键层直接撤下
        if self.tkui is not None:
            try:
                self.tkui.jobs.put(("tk_quit",))
            except Exception:  # noqa: BLE001
                pass
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.quit()
                return
        except Exception:  # noqa: BLE001
            pass
        if self.root is not None:
            self.root.quit()

    def _on_toggle_zero_confirm(self) -> None:
        self.zero_confirm = not self.zero_confirm
        self.settings.set(zero_confirm=self.zero_confirm)
        try:
            self.settings.save()
        except Exception as e:  # noqa: BLE001
            log(f"settings 保存失败: {e}")
        if self.card is not None:
            try:
                self.card.set_zero_confirm(self.zero_confirm)
            except Exception:  # noqa: BLE001
                pass
        log(f"零确认模式: {'开' if self.zero_confirm else '关'}")

    def _on_toggle_unload(self) -> None:
        self.unload_idle_min = 0.0 if self.unload_idle_min > 0 else 10.0
        self.settings.set(unload_idle_min=self.unload_idle_min)
        try:
            self.settings.save()
        except Exception as e:  # noqa: BLE001
            log(f"settings 保存失败: {e}")
        log(f"空闲卸载: {'开（10min）' if self.unload_idle_min > 0 else '关'}")

    def _on_toggle_autostart(self) -> None:
        want = not bool(self.settings.get("autostart"))
        # set_autostart 写前先以日志打印目标命令（dry-run 语义内嵌于 log 行）
        actual = set_autostart(want, log=log)
        self.settings.set(autostart=actual)
        try:
            self.settings.save()
        except Exception as e:  # noqa: BLE001
            log(f"settings 保存失败: {e}")
        log(f"开机自启: {'开' if actual else '关'}")

    def _on_history(self, latex: str) -> None:
        """菜单点击历史条目 → 重灌卡片（降级/零确认则直写剪贴板）。"""
        self._on_result(latex, None, None, None)

    def _on_capture(self) -> None:
        """截图入口（事件环）。Qt 模式把遮罩工作下发 Tk 线程（结果经
        region_ready 事件回来）；降级模式主线程同步弹遮罩（旧行为）。
        防重入：遮罩在途时忽略新的 capture 事件。"""
        if self._capturing:
            log("截图已在进行中，忽略重入触发")
            return
        self._capturing = True
        # 兜底：150s 后若还没收到 region_ready（Tk 线程意外卡死），解锁截图态
        self._timer(150.0, lambda: self.post("capture_stuck"))
        if not self.ui_degraded and self.tkui is not None:
            self.tkui.jobs.put(("capture",))
            return
        try:
            factory = self.overlay_factory or capture.TkOverlay
            self._abort_capture.clear()
            img, rect = capture.capture_region(
                root=self.root, overlay_factory=factory,
                abort_event=self._abort_capture)
            if img is None:
                return
            path = os.path.join(tempfile.gettempdir(),
                                f"snipeq_shot_{int(time.time() * 1000)}.png")
            img.save(path)
            del img
            self._enqueue_pipeline(path, rect)
        except Exception:  # noqa: BLE001
            log("截图异常:\n" + traceback.format_exc())
            self._notify("截图失败", "capture 异常，见日志")
        finally:
            self._capturing = False

    def _on_capture_stuck(self) -> None:
        if self._capturing:
            log("截图态 150s 未返回 → 强制解锁（遮罩可能已被看门狗/abort 撤下）")
            self._abort_capture.set()
            self._capturing = False

    def _on_region_ready(self, png_path, rect) -> None:
        """Tk 线程截图完成（png_path=None 表示取消/超时/中止）。"""
        self._capturing = False
        if png_path:
            self._enqueue_pipeline(png_path, rect)

    def _enqueue_pipeline(self, image_path: str, rect) -> None:
        self.tasks.put(("recognize", image_path, rect))

    # ------------------------------------------------------------ 结果处理
    def _on_result(self, latex, err, rect, png, image_path: str | None = None) -> None:
        if err:
            log(f"管线失败: {err}")
            self._notify("识别失败", err[:120])
            return
        if self.zero_confirm or self.ui_degraded:
            self._copy_word_and_notify(latex)
            return
        card = self._ensure_card()
        if card is None:
            self._copy_word_and_notify(latex)
            return
        try:
            self._cur_latex = latex
            card.show_result(png, latex)
            if rect:
                # capture 输出物理像素 → show_near 契约逻辑像素（主屏 scale）
                card.show_near(*physical_to_logical_rect(rect, display_scale()))
            log(f"卡片已展示: {latex[:60]}")
            # 预览异步回填：卡片先文本降级，worker 渲完 PNG 再刷新（不阻塞出卡）
            if png is None and self.preview_enabled and latex:
                self.tasks.put(("reprerender", latex))
        except Exception:  # noqa: BLE001
            log("FormulaCard 展示异常，转降级直写:\n" + traceback.format_exc())
            self.ui_degraded = True
            self._copy_word_and_notify(latex)

    def _copy_word_and_notify(self, latex: str) -> None:
        try:
            info = write_clipboard_formula(latex)
            warn = f"（{len(info['warnings'])} 格式降级）" if info["warnings"] else ""
            self._notify("已复制 Word 公式", latex[:60] + warn)
            log(f"直写剪贴板: {latex}")
        except ClipboardError as e:
            self._notify("剪贴板写入失败", str(e)[:120])

    # ------------------------------------------------------------ 卡片
    def _ensure_card(self):
        if self.card is not None:
            return self.card
        try:
            self.card = FormulaCard(on_latex_edited=self._on_latex_edited)
            # des-1 契约信号（均无参，latex 由 _cur_latex 跟踪）
            self.card.copy_word.connect(self._card_copy_word)
            self.card.copy_latex.connect(self._card_copy_latex)
            self.card.cloud_enhance.connect(self._card_cloud)
            self.card.zero_confirm_toggled.connect(self._card_zero_toggled)
            self.card.close_requested.connect(self._card_closed)
        except Exception as e:  # noqa: BLE001
            log(f"FormulaCard 构造/接线失败，转降级直写: {e}")
            self.card = None
            self.ui_degraded = True
            return None
        return self.card

    def _on_latex_edited(self, text: str) -> None:
        """卡片编辑框 300ms 防抖回调：更新跟踪值并请求 worker 重渲染预览。"""
        self._cur_latex = text
        if self.preview_enabled:
            self.tasks.put(("reprerender", text))

    def _on_reprerender(self, text, png) -> None:
        if self.card is None or self._cur_latex != text:
            return
        try:
            self.card.show_result(png, text)  # show_result 程序化写入不回环
            if png:
                log(f"卡片预览回填: {png}")
        except Exception:  # noqa: BLE001
            log("重渲染展示失败:\n" + traceback.format_exc())

    def _card_copy_word(self) -> None:
        if self._cur_latex:
            self._copy_word_and_notify(self._cur_latex)
        self._hide_card()

    def _card_copy_latex(self) -> None:
        if self._cur_latex:
            write_clipboard_text(self._cur_latex)
            self._notify("已复制 LaTeX", self._cur_latex[:60])
        self._hide_card()

    def _card_cloud(self) -> None:
        # M2 占位：L2 云端增强未接入（v1.1 路由），卡片侧按钮已禁用
        self._notify("云端增强", "暂不可用（v1.1 规划）")

    def _card_zero_toggled(self, enabled: bool) -> None:
        self.zero_confirm = bool(enabled)
        log(f"零确认模式（卡片开关）: {'开' if self.zero_confirm else '关'}")

    def _card_closed(self) -> None:
        pass  # 卡片自行 hide

    def _hide_card(self) -> None:
        if self.card is not None:
            try:
                self.card.hide()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 气泡
    def _notify(self, title: str, msg: str) -> None:
        log(f"气泡[{title}] {msg}")
        if self.icon is not None:
            try:
                self.icon.notify(msg, title)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ worker 线程
    def worker_loop(self) -> None:
        idle_check_s = 30.0
        while not self._stop.is_set():
            try:
                task = self.tasks.get(timeout=idle_check_s)
            except queue.Empty:
                self._maybe_unload()
                continue
            kind = task[0]
            if kind == "exit":
                break
            try:
                if kind == "recognize":
                    _, image_path, rect = task
                    latex, err = self._recognize_pipeline(image_path)
                    if latex is not None and self.settings.get("history_enabled", True):
                        self.history.append(latex, src=os.path.basename(image_path),
                                            sha1=image_sha1(image_path))
                    # 预览渲染（Edge ~1s）不阻塞出卡/写剪贴板；PNG 由卡片路径
                    # _on_result→reprerender 任务异步回填（降级/零确认不需要）。
                    self.post("result", latex, err, rect, None, image_path)
                elif kind == "reprerender":
                    png = self._render_preview(task[1])
                    self.post("reprerender", task[1], png)
            except Exception as e:  # noqa: BLE001
                # P0 崩溃防护：任务级异常也要把结果投回主环（错误可见，绝不静默）
                log("worker 异常:\n" + traceback.format_exc())
                if kind == "recognize":
                    self.post("result", None, f"识别线程异常: {e}",
                              task[2] if len(task) > 2 else None, None,
                              task[1] if len(task) > 1 else "")
                elif kind == "reprerender":
                    self.post("reprerender", task[1], None)
            self._maybe_unload()
        with self._preview_lock:
            if self.preview is not None:
                self.preview.close()
                self.preview = None

    def _get_preview(self):
        with self._preview_lock:
            if self.preview is None:
                from preview import PreviewRenderer
                self.preview = PreviewRenderer()
            return self.preview

    def _render_preview(self, latex: str) -> str | None:
        if not self.preview_enabled or not latex:
            return None
        pv = self._get_preview()
        png = pv.render(latex)
        if png is None:
            log(f"预览渲染失败（卡片降级纯文本）: {pv.last_error}")
        return png

    def _recognize_pipeline(self, image_path: str):
        """识别 + 归一化 → (latex|None, err|None)。仅 worker 线程调用（独占 self.rec）。"""
        try:
            from recognizer import FormulaRecognizer
            if self.rec is None:
                self.rec = FormulaRecognizer()
                log("模型已懒加载")
            raw, infer_s = self.rec.recognize(image_path)
            self.rec_last_use = time.monotonic()
            latex, _rules = normalize(raw)
            log(f"识别完成 {infer_s:.2f}s: {latex}")
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
        return latex, None

    def _maybe_unload(self) -> None:
        if self.unload_idle_min <= 0 or self.rec is None:
            return
        if time.monotonic() - self.rec_last_use > self.unload_idle_min * 60:
            self.rec = None
            gc.collect()
            log("模型空闲卸载完成")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tray_app", description="SnipEq 托盘主程序")
    ap.add_argument("--hotkey", default=None,
                    help="覆盖 settings.json 的全局热键（默认取配置，再默认 ctrl+alt+a）")
    ap.add_argument("--zero-confirm", action="store_true", default=None,
                    help="零确认模式启动即开（否则取 settings.json）")
    ap.add_argument("--unload-idle-min", type=float, default=None,
                    help="空闲 N 分钟卸载模型，0=从不卸载（默认取 settings.json）")
    ap.add_argument("--no-preview", action="store_true",
                    help="关闭 KaTeX 预览渲染")
    ap.add_argument("--once-image", metavar="PATH",
                    help="冒烟：跳过热键/框选，直接对图片跑管线")
    ap.add_argument("--smoke", type=float, default=0.0, metavar="SECONDS",
                    help="冒烟：运行 N 秒后自动退出")
    args = ap.parse_args(argv)

    app = SnipEqApp(
        hotkey=args.hotkey,
        zero_confirm=True if args.zero_confirm else None,
        unload_idle_min=args.unload_idle_min,
        preview_enabled=False if args.no_preview else None,
    )
    worker = threading.Thread(target=app.worker_loop, daemon=True, name="snipeq-worker")
    worker.start()
    try:
        rc = app.run(smoke_seconds=args.smoke, once_image=args.once_image)
    finally:
        app._stop.set()
        app.tasks.put(("exit",))
        worker.join(timeout=5)
    if args.smoke:
        print("SMOKE OK")
    return rc


if __name__ == "__main__":
    sys.exit(main())
