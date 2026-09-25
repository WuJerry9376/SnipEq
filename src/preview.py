"""preview.py — LaTeX → PNG 预览渲染桥（SnipEq M2）。

路线（复用 spike 车道② 已验证的 KaTeX+Edge 渲染链）：
  常驻 node 子进程（stdio 行协议，src/preview_katex.js）加载
  spike/model/node_modules/katex 做 renderToString（省每次 ~200ms 的 node 冷启动）
  → 写临时 HTML（本地 katex.min.css，file:// 引用）
  → headless Edge --screenshot → PIL 紧裁（pad 6px）→ PNG 路径。

失败语义（调用方降级显示纯 LaTeX 文本）：
  node 不可用 / katex 抛错 / Edge 不可用 / 截图失败 / 渲染全白 —— 一律返回 None，
  不抛异常（render_ok_once 例外，抛原因供诊断）。单次渲染目标 <500ms（冒烟打印计时）。

用法：
    with PreviewRenderer() as pv:
        png = pv.render(latex)          # str | None
    或模块级 render_once(latex)（自管进程，慢在 node 冷启动，仅兜底/脚本用）。
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent

# katex node 模块解析：包内 app/katex_node_modules 优先，开发机回退 spike
DEFAULT_NODE_MODULES = (_HERE / "katex_node_modules"
                        if (_HERE / "katex_node_modules" / "katex").is_dir()
                        else _REPO_ROOT / "spike" / "model" / "node_modules")
# 浏览器内渲染资产（便携包主路线：无 node 时 Edge 直接跑 katex.min.js）；
# 包内 app/katex_assets 优先，开发机回退 spike katex/dist
_A1 = _HERE / "katex_assets"
_A2 = _REPO_ROOT / "spike" / "model" / "node_modules" / "katex" / "dist"
KATEX_ASSETS = _A1 if (_A1 / "katex.min.js").is_file() else _A2
KATEX_JS_NAME = "katex.min.js"
KATEX_CSS_NAME = "katex.min.css"
KATEX_JS = _HERE / "preview_katex.js"

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

HTML_TMPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="file:///{css}">
<style>
  html, body {{ margin: 0; padding: 0; background: #ffffff; }}
  #box {{ display: inline-block; padding: 14px 18px; background: #ffffff;
          color: #000000; font-size: 26px; }}
</style>
</head><body><div id="box">{body}</div></body></html>
"""


def find_edge() -> str | None:
    for p in _EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return shutil.which("msedge")


def _katex_css() -> str:
    return (DEFAULT_NODE_MODULES / "katex" / "dist" / "katex.min.css").resolve().as_posix()


class PreviewRenderer:
    """常驻 node 子进程 + 串行渲染（线程安全：内部锁，Edge 截图本身重）。"""

    def __init__(
        self,
        node_modules: str | os.PathLike | None = None,
        workdir: str | os.PathLike | None = None,
        request_timeout: float = 5.0,
        edge_timeout: float = 10.0,
    ):
        self.node_modules = str(node_modules or DEFAULT_NODE_MODULES)
        self.workdir = Path(workdir or (Path(tempfile.gettempdir()) / "snipeq_preview"))
        self.request_timeout = request_timeout
        self.edge_timeout = edge_timeout
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self.last_error: str | None = None
        self.last_timings: dict[str, float] = {}
        self._edge = find_edge()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._edge_profile = self.workdir / "_edge_profile"
        atexit.register(self.close)

    # ------------------------------------------------------------ node 进程

    def _ensure_node(self) -> bool:
        """确保常驻 node 存活并完成握手。失败返回 False（记 last_error）。"""
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._proc = None
        if shutil.which("node") is None:
            self.last_error = "node 不在 PATH"
            return False
        if not (Path(self.node_modules) / "katex").is_dir():
            self.last_error = f"katex 不存在: {self.node_modules}"
            return False
        try:
            self._proc = subprocess.Popen(
                ["node", str(KATEX_JS), self.node_modules],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            self.last_error = f"node 启动失败: {e}"
            return False
        hello = self._readline_with_timeout(self.request_timeout)
        if not hello or not hello.get("hello"):
            self.last_error = "katex 子进程握手失败"
            self._kill_node()
            return False
        return True

    def _kill_node(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def _readline_with_timeout(self, timeout: float) -> dict | None:
        """带超时读一行 JSON；超时 kill node（令其管道 EOF，避免永久阻塞）。"""
        assert self._proc is not None and self._proc.stdout is not None
        result: list[str | None] = [None]

        def _reader():
            try:
                result[0] = self._proc.stdout.readline()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                result[0] = ""
        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.last_error = "katex 子进程响应超时"
            self._kill_node()
            return None
        if not result[0]:
            self.last_error = "katex 子进程已退出"
            self._kill_node()
            return None
        try:
            return json.loads(result[0])
        except json.JSONDecodeError:
            return None

    def _katex_html(self, latex: str) -> str | None:
        with self._lock:
            if not self._ensure_node():
                return None
            assert self._proc is not None and self._proc.stdin is not None
            self._req_id += 1
            rid = self._req_id
            try:
                self._proc.stdin.write(json.dumps({"id": rid, "latex": latex}) + "\n")
                self._proc.stdin.flush()
            except OSError as e:
                self.last_error = f"katex 请求写入失败: {e}"
                self._kill_node()
                return None
            resp = self._readline_with_timeout(self.request_timeout)
            if resp is None or resp.get("id") != rid:
                if resp is not None:
                    self.last_error = "katex 应答 id 错乱"
                return None
            if not resp.get("ok"):
                self.last_error = f"katex 渲染错误: {resp.get('err')}"
                return None
            return resp["html"]

    # ------------------------------------------------------------ Edge 截图

    def _screenshot_png(self, html: str, png_path: Path) -> bool:
        if not self._edge:
            self.last_error = "未找到 msedge（Edge 不可用）"
            return False
        html_path = png_path.with_suffix(".html")
        html_path.write_text(
            HTML_TMPL.format(css=_katex_css(), body=html), encoding="utf-8")
        uri = "file:///" + html_path.as_posix().lstrip("/")
        cmd = [
            self._edge, "--headless=new", "--disable-gpu", "--no-first-run",
            "--disable-sync", "--hide-scrollbars",
            f"--user-data-dir={self._edge_profile}",
            "--force-device-scale-factor=2",
            "--virtual-time-budget=500",
            "--window-size=900,500",
            "--default-background-color=FFFFFFFF",
            f"--screenshot={png_path}",
            uri,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=self.edge_timeout)
        except subprocess.TimeoutExpired:
            self.last_error = f"Edge 截图超时(>{self.edge_timeout}s)"
            html_path.unlink(missing_ok=True)
            return False
        html_path.unlink(missing_ok=True)
        if not png_path.is_file():
            self.last_error = "Edge 截图失败（未产出 PNG）"
            return False
        return self._tight_crop(png_path)

    @staticmethod
    def _tight_crop(png_path: Path, pad: int = 6) -> bool:
        """紧裁到公式包围盒（白底黑字口径，同 make_synth）。全白判失败。"""
        from PIL import Image

        im = Image.open(png_path).convert("L")
        bbox = im.point(lambda v: 255 if v < 250 else 0).getbbox()
        if bbox is None:
            png_path.unlink(missing_ok=True)
            return False
        x0, y0, x1, y1 = bbox
        W, H = im.size
        box = (max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad))
        Image.open(png_path).convert("RGB").crop(box).save(png_path)
        return True

    # ------------------------------------------------------------ 公共口

    def render(self, latex: str, scale: float = 2.0) -> str | None:
        """LaTeX → PNG 绝对路径；任何失败返回 None（原因见 self.last_error）。

        双路线：① 常驻 node(katex renderToString) → 静态 HTML（开发机，katex ~5ms）；
        ② 浏览器内渲染：HTML 引本地 katex.min.js，Edge 截图时完成渲染
        （便携包路线，无 node/SNIPEQ_NO_NODE=1 时；失败页留白 → 紧裁判 blank → None）。
        scale 目前仅记录（Edge 侧固定 2× 超采样，紧裁后按需缩放）。
        """
        t0 = time.perf_counter()
        if not latex or not latex.strip():
            self.last_error = "latex 为空"
            return None
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._req_id += 1
        png_path = self.workdir / f"prev_{os.getpid()}_{self._req_id}.png"
        node_ok = os.environ.get("SNIPEQ_NO_NODE") != "1"
        html = self._katex_html(latex) if node_ok else None
        t_katex = time.perf_counter() - t0
        if html is not None:
            node_err = None
            ok = self._screenshot_png(html, png_path)
        else:
            node_err = self.last_error
            try:
                ok = self._screenshot_png(self._client_html(latex), png_path)
            except FileNotFoundError as e:
                self.last_error = f"{e}（node 路线: {node_err}）"
                return None
        if not ok:
            if node_err:
                self.last_error = f"{self.last_error}（node 路线: {node_err}）"
            return None
        if scale != 2.0:
            self._rescale(png_path, scale / 2.0)
        self.last_timings = {
            "katex": t_katex,
            "total": time.perf_counter() - t0,
        }
        return str(png_path.resolve())

    def _client_html(self, latex: str) -> str:
        """浏览器内 KaTeX 渲染页（便携包无 node 路线）。渲染失败留白页。"""
        if not (KATEX_ASSETS / KATEX_JS_NAME).is_file():
            raise FileNotFoundError("katex_assets 缺失（且无 node 路线）")
        css = (KATEX_ASSETS / KATEX_CSS_NAME).resolve().as_posix()
        js = (KATEX_ASSETS / KATEX_JS_NAME).resolve().as_posix()
        payload = json.dumps(latex)
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="file:///{css}">
<script src="file:///{js}"></script>
<style>
  html, body {{ margin: 0; padding: 0; background: #ffffff; }}
  #box {{ display: inline-block; padding: 14px 18px; background: #ffffff;
          color: #000000; font-size: 26px; }}
</style>
</head><body><div id="box"></div>
<script>
try {{
  katex.render({payload}, document.getElementById('box'),
               {{ displayMode: true, throwOnError: true }});
}} catch (e) {{ document.body.innerHTML = ''; document.title = 'KATEX-FAIL'; }}
</script></body></html>"""

    @staticmethod
    def _rescale(png_path: Path, factor: float) -> None:
        from PIL import Image

        im = Image.open(png_path)
        w, h = im.size
        nw, nh = max(1, int(w * factor)), max(1, int(h * factor))
        im.resize((nw, nh), Image.LANCZOS).save(png_path)

    def close(self) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write('{"cmd":"quit"}\n')
                self._proc.stdin.flush()
                self._proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                self._kill_node()
            else:
                self._proc = None

    __enter__ = lambda self: self  # noqa: E731

    def __exit__(self, *exc) -> None:
        self.close()


_shared: PreviewRenderer | None = None
_shared_lock = threading.Lock()


def render_once(latex: str) -> str | None:
    """模块级便捷口：进程内共享一个 PreviewRenderer（懒建，常驻到进程退出）。"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = PreviewRenderer()
        return _shared.render(latex)


if __name__ == "__main__":
    # 手工诊断：py preview.py "<latex>"（渲染并打印 PNG 路径与计时）
    demo = sys.argv[1] if len(sys.argv) > 1 else r"\frac{a}{b}"
    with PreviewRenderer() as pv:
        p = pv.render(demo)
        if p:
            print(f"OK {p}  katex={pv.last_timings['katex']*1000:.0f}ms "
                  f"total={pv.last_timings['total']*1000:.0f}ms")
        else:
            print(f"FAIL {pv.last_error}")
            sys.exit(1)
