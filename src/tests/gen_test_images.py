"""gen_test_images.py — 渲染 SnipEq E2E 测试图（分式+求和、cases、matrix、aligned）。

渲染方案与 spike/model/make_synth.py 同源（KaTeX renderToString → 静态 HTML →
headless Edge 截图 → PIL 紧裁剪），更接近真实打印体截图；spike 目录**只读引用**
（node_modules/katex 与 katex_pre.js），所有产物写到本目录 _work/ 与 images/。

运行（任一带 Pillow 的解释器，需本机有 node 与 Edge）：
  & ..\\.venv\\Scripts\\python.exe gen_test_images.py
"""
import json
import os
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "images")
WORK = os.path.join(HERE, "_work")
SPIKE_MODEL = os.path.abspath(os.path.join(HERE, "..", "..", "spike", "model"))  # 只读
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
KATEX_CSS = (os.path.join(SPIKE_MODEL, "node_modules", "katex", "dist", "katex.min.css")
             .replace("\\", "/"))

ITEMS = {
    "frac_sum": r"\frac{\sum_{i=1}^{n} i^{2}}{n}=\frac{(n+1)(2n+1)}{6}",
    "cases": r"f(x)=\begin{cases}x^{2}, & x\geq 0\\ -x, & x<0\end{cases}",
    "matrix": r"\begin{pmatrix}a & b\\ c & d\end{pmatrix}^{-1}=\frac{1}{ad-bc}\begin{pmatrix}d & -b\\ -c & a\end{pmatrix}",
    "aligned": r"\begin{aligned}\nabla\cdot\mathbf{E} &= \frac{\rho}{\varepsilon_0}\\ \nabla\times\mathbf{B} &= \mu_0\mathbf{J}\end{aligned}",
}

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


def katex_render_all(latexes):
    inp = os.path.join(WORK, "_latex.json")
    outp = os.path.join(WORK, "_katex.json")
    with open(inp, "w", encoding="utf-8") as f:
        json.dump({"items": [{"latex": l} for l in latexes]}, f)
    r = subprocess.run(
        ["node", os.path.join(SPIKE_MODEL, "katex_pre.js"), inp, outp],
        capture_output=True, text=True, cwd=SPIKE_MODEL, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node katex failed: {r.stderr[-500:]}")
    res = json.load(open(outp, encoding="utf-8"))
    bad = [b for b in res if not b["ok"]]
    if bad:
        raise RuntimeError(f"katex errors: {bad[0]['err'][:200]}")
    return [b["html"] for b in sorted(res, key=lambda x: x["i"])]


def screenshot(html_path, png_path):
    uri = "file:///" + html_path.replace("\\", "/")
    cmd = [
        EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
        "--disable-sync", "--hide-scrollbars",
        "--user-data-dir=" + os.path.join(WORK, "_edge"),
        "--force-device-scale-factor=2.08",
        "--virtual-time-budget=5000",
        "--window-size=2600,1600",
        "--default-background-color=FFFFFFFF",
        f"--screenshot={png_path}",
        uri,
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.exists(png_path):
        raise RuntimeError("edge screenshot failed")


def tight_crop(png_path, pad=6):
    im = Image.open(png_path).convert("L")
    bw = im.point(lambda v: 255 if v < 250 else 0)
    bbox = bw.getbbox()
    if bbox is None:
        raise RuntimeError("blank render")
    x0, y0, x1, y1 = bbox
    W, H = im.size
    box = (max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad))
    Image.open(png_path).convert("RGB").crop(box).save(png_path)
    return (box[2] - box[0], box[3] - box[1])


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    names = list(ITEMS)
    htmls = katex_render_all([ITEMS[n] for n in names])
    for name, body_html in zip(names, htmls):
        png = os.path.join(OUT_DIR, f"{name}.png")
        html = os.path.join(WORK, f"{name}.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write(HTML_TMPL.format(css=KATEX_CSS, body=body_html))
        screenshot(html, png)
        size = tight_crop(png)
        print(f"[{name}] OK {png} {size}")
    # ground truth 清单（run_e2e 参考用，不参与断言——识别结果逐字不保证等于 GT）
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(ITEMS, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
