# SnipEq（公式快贴）

**Screenshot a math formula → paste a native Word equation.** Offline-first,
zero-install portable, free alternative to Mathpix-style paid snippers for the
"move formulas into Word/WPS" workflow.

How it works: a global hotkey freezes the screen, you drag-select a formula,
a local model (PaddleX **PP-FormulaNet_plus-S**, CPU) recognizes it into LaTeX,
a normalization layer rewrites the model's LaTeX dialect into what
`latex2mathml` converts losslessly, and the clipboard receives **three formats
at once**:

| Clipboard format | Consumed by |
|---|---|
| `HTML Format` with Presentation MathML | Word 2016+ / Microsoft 365 → **native equation object** on Ctrl+V |
| `CF_UNICODETEXT` as `$…$`-delimited LaTeX | Word "LaTeX input mode" (`Alt+=` then paste), WPS formula box |
| custom `MathML` flavor | Paste-specialist fallback paths |

No cloud, no account, no telemetry. Everything runs on your machine.

## Quick start (portable)

1. Download `SnipEq-portable.zip` from the [latest release](../../releases)
   and extract anywhere (no admin rights needed).
2. Double-click **`SnipEq启动.vbs`** — a ∑ icon appears in the tray
   (~10 s first model load). Optionally run `检查环境.bat` first: all `[OK]` = ready.
3. Point at any formula picture → press **`Ctrl+Alt+A`** → drag a box → Enter.
   A compact card shows a rendered preview + editable LaTeX.
   Enter / "复制为 Word 公式" → **Ctrl+V in Word**.

Tray menu: capture now · zero-confirm mode (skip the card, copy straight to
clipboard) · idle model unload · autostart · **change hotkey** (register-live,
rolls back on conflict) · recent history · **check for updates** (only talks to
GitHub when you click it). Settings & history live in
`%APPDATA%\SnipEq\{settings.json,history.jsonl}`.

## Running from source

```powershell
python -m venv src\.venv          # Python 3.12 x64
src\.venv\Scripts\python.exe -m pip install -r src\requirements.txt
src\.venv\Scripts\python.exe src\snipeq.py image path\to\formula.png   # CLI
src\.venv\Scripts\python.exe src\tray_app.py                           # tray app
```

The recognizer downloads **PP-FormulaNet_plus-S** (~250 MB, Apache-2.0) on
first use into the PaddleX cache, or point `PADDLE_PDX_CACHE_HOME` at a
pre-populated `official_models/` directory. Tests under `src/tests/` cover the
full chain (clipboard write → read-back verification) but **the complete
data-chain suites expect repo-internal `spike/` assets (model cache, corpus)
that are not published with this facade**; the self-contained ones
(`run_m3a_fixes.py`, `run_m3c_features.py`) run anywhere.

## Repository layout

- `src/` — the app: `recognizer.py` (PaddleX wrapper + decode-loop circuit
  breaker), `normalize.py` (N1–N12 dialect rules), `mathml.py`,
  `clipboard_writer.py` (raw ctypes CF_HTML/MathML/Unicode clipboard, no pywin32),
  `preview.py` (KaTeX→PNG via resident Node or headless Edge),
  `capture.py` (global hotkey + multi-monitor freeze + rubber-band overlay),
  `tray_app.py` (PySide6 result card + pystray tray + pipeline shell),
  `ui/formula_card.py` (frameless floating card widget), `settings.py`,
  `update.py`, `version.py` (single source of truth), `tests/`.
- `package/build.ps1` — reproducibility script for the portable zip
  (embedded CPython + trimmed site-packages + bundled model).

## Known boundaries

- Verified against Microsoft 365 / Word 2019+ on real office machines; older
  builds may paste LaTeX text instead of an object (fallback path documented above).
- Recognition quality ceiling on handwriting / blurry screenshots (it is a
  small CPU model by design). Very long strip selections can trigger the
  decode-loop circuit breaker — crop tighter.
- Preview render is ~1 s (headless Edge); it runs async and never blocks the
  clipboard. A strict <500 ms renderer is on the v1.1 roadmap.
- Mixed-DPI multi-monitor: card placement uses the primary screen scale.
- Cloud-assist (L2) endpoint is a placeholder in settings; v1.1.

## 中文速览

免安装便携版解压即用：双击 `SnipEq启动.vbs`，`Ctrl+Alt+A` 框选公式截图，
本地模型识别出 LaTeX，浮卡可即时改错，回车即把「原生 Word 公式 + `$LaTeX$` +
MathML」三格式写入剪贴板，Word 里直接 `Ctrl+V`。全程离线、无账号、无上传；
识别历史只存本机。许可 MIT；模型权重 Apache-2.0。

## License

MIT (see `LICENSE`). Model weights PP-FormulaNet_plus-S are Apache-2.0 by
PaddlePaddle/Baidu.
