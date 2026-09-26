r"""soak_capture.py — P0-B 内存增长治理验证：30 轮完整"截图→识别→归一化→剪贴板→预览"。

断言（M3e 修复口径）：
  1) RSS 增幅 < 50MB（预热 2 轮后取基线，末值-基线；模型常驻 700MB 为设计值不计入）
  2) 结束时 msedge（按 --user-data-dir=snipeq_preview 特征过滤）残留进程 = 0
  3) 结束时 node（preview_katex.js）残留 = 0（PreviewRenderer.close 后）
  4) 进程句柄数增长 < 200（GetProcessHandleCount，防句柄泄漏）

对比口径：修复前同类循环的已知形态——每轮 Edge 子进程若未被跟踪回收则
进程/句柄线性增长；全屏冻结图 ~30MB/张若被 TkPhotoImage/引用链滞留则 RSS 阶梯上升。
"""
from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROUNDS = 30
WARMUP = 2
RSS_BUDGET_MB = 50
HANDLE_BUDGET = 200


def rss_mb() -> float:
    """当前进程工作集（MB）。Windows PSAPI。"""
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    gpm = ctypes.windll.psapi.GetProcessMemoryInfo
    gpm.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), wintypes.DWORD]
    if not gpm(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        return float("nan")
    return pmc.WorkingSetSize / 1e6


def handle_count() -> int:
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    gph = k32.GetProcessHandleCount
    gph.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    n = wintypes.DWORD()
    gph(k32.GetCurrentProcess(), ctypes.byref(n))
    return int(n.value)


def count_procs(pattern: str) -> int:
    """按命令行特征计数外部进程（msedge 用 --user-data-dir 路径、node 用脚本名）。
    排除计数器自身（pwsh/cmd 命令行里就带着 pattern，否则会自我匹配计成 1）。"""
    ps = ("Get-CimInstance Win32_Process | "
          "Where-Object { $_.Name -notmatch '^(pwsh|powershell|cmd)\\.exe$' } | "
          f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | Measure-Object | "
          "Select-Object -ExpandProperty Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=60)
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return -1


def main() -> int:
    print(f"== soak_capture：{ROUNDS} 轮完整管线（模型常驻另计）", flush=True)
    os.environ.setdefault("SNIPEQ_NO_NODE", "1")   # 走便携包同口径的浏览器路线

    from PIL import Image
    import capture
    from recognizer import FormulaRecognizer
    from normalize import normalize
    from mathml import latex_to_mathml  # noqa: F401
    from clipboard_writer import write_clipboard_formula
    from preview import PreviewRenderer

    img_path = str(HERE / "images" / "frac_sum.png")
    rec = FormulaRecognizer()
    rec.recognize(img_path)          # 预热模型（load ~700MB 设计值）
    pv = PreviewRenderer()

    fake_full = Image.new("RGB", (1280, 720), "white")

    samples = []
    edge_mid = -1
    for i in range(ROUNDS):
        # 截图链 plumbing（30 轮弹真实全屏遮罩不可行；full_image_factory 走
        # capture_region/选区同一坐标-裁剪-释放语义，无 UI）
        frozen, offset = fake_full, (0, 0)
        box = capture.full_image_factory(frozen, offset, None).run()
        sel = frozen.crop(box)
        del frozen, box
        # 识别 + 归一化 + 剪贴板 + 预览
        raw, _ = rec.recognize(img_path)
        latex, _ = normalize(raw)
        write_clipboard_formula(latex)
        png = pv.render(latex)
        del sel, raw
        if png:
            os.remove(png)
        gc.collect()
        if i >= WARMUP - 1:
            samples.append((i + 1, rss_mb(), handle_count()))
            if i == WARMUP + 8:
                edge_mid = count_procs("snipeq_preview")
            if (i + 1) % 10 == 0:
                print(f"  round {i+1:2d}: RSS={samples[-1][1]:.0f}MB "
                      f"handles={samples[-1][2]}", flush=True)

    pv.close()                        # node 常驻应退出
    gc.collect()
    time.sleep(1.5)

    base = samples[0][1]
    final = samples[-1][1]
    dh0, dh1 = samples[0][2], samples[-1][2]
    edge_after = count_procs("snipeq_preview")
    node_after = count_procs("preview_katex.js")
    print("\n---- soak 结论 ----")
    print(f"RSS: 基线(第{samples[0][0]}轮)={base:.0f}MB 末值={final:.0f}MB "
          f"增幅={final-base:+.0f}MB（预算 <{RSS_BUDGET_MB}MB）")
    print(f"句柄: {dh0} → {dh1}（增长 {dh1-dh0:+}，预算 <{HANDLE_BUDGET}）")
    print(f"Edge 残留(第10轮中测={edge_mid}) 末测={edge_after}（要求 0）")
    print(f"node 残留={node_after}（要求 0）")
    probs = []
    if final - base >= RSS_BUDGET_MB:
        probs.append(f"RSS 增幅 {final-base:.0f}MB 超预算")
    if dh1 - dh0 >= HANDLE_BUDGET:
        probs.append(f"句柄增长 {dh1-dh0} 超预算")
    if edge_after != 0:
        probs.append(f"Edge 残留 {edge_after} 个")
    if node_after != 0:
        probs.append(f"node 残留 {node_after} 个")
    if probs:
        print("SOAK FAIL:", "; ".join(probs))
        return 1
    print("SOAK PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
