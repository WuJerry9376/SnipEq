"""SnipEq 识别结果浮卡（UI 形态 B，规划书 §6.5）。

契约（集成方按此对接，勿改动签名）：

- 类 ``FormulaCard(QWidget)``：无边框、置顶、无任务栏痕迹（Qt.Tool +
  FramelessWindowHint + WindowStaysOnTopHint）。
- 方法：
    show_result(image_png: str | None, latex: str)
        展示识别结果；``image_png`` 为 PNG 文件路径，None（或读取失败）时
        预览区降级显示 LaTeX 原文。
    show_near(x: int, y: int, w: int, h: int)
        按截图选区（逻辑像素）把卡片摆到其右下侧，越界自动翻边。
- 信号：
    copy_word            点击主按钮 / Enter（编辑框内 Enter 等价触发）
    copy_latex           点击「复制 LaTeX」
    cloud_enhance        「云端增强」（当前禁用，为 v1.1 预留）
    zero_confirm_toggled(bool)  勾选/取消「零确认模式」
    close_requested      Esc / 点击 ×（卡片同时自行隐藏）
- 构造参数 ``on_latex_edited: Callable[[str], None]``：LaTeX 编辑框内容
  变化经 300ms 防抖后回调；集成方重渲染预览后会再次调用 show_result，
  程序化写入不会触发回调（无回环）。

视觉：380–440px 宽紧凑浮卡，跟随系统明暗主题，淡入 120ms，HiDPI 感知。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPalette, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------- 尺寸常量
CARD_WIDTH = 420          # 卡片可见宽度（380–440 区间内）
SHADOW_MARGIN = 20        # 透明窗口四周留给投影的边距
EDGE_GAP = 12             # 卡片与选区间隙
PREVIEW_H = 148           # 预览区高度（含内边距）
PREVIEW_INNER_W = CARD_WIDTH - 16 * 2 - 2 - 28   # 图片可用宽（body 边距+边框+label 内边距）
PREVIEW_INNER_H = PREVIEW_H - 10 * 2 - 2
MAX_UPSCALE = 2.5         # 小图放大有上限，避免糊成一团

DEBOUNCE_MS = 300         # LaTeX 编辑防抖
FADE_MS = 120             # 淡入时长

# ---------------------------------------------------------------- 主题
_LIGHT = dict(
    card="#ffffff", border="#e3e3e8", border2="#dcdce2",
    text="#1b1c1e", muted="#6d7178",
    preview="#f7f7f9", field="#fbfbfd", hover="#ececf1",
    accent="#2f6feb", accent_hover="#2559c9", accent_press="#1e4bab",
    link="#2f6feb",  # 白底对比度 4.5:1
    on_accent="#ffffff",
    shadow="#4e14161c",
)
_DARK = dict(
    # 边框提亮到 #4a4e58：与深色宿主背景拉开边界
    card="#24262b", border="#4a4e58", border2="#3a3e46",
    text="#e9ebee", muted="#9aa0a9",
    preview="#1b1d21", field="#1e2126", hover="#2f3339",
    # 填充加深：#3b72d9 上白字对比度 ≈4.6:1（AA）
    accent="#3b72d9", accent_hover="#3568c8", accent_press="#2f5ab0",
    link="#6e9ff2",  # 暗底(#24262b)上对比度 ≈5.7:1（AA）
    on_accent="#ffffff",
    shadow="#8c000000",
)

_QSS = """
#card {{
    background: {card};
    border: 1px solid {border};
    border-radius: 12px;
    font-family: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
    color: {text};
}}
#title {{ color: {muted}; font-size: 12px; font-weight: 600; }}
#closeBtn {{
    border: none; background: transparent; color: {muted};
    font-size: 15px; padding: 2px 5px; border-radius: 6px;
}}
#closeBtn:hover {{ background: {hover}; color: {text}; }}
#preview {{
    background: {preview};
    border: 1px solid {border2};
    border-radius: 8px;
}}
#preview[mode="text"] {{
    color: {muted};
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
}}
#latex {{
    border: 1px solid {border2};
    border-radius: 8px;
    padding: 6px 9px;
    background: {field};
    color: {text};
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 12px;
    selection-background-color: {accent};
    selection-color: {on_accent};
}}
#latex:focus {{ border: 1px solid {accent}; }}
#primary {{
    background: {accent};
    color: {on_accent};
    border: none;
    border-radius: 8px;
    padding: 7px 16px;
    font-weight: 600;
}}
#primary:hover {{ background: {accent_hover}; }}
#primary:pressed {{ background: {accent_press}; }}
#ghost {{
    background: transparent; border: none; border-radius: 7px;
    padding: 6px 10px; color: {link};
}}
#ghost:hover {{ background: {hover}; }}
#ghost:disabled {{ color: {muted}; background: transparent; }}
#zero {{ color: {muted}; font-size: 11px; }}
"""


def _system_is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorScheme"):
        try:
            return hints.colorScheme() == Qt.ColorScheme.Dark
        except Exception:
            pass
    base = QGuiApplication.palette().color(QPalette.Base)
    return base.lightness() < 128


class FormulaCard(QWidget):
    """识别结果浮卡（形态 B）。"""

    copy_word = Signal()
    copy_latex = Signal()
    cloud_enhance = Signal()
    zero_confirm_toggled = Signal(bool)
    close_requested = Signal()

    def __init__(
        self,
        on_latex_edited: Optional[Callable[[str], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._on_latex_edited = on_latex_edited
        self._last_sent_latex: Optional[str] = None
        self._placed = False

        # 无边框 + 置顶 + Tool（无任务栏痕迹）；透明背景承载投影
        self.setWindowFlags(
            Qt.Window
            | Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(CARD_WIDTH + 2 * SHADOW_MARGIN)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

        self._edit_timer = QTimer(self, singleShot=True, interval=DEBOUNCE_MS)
        self._edit_timer.timeout.connect(self._flush_edit)

        self._build_ui()
        self._apply_theme()

        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            try:
                hints.colorSchemeChanged.connect(lambda *_: self._apply_theme())
            except (TypeError, RuntimeError):
                pass

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN,
                                 SHADOW_MARGIN, SHADOW_MARGIN)

        self._body = QFrame(self)
        self._body.setObjectName("card")
        outer.addWidget(self._body)

        v = QVBoxLayout(self._body)
        v.setContentsMargins(16, 10, 16, 12)
        v.setSpacing(8)

        # 顶部细条：小标题 + 关闭 ×（× 右缘负边距外溢 6px，光学留白与左缘一致）
        header = QHBoxLayout()
        header.setSpacing(0)
        header.setContentsMargins(0, 0, -6, 0)
        title = QLabel("SnipEq", self._body)
        title.setObjectName("title")
        close_btn = QToolButton(self._body)
        close_btn.setObjectName("closeBtn")
        close_btn.setText("\u00d7")  # ×
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setToolTip("关闭（Esc）")
        close_btn.clicked.connect(self._request_close)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(close_btn)
        v.addLayout(header)

        # 预览区：图片等比居中、留呼吸感；降级时显示 LaTeX 原文
        self._preview = QLabel(self._body)
        self._preview.setObjectName("preview")
        self._preview.setFixedHeight(PREVIEW_H)
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setWordWrap(True)
        self._preview.setContentsMargins(14, 10, 14, 10)
        self._preview.setProperty("mode", "text")
        v.addWidget(self._preview)

        # LaTeX 编辑单行框（等宽字体走 QSS #latex）
        self._latex_edit = QLineEdit(self._body)
        self._latex_edit.setObjectName("latex")
        mono = QFont()
        mono.setFamilies(["Cascadia Mono", "Consolas", "monospace"])
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSizeF(9.5)
        self._latex_edit.setFont(mono)
        self._latex_edit.textChanged.connect(self._edit_timer.start)
        self._latex_edit.returnPressed.connect(self.copy_word.emit)
        v.addWidget(self._latex_edit)

        # 操作区：主按钮 + 次要文字按钮 + 禁用态云端增强
        row = QHBoxLayout()
        row.setSpacing(4)
        self._btn_word = QPushButton("复制为 Word 公式", self._body)
        self._btn_word.setObjectName("primary")
        self._btn_word.setCursor(Qt.PointingHandCursor)
        self._btn_word.clicked.connect(self.copy_word.emit)

        self._btn_latex = QPushButton("复制 LaTeX", self._body)
        self._btn_latex.setObjectName("ghost")
        self._btn_latex.setCursor(Qt.PointingHandCursor)
        self._btn_latex.clicked.connect(self.copy_latex.emit)

        self._btn_cloud = QPushButton("云端增强", self._body)
        self._btn_cloud.setObjectName("ghost")
        self._btn_cloud.setEnabled(False)
        self._btn_cloud.setToolTip("难例升档 v1.1 提供")
        # 信号为 v1.1 预留，按钮禁用期间不会触发
        self._btn_cloud.clicked.connect(self.cloud_enhance.emit)

        row.addWidget(self._btn_word)
        row.addWidget(self._btn_latex)
        row.addSpacing(6)
        row.addWidget(self._btn_cloud)
        row.addStretch(1)
        v.addLayout(row)

        # 底部一行小字勾选
        self._zero_check = QCheckBox("零确认模式（识别后直接进剪贴板）", self._body)
        self._zero_check.setObjectName("zero")
        self._zero_check.toggled.connect(self.zero_confirm_toggled)
        v.addWidget(self._zero_check)

    # ------------------------------------------------------------ 主题
    def _apply_theme(self) -> None:
        tokens = _DARK if _system_is_dark() else _LIGHT
        self._body.setStyleSheet(_QSS.format(**tokens))
        effect = QGraphicsDropShadowEffect(self._body)
        effect.setBlurRadius(30)
        effect.setOffset(0, 10)
        effect.setColor(QColor(tokens["shadow"]))
        self._body.setGraphicsEffect(effect)

    # ------------------------------------------------------------ 公开接口
    def show_result(self, image_png: Optional[str], latex: str) -> None:
        """展示识别结果；image_png 为 None 时预览区降级显示 LaTeX 原文。"""
        latex = latex or ""

        # 程序化写入不触发编辑回调（避免与集成方重渲染形成回环）
        self._edit_timer.stop()
        self._latex_edit.blockSignals(True)
        self._latex_edit.setText(latex)
        # 长公式灌入后重置到开头，保证开头可见（滚动/光标归零）
        self._latex_edit.deselect()
        self._latex_edit.setCursorPosition(0)
        self._latex_edit.blockSignals(False)
        self._last_sent_latex = latex

        pm = None
        if image_png:
            pm = QPixmap(image_png)
            if pm.isNull():
                pm = None
        if pm is not None:
            self._show_image(pm)
        else:
            self._show_degraded(latex)

        self._ensure_shown()

    def show_near(self, x: int, y: int, w: int, h: int) -> None:
        """把卡片摆到截图选区（逻辑像素）右下侧，越界自动翻边。"""
        screen = (
            QGuiApplication.screenAt(QPoint(x + w // 2, y + h // 2))
            or QGuiApplication.primaryScreen()
        )
        avail = screen.availableGeometry()
        cw, ch = self.sizeHint().width(), self.sizeHint().height()

        # 优先右下（右侧、下缘），越界翻边，最后夹回工作区
        left = x + w + EDGE_GAP
        if left + cw > avail.x() + avail.width():
            left = x - EDGE_GAP - cw                      # 翻到左侧
        left = max(avail.x(), min(left, avail.x() + avail.width() - cw))

        top = y + h + EDGE_GAP
        if top + ch > avail.y() + avail.height():
            top = y - EDGE_GAP - ch                       # 翻到上方
        top = max(avail.y(), min(top, avail.y() + avail.height() - ch))

        self._placed = True
        self.move(left, top)

    def set_zero_confirm(self, checked: bool) -> None:
        """外部同步勾选状态（托盘菜单等），不发信号、不触发回调。"""
        self._zero_check.blockSignals(True)
        self._zero_check.setChecked(checked)
        self._zero_check.blockSignals(False)

    # ------------------------------------------------------------ 内部
    def _show_image(self, pm: QPixmap) -> None:
        dpr = max(self.devicePixelRatioF(), 1.0)
        # PNG 按物理像素存储，按逻辑尺寸等比适配预览框
        w_log, h_log = pm.width() / dpr, pm.height() / dpr
        fit = min(PREVIEW_INNER_W / w_log, PREVIEW_INNER_H / h_log, MAX_UPSCALE)
        target = QSize(max(1, round(pm.width() * fit)),
                       max(1, round(pm.height() * fit)))
        pm = pm.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self._set_preview_mode("image")
        self._preview.setText("")
        self._preview.setPixmap(pm)

    def _show_degraded(self, latex: str) -> None:
        self._set_preview_mode("text")
        self._preview.clear()
        self._preview.setText(latex)

    def _set_preview_mode(self, mode: str) -> None:
        if self._preview.property("mode") != mode:
            self._preview.setProperty("mode", mode)
            style = self._preview.style()
            style.unpolish(self._preview)
            style.polish(self._preview)

    def _ensure_shown(self) -> None:
        if not self._placed:
            # 未经 show_near 时兜底：跟随鼠标附近
            pos = QCursor.pos()
            self.show_near(pos.x(), pos.y(), 1, 1)
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
            self._fade.stop()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self.raise_()
        self.activateWindow()

    def _flush_edit(self) -> None:
        text = self._latex_edit.text()
        if text == self._last_sent_latex:
            return
        self._last_sent_latex = text
        if self._on_latex_edited is not None:
            self._on_latex_edited(text)

    def _request_close(self) -> None:
        self.hide()
        self.close_requested.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            # 编辑框/按钮内的 Enter 已被各自机制消费，不会走到这里重复触发
            self.copy_word.emit()
            event.accept()
            return
        if key == Qt.Key_Escape:
            self._request_close()
            event.accept()
            return
        super().keyPressEvent(event)
