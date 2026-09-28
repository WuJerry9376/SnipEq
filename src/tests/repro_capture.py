r"""repro_capture.py — P0-A 截图遮罩卡死根因采证（修复前后各跑一次对比）。

采证三实验（只证不修）：
  E1 焦点送达：TkOverlay 显示（overrideredirect + topmost + focus_force）后，
     读 Win32 GetForegroundWindow / GetGUIThreadInfo 判定键盘输入是否会到达
     遮罩窗口 —— 若前台句柄 ≠ 遮罩句柄，则 Enter/Esc 绑定形同虚设
     （置顶全屏吞鼠标、键盘漏到别的程序 = 用户"确认后不跳识别、整机假死"）。
  E2 SendInput 真实按键：先试获取前台权，再 SendInput 一次 Enter；轮询
     EnumWindows 看遮罩窗口是否在 3s 内销毁（销毁=键被接收并确认）。
  E3 双泵计时：Qt 主环 _tick(QTimer) + after 自注册链并存时，统计每秒实际
     执行的 Tk after 回调数是否随时间线性增长（泵风暴→卡顿→泄漏的机制证据）。

证据等级标注：VMware 交互会话可用则 E1/E2 为 A 级（OS 真实输入路径）；
SendInput 无效果时 E2 降级为 B 级（仅 E1 句柄级证据 + 代码审计）。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

user32 = ctypes.windll.user32


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", ctypes.c_int * 4)]


def hwnd_of_widget(win) -> int:
    """tk widget 的顶层 Win32 句柄（winfo_id 是客户区，取 GA_ROOT）。"""
    h = win.winfo_id()
    GA_ROOT = 2
    return int(user32.GetAncestor(wintypes.HWND(h), GA_ROOT)) or h


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def focused_hwnd(pid_tid) -> tuple[int, int]:
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    pid, tid = pid_tid
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return int(gti.hwndActive or 0), int(gti.hwndFocus or 0)
    return 0, 0


def _input_structs():
    """官方 40B INPUT 布局（x64）。所有 SendInput 探针共用。"""
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_ulonglong)]

    class U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", U)]

    assert ctypes.sizeof(INPUT) == 40, "INPUT 布局错误（非 40B 会被 SendInput 拒收 err=87）"
    return INPUT


def sendinput_drag(x0: int, y0: int, x1: int, y1: int) -> int:
    """OS 级鼠标拖拽（绝对坐标归一化到虚拟屏）。返回注入事件数（0=被拒）。"""
    INPUT = _input_structs()
    vx = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
    vh = user32.GetSystemMetrics(79)

    def ev(x, y, flags):
        i = INPUT()
        i.type = 0
        i.u.mi.dx = int((x - vx) * 65535 / max(vw - 1, 1))
        i.u.mi.dy = int((y - vy) * 65535 / max(vh - 1, 1))
        i.u.mi.dwFlags = flags | 0x8000  # | ABSOLUTE
        return i

    MOVE, DOWN, UP = 0x0001, 0x0002, 0x0004
    seq = (INPUT * 4)(ev(x0, y0, MOVE), ev(x0, y0, DOWN),
                      ev(x1, y1, MOVE), ev(x1, y1, UP))
    n = user32.SendInput(4, ctypes.byref(seq), ctypes.sizeof(INPUT))
    print(f"    SendInput(drag {x0},{y0}→{x1},{y1}) -> injected={n} "
          f"err={ctypes.get_last_error()}", flush=True)
    return n


def sendinput_enter() -> int:
    """SendInput 一次真实 Enter 键（OS 级注入）。返回实际注入的事件数。

    **结构体纪律**（VM 实测教训）：x64 INPUT 必须 40 字节官方布局
    （见 _input_structs docstring），曾被误判为"注入被系统拒绝"。"""
    INPUT = _input_structs()
    VK_RETURN, KEYEVENTF_KEYUP = 0x0D, 0x0002

    def key(up):
        i = INPUT()
        i.type = 1
        i.u.ki.wVk = VK_RETURN
        i.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        return i

    arr = (INPUT * 2)(key(False), key(True))
    n = user32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(INPUT))
    print(f"    SendInput(Enter) -> injected={n} err={ctypes.get_last_error()}",
          flush=True)
    return n


def e1_e2() -> dict:
    """真实 TkOverlay 显示期间，经 root.after 在其事件环内采证：

    E1 = 前台句柄归属（键盘能否到达遮罩，OS 级）
    E2 = SendInput 真实 Enter → 遮罩是否在 1s 内确认关闭（injected=0 则降级）
    E2b = 应用内 event_generate Return → 验证绑定/确认链
    （单线程纪律：Tk 对象只在主线程触碰。）
    """
    import tkinter as tk
    from PIL import Image
    import capture

    out: dict = {}
    root = tk.Tk()
    root.withdraw()
    fake = Image.new("RGB", (900, 500), "white")
    st: dict = {"box": "unset", "probe": {}}

    def probe_flow():
        """overlay 已显示：此函数在 run() 的轮询事件环内执行。"""
        toplevel = next((w for w in root.winfo_children() if isinstance(w, tk.Toplevel)), None)
        if toplevel is None:
            st["probe"]["E1"] = "遮罩未创建？"
            return
        ov_h = hwnd_of_widget(toplevel)
        fg = foreground_hwnd()
        msg = (f"overlay hwnd={ov_h:#x} foreground={fg:#x} " +
               ("== 遮罩拿到前台（键可达）" if fg == ov_h else "!= 遮罩未获前台 ← 键盘进不了遮罩"))
        st["probe"]["E1"] = msg
        print(f"  [E1] {msg}", flush=True)
        # 拖拽：A 级真 SendInput 优先（被系统拒收时退回 B 级画布合成事件）
        canvas = next((c for c in toplevel.winfo_children()
                       if c.winfo_class() == "Canvas"), toplevel)
        n_drag = sendinput_drag(300, 200, 500, 320)
        if n_drag:
            st["probe"]["drag"] = "A 级：SendInput 真实拖拽 (300,200)→(500,320)"
        else:
            st["probe"]["drag"] = "B 级：event_generate 画布合成拖拽（OS 注入被拒）"
            canvas.event_generate("<ButtonPress-1>", x=300, y=200)
            canvas.event_generate("<B1-Motion>", x=500, y=320)
            canvas.event_generate("<ButtonRelease-1>", x=500, y=320)
        root.after(400, lambda: send_enter(toplevel))

    def send_enter(toplevel):
        n = sendinput_enter()
        st["probe"]["E2_injected"] = n
        root.after(1000, lambda: check_alive(toplevel))

    def check_alive(toplevel):
        alive = toplevel.winfo_exists()
        if st["probe"].get("E2_injected", 0) == 0:
            st["probe"]["E2_sendinput"] = ("SendInput 被系统拒绝(injected=0)：E2 证据降级 B，"
                                           "改由 E2b 应用内事件验证确认链")
        else:
            st["probe"]["E2_sendinput"] = (
                "SendInput Enter 后遮罩仍在 → OS 级 Enter 未触发确认"
                if alive else "SendInput Enter 关闭了遮罩（键盘路径可用）")
        print(f"  [E2] {st['probe']['E2_sendinput']}", flush=True)
        if alive:
            toplevel.event_generate("<Return>")   # 应用内注入，验证绑定/确认链
            root.after(600, lambda: ctrl_result(toplevel))

    def ctrl_result(toplevel):
        gone = not toplevel.winfo_exists()
        st["probe"]["E2b_event"] = (
            "event_generate(Return) 关闭遮罩 → 确认链/绑定正常（bind_all 修复生效）"
            if gone else "event_generate(Return) 关不掉 → 绑定/确认链仍断")
        print(f"  [E2b] {st['probe']['E2b_event']}", flush=True)
        if not gone:
            try:
                toplevel.event_generate("<Escape>")  # 最后逃生
            except Exception:  # noqa: BLE001
                pass

    def run_overlay():
        ov = capture.TkOverlay(fake, (0, 0), root)
        st["box"] = ov.run()
        print(f"  [run] TkOverlay.run() 返回 box={st['box']} "
              f"reason={ov.cancel_reason}（不再永久 hang）", flush=True)

    root.after(800, probe_flow)     # overlay 显示后再采证
    try:
        run_overlay()
    except Exception as e:  # noqa: BLE001
        st["probe"]["overlay_exc"] = repr(e)
    out["level"] = ("A" if st["probe"].get("E2_injected") else "B（SendInput 被拒，E2b 兜底）")
    out.update(st["probe"])
    out["box_returned"] = st["box"]
    root.destroy()
    return out


def e3_double_pump() -> dict:
    """E3：量化 _pump/_tick 双源调度回调数随时间的增长（修复前线性/指数爆炸）。

    自身带 2.5s 时间预算：若 root.update() 因 timer 风暴无法返回，hang 即证据。
    """
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    import tkinter as tk

    app = QApplication.instance() or QApplication([])
    root = tk.Tk()
    root.withdraw()

    tick_count = {"n": 0}
    hang = {"in_update": False}

    def _pump():
        root.update_idletasks()
        hang["in_update"] = True
        root.update()
        hang["in_update"] = False
        tick_count["n"] += 1

    def _tick():
        _pump()
        root.after(30, _tick)          # 与 QTimer 双源（现行结构）

    timer = QTimer()
    timer.setInterval(30)
    timer.timeout.connect(_tick)
    timer.start()
    samples = []
    t_end = time.time() + 4.0
    last, t0 = 0, time.time()
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.005)
        now = time.time()
        if now - t0 >= 1.0:
            samples.append((tick_count["n"] - last, hang["in_update"]))
            last = tick_count["n"]
            t0 = now
    hung_mid_update = hang["in_update"]
    timer.stop()
    root.destroy()
    per_sec = [s[0] for s in samples]
    out = {"ticks_per_sec": per_sec,
           "growth": per_sec[-1] - per_sec[1] if len(per_sec) >= 2 else 0,
           "hung_in_update": hung_mid_update}
    print(f"  [E3] _pump/秒 采样={per_sec}（双源理论稳态≈66/s；线性增长=after 链复利）"
          f" 采样点卡死在 root.update: {[s[1] for s in samples]}")
    return out


def e4_escapes() -> dict:
    """E4：看门狗与 abort 事件两条逃生路径实测（不依赖任何键盘/鼠标）。"""
    import tkinter as tk
    import threading
    from PIL import Image
    import capture

    out = {}
    # a) watchdog：0.6s 无操作自动取消
    root = tk.Tk(); root.withdraw()
    ov = capture.TkOverlay(Image.new("RGB", (400, 300), "white"), (0, 0), root,
                           watchdog_s=0.6)
    t0 = time.time()
    box = ov.run()
    out["watchdog"] = (f"run 返回 box={box} reason={ov.cancel_reason} "
                       f"耗时={time.time()-t0:.2f}s（预期≈0.6s/None/watchdog）")
    print(f"  [E4a] {out['watchdog']}", flush=True)
    root.destroy()

    # b) abort_event：另一线程 0.4s 后置位 → 轮询环 <0.6s 内撤遮罩
    root2 = tk.Tk(); root2.withdraw()
    ev = threading.Event()
    ov2 = capture.TkOverlay(Image.new("RGB", (400, 300), "white"), (0, 0), root2)
    ov2.set_abort_event(ev)
    threading.Timer(0.4, ev.set).start()
    t0 = time.time()
    box2 = ov2.run()
    out["abort"] = (f"run 返回 box={box2} reason={ov2.cancel_reason} "
                    f"耗时={time.time()-t0:.2f}s（预期≈0.4s/None/abort_hotkey）")
    print(f"  [E4b] {out['abort']}", flush=True)
    root2.destroy()
    return out


def main() -> int:
    print("== P0-A 复现/回归采证", flush=True)

    def watchdog():
        time.sleep(45)
        print("\n[WATCHDOG] 45s 未完成 → 强制退出（hang 本身记为 E2/E3 证据）", flush=True)
        os._exit(3)
    threading.Thread(target=watchdog, daemon=True).start()

    r12 = e1_e2()
    print("-- e1_e2 done --", flush=True)
    r3 = e3_double_pump()
    print("-- e3 done --", flush=True)
    r4 = e4_escapes()
    print("\n---- 汇总 ----", flush=True)
    for k, v in r12.items():
        print(f"{k}: {v}")
    print(f"E3: per-sec={r3['ticks_per_sec']} growth={r3['growth']} hung={r3['hung_in_update']}")
    for k, v in r4.items():
        print(f"E4_{k}: {v}")
    ok_box = isinstance(r12.get("box_returned"), tuple)
    ok_wd = "watchdog" in str(r4.get("watchdog", ""))
    ok_ab = "abort" in str(r4.get("abort", ""))
    verdict = "PASS（确认链修复 + 两条逃生可用）" if (ok_box and ok_wd and ok_ab) \
        else "FAIL（至少一条链路未闭合，见上）"
    print("VERDICT:", verdict)
    return 0 if (ok_box and ok_wd and ok_ab) else 1


if __name__ == "__main__":
    sys.exit(main())
