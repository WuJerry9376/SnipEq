"""FormulaCard 独立演示 / 自检 / 截图（不依赖 fix-5 的任何模块）。

用法（工作目录 src/）：
    python -m ui.demo              # 循环演示：3 条公式 + 降级态，show_result/show_near/编辑回调
    python -m ui.demo --selftest   # 契约行为自检（信号、防抖、翻边、键盘），全过退出 0
    python -m ui.demo --shots      # 生成明/暗主题截图到 src/ui/shots/
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QLinearGradient, QFont, QPainter, QPen, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton

from ui.formula_card import FormulaCard

UI_DIR = Path(__file__).resolve().parent
TMP_DIR = UI_DIR / "_demo_tmp"
SHOTS_DIR = UI_DIR / "shots"

# ---------------------------------------------------------------- 假公式图
# token: ("text", s) | ("sup", s) | ("frac", num, den) | ("cases", [lines])
FORMULAS: list[tuple[list, str]] = [
    (
        [("text", "\u222b\u2080"), ("sup", "\u221e"), ("text", " e"),
         ("sup", "\u2212x\u00b2"), ("text", " dx = "), ("frac", "\u221a\u03c0", "2")],
        r"\int_{0}^{\infty} e^{-x^{2}}\,\mathrm{d}x=\frac{\sqrt{\pi}}{2}",
    ),
    (
        [("text", "\u2207\u00b7E = "), ("frac", "\u03c1", "\u03b5\u2080")],
        r"\nabla\cdot\mathbf{E}=\frac{\rho}{\varepsilon_{0}}",
    ),
    (
        [("text", "f(x) = "), ("cases", ["x + y = 1", "x \u2212 y = 3"])],
        r"f(x)=\begin{cases}x+y=1\\x-y=3\end{cases}",
    ),
    (
        # 长公式样本（LaTeX >80 字符），"fit" 自动缩排至画布宽
        [("fit", "\u2112 = \u03a3\u1d62 [ y\u1d62 log \u0177\u1d62 + (1\u2212y\u1d62) log(1\u2212\u0177\u1d62) ] "
                 "+ \u03bb\u2016w\u2016\u2082\u00b2 + \u2202\u03b1/\u2202t "
                 "\u2212 \u2207\u00b7(\u03ba\u2207\u03b1) = 0")],
        r"\mathcal{L}=\sum_{i=1}^{N}\left[\,y_i\log\hat{y}_i+(1-y_i)\log(1-\hat{y}_i)\,\right]"
        r"+\lambda\|\mathbf{w}\|_2^2+\frac{\partial\alpha}{\partial t}-\nabla\cdot(\kappa\nabla\alpha)=0",
    ),
]


def _mkfont(size: float) -> QFont:
    f = QFont("Cambria Math", size)
    f.setItalic(True)
    return f


def _draw_tokens(p: QPainter, tokens: list, x: float, cy: float,
                 size: float) -> float:
    """在 (x, cy 垂直中心) 画 token 序列，返回结束 x。"""
    for tok in tokens:
        kind = tok[0]
        if kind == "text":
            p.setFont(_mkfont(size))
            fm = p.fontMetrics()
            p.drawText(int(x), int(cy + (fm.ascent() - fm.descent()) / 2), tok[1])
            x += fm.horizontalAdvance(tok[1]) + 1
        elif kind == "sup":
            p.setFont(_mkfont(size * 0.62))
            fm2 = p.fontMetrics()
            p.drawText(int(x), int(cy - size * 0.32), tok[1])
            x += fm2.horizontalAdvance(tok[1]) + 1
        elif kind == "frac":
            num, den = tok[1], tok[2]
            p.setFont(_mkfont(size * 0.72))
            fm2 = p.fontMetrics()
            w = max(fm2.horizontalAdvance(num), fm2.horizontalAdvance(den)) + 8
            p.drawText(int(x + (w - fm2.horizontalAdvance(num)) / 2),
                       int(cy - size * 0.16), num)
            p.drawText(int(x + (w - fm2.horizontalAdvance(den)) / 2),
                       int(cy + size * 0.68), den)
            pen = QPen(p.pen().color())
            pen.setWidthF(max(1.0, size * 0.05))
            p.setPen(pen)
            p.drawLine(int(x + 2), int(cy + size * 0.20),
                       int(x + w - 2), int(cy + size * 0.20))
            x += w + 5
        elif kind == "cases":
            lines = tok[1]
            p.setFont(_mkfont(size))
            fm3 = p.fontMetrics()
            lh = fm3.height() + 4
            h = lh * len(lines)
            w = max(fm3.horizontalAdvance(s) for s in lines)
            bx = x + 4
            pen = QPen(p.pen().color())
            pen.setWidthF(max(1.2, size * 0.06))
            p.setPen(pen)
            p.drawArc(int(bx), int(cy - h / 2), 12, int(h / 2) + 2, 0, 180 * 16)
            p.drawArc(int(bx), int(cy - 2), 12, int(h / 2) + 2, 180 * 16, 180 * 16)
            p.setPen(QColor("#101114"))
            y = cy - h / 2 + lh / 2
            p.setFont(_mkfont(size))
            for s in lines:
                p.drawText(int(bx + 18), int(y + (fm3.ascent() - fm3.descent()) / 2), s)
                y += lh
            x = bx + 18 + w + 8
        elif kind == "fit":
            # 单行长文本：从 size 逐步缩到能放进画布剩余宽度
            s = tok[1]
            right = p.window().width() - 40
            sz = size
            while sz > size * 0.30:
                p.setFont(_mkfont(sz))
                if p.fontMetrics().horizontalAdvance(s) <= right - x:
                    break
                sz *= 0.88
            fm = p.fontMetrics()
            p.drawText(int(x), int(cy + (fm.ascent() - fm.descent()) / 2), s)
            x += fm.horizontalAdvance(s) + 1
        else:
            raise ValueError(kind)
    return x


def render_formula_png(tokens: list, tag: str) -> str:
    """把 token 序列画成白底黑墨 PNG（2x 物理像素），返回文件路径。"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    scale = 2
    w, h, size = 860, 170, 27
    pm = QPixmap(w * scale, h * scale)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(QColor("#101114"))
    _draw_tokens(p, list(tokens), 50, h * scale / 2 - 14, size * scale)
    p.end()
    path = TMP_DIR / f"formula_{tag}.png"
    pm.save(str(path), "PNG")
    return str(path)


def render_regressed_png(latex: str) -> str:
    """模拟集成方按编辑后 LaTeX 重渲染：等宽直排源文。"""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    scale = 2
    pm = QPixmap(860 * scale, 110 * scale)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(QColor("#101114"))
    p.setFont(QFont("Cascadia Mono", 17))
    p.drawText(pm.rect().adjusted(24 * scale, 0, -24 * scale, 0),
               Qt.AlignVCenter | Qt.AlignLeft, f"rendered: {latex}")
    p.end()
    path = TMP_DIR / "regressed_last.png"
    pm.save(str(path), "PNG")
    return str(path)


# ---------------------------------------------------------------- 演示外壳
_card_ref: dict = {}


def make_card(log: list[str]) -> FormulaCard:
    def on_edited(text: str) -> None:
        log.append(f"on_latex_edited:{text}")
        card = _card_ref.get("card")
        if card is not None:
            # 模拟集成方：按新 LaTeX 重渲染预览，再次 show_result
            card.show_result(render_regressed_png(text), text)

    card = FormulaCard(on_latex_edited=on_edited)
    _card_ref["card"] = card
    card.copy_word.connect(lambda: log.append("signal:copy_word"))
    card.copy_latex.connect(lambda: log.append("signal:copy_latex"))
    card.zero_confirm_toggled.connect(
        lambda on: log.append(f"signal:zero_confirm_toggled({on})"))
    card.close_requested.connect(lambda: log.append("signal:close_requested"))
    return card


def wait_ms(ms: int) -> None:
    app = QGuiApplication.instance()
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()


def selection_cases(avail: QRect) -> list[QRect]:
    """四个假选区：常规右下 / 贴右缘 / 贴下缘 / 贴右下角。"""
    L, T, W, H = avail.x(), avail.y(), avail.width(), avail.height()
    return [
        QRect(L + W // 3, T + H // 6, 320, 110),
        QRect(L + W - 380, T + H // 4, 320, 110),
        QRect(L + W // 4, T + H - 220, 320, 110),
        QRect(L + W - 400, T + H - 200, 320, 110),
    ]


# ---------------------------------------------------------------- 循环演示
def run_loop(app: QApplication) -> int:
    print("SnipEq FormulaCard demo：每 2.6s 换一条公式并换位弹出。")
    print("试试：编辑框改 LaTeX（300ms 防抖后模拟重渲染）、Enter 复制、Esc 关闭、勾选零确认。")
    avail = QGuiApplication.primaryScreen().availableGeometry()
    log: list[str] = []
    card = make_card(log)
    images = [render_formula_png(t, str(i)) for i, (t, _) in enumerate(FORMULAS)]
    cases = selection_cases(avail)
    total = len(FORMULAS) + 1  # 末轮演示降级态
    state = {"i": 0}

    def tick() -> None:
        i = state["i"]
        sel = cases[i % len(cases)]
        card.show_near(sel.x(), sel.y(), sel.width(), sel.height())
        if i < len(FORMULAS):
            card.show_result(images[i], FORMULAS[i][1])
        else:
            card.show_result(None, FORMULAS[0][1])
        print(f"[demo] cycle {i}: selection={sel.getRect()} "
              f"image={'None(降级)' if i >= len(FORMULAS) else 'png'}")
        state["i"] = (i + 1) % total

    t = QTimer()
    t.timeout.connect(tick)
    tick()
    t.start(2600)
    return app.exec()


# ---------------------------------------------------------------- 自检
def run_selftest(app: QApplication) -> int:
    log: list[str] = []
    results: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")

    avail = QGuiApplication.primaryScreen().availableGeometry()
    card = make_card(log)
    img = render_formula_png(FORMULAS[0][0], "st")

    # 1. 弹出与摆放
    sel = QRect(avail.x() + 200, avail.y() + 150, 320, 110)
    card.show_near(sel.x(), sel.y(), sel.width(), sel.height())
    card.show_result(img, FORMULAS[0][1])
    wait_ms(320)
    flags = card.windowFlags()
    check("无边框/置顶/Tool 窗口",
          bool(flags & Qt.FramelessWindowHint)
          and bool(flags & Qt.WindowStaysOnTopHint)
          and bool(flags & Qt.Tool))
    check("卡片可见", card.isVisible())
    check("右下侧摆放", card.x() > sel.x() + sel.width()
          and card.y() > sel.y() + sel.height() - 20)
    check("预览为图片模式", card._preview.property("mode") == "image")
    check("LaTeX 框同步", card._latex_edit.text() == FORMULAS[0][1])

    # 2. 翻边
    sel2 = QRect(avail.right() - 370, avail.y() + 120, 320, 110)
    card.show_near(sel2.x(), sel2.y(), sel2.width(), sel2.height())
    check("贴右缘翻到左侧", card.x() + card.width() <= sel2.x() + 2)
    sel3 = QRect(avail.x() + 100, avail.bottom() - 190, 320, 110)
    card.show_near(sel3.x(), sel3.y(), sel3.width(), sel3.height())
    check("贴下缘翻到上方", card.y() + card.height() <= sel3.y() + 2)
    inside = (avail.left() <= card.x()
              and card.x() + card.width() <= avail.right() + 1
              and avail.top() <= card.y()
              and card.y() + card.height() <= avail.bottom() + 1)
    check("翻边后仍在工作区内", inside)

    # 3. 编辑防抖回调
    log.clear()
    card._latex_edit.insert(" \\!")
    wait_ms(150)
    early = any(s.startswith("on_latex_edited") for s in log)
    wait_ms(400)
    fired = [s for s in log if s.startswith("on_latex_edited")]
    check("编辑触发回调（300ms 防抖）", bool(fired) and not early)
    check("重渲染回灌不触发回环", len(fired) == 1)

    # 4. 键盘与按钮信号
    log.clear()
    QTest.keyClick(card, Qt.Key_Return)
    wait_ms(60)
    check("Enter 触发 copy_word", "signal:copy_word" in log)
    log.clear()
    card._btn_word.click()
    card._btn_latex.click()
    check("按钮触发 copy_word / copy_latex",
          log == ["signal:copy_word", "signal:copy_latex"])
    check("云端增强禁用 + tooltip 逐字",
          not card._btn_cloud.isEnabled()
          and card._btn_cloud.toolTip() == "难例升档 v1.1 提供")

    log.clear()
    card._zero_check.click()
    check("zero_confirm_toggled(True)",
          "signal:zero_confirm_toggled(True)" in log)
    log.clear()
    card.set_zero_confirm(False)
    check("set_zero_confirm 不发信号", log == [])

    # 5. 降级与关闭
    card.show_result(None, r"E=mc^2")
    wait_ms(120)
    check("image_png=None 降级显示 LaTeX 原文",
          card._preview.property("mode") == "text"
          and card._preview.text() == r"E=mc^2")
    log.clear()
    QTest.keyClick(card, Qt.Key_Escape)
    wait_ms(60)
    check("Esc 关闭 + close_requested",
          (not card.isVisible()) and "signal:close_requested" in log)
    card.show_result(img, FORMULAS[0][1])
    wait_ms(200)
    log.clear()
    card._body.findChild(QToolButton).click()
    check("× 关闭 + close_requested",
          (not card.isVisible()) and "signal:close_requested" in log)

    failed = [n for n, ok in results if not ok]
    print(f"\nselftest: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


# ---------------------------------------------------------------- 截图
def _compose_shot(card: FormulaCard, sel: QRect, dark: bool) -> QPixmap:
    avail = QGuiApplication.primaryScreen().availableGeometry()
    wait_ms(340)  # 等淡入结束
    canvas = QPixmap(avail.width() * 2, avail.height() * 2)
    canvas.setDevicePixelRatio(2)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, canvas.width(), canvas.height())
    if dark:
        g.setColorAt(0, QColor("#2b2e35"))
        g.setColorAt(1, QColor("#1c1e24"))
    else:
        g.setColorAt(0, QColor("#e3e7ee"))
        g.setColorAt(1, QColor("#cdd3dd"))
    p.fillRect(canvas.rect(), g)
    pen = QPen(QColor("#4d82f0"), 2, Qt.DashLine)
    p.setPen(pen)
    p.drawRect(sel.x() - avail.x(), sel.y() - avail.y(),
               sel.width(), sel.height())
    p.drawPixmap(card.x() - avail.x(), card.y() - avail.y(), card.grab())
    p.end()
    return canvas


def run_shots(app: QApplication) -> int:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    hints = QGuiApplication.styleHints()
    avail = QGuiApplication.primaryScreen().availableGeometry()
    img = render_formula_png(FORMULAS[0][0], "shot")

    def shoot(name: str, dark: bool, degraded: bool) -> None:
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme(
                Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
        log: list[str] = []
        card = make_card(log)
        sel = QRect(avail.x() + avail.width() // 2 - 380,
                    avail.y() + 70, 320, 110)
        card.show_near(sel.x(), sel.y(), sel.width(), sel.height())
        if degraded:
            card.show_result(None, FORMULAS[3][1])  # 降级 + 长公式：验证滚动重置与换行
        else:
            card.show_result(img, FORMULAS[0][1])
        canvas = _compose_shot(card, sel, dark)
        out = SHOTS_DIR / name
        canvas.save(str(out), "PNG")
        print(f"[shots] {out}")
        card.close()
        card.deleteLater()
        wait_ms(50)

    shoot("01_light.png", dark=False, degraded=False)
    shoot("02_dark.png", dark=True, degraded=False)
    shoot("03_dark_degraded.png", dark=True, degraded=True)
    return 0


# ---------------------------------------------------------------- 入口
def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SnipEqDemo")
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        return run_selftest(app)
    if arg == "--shots":
        return run_shots(app)
    return run_loop(app)


if __name__ == "__main__":
    raise SystemExit(main())
