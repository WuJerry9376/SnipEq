"""recognizer.py — PaddleX 公式识别 pipeline 封装（PP-FormulaNet_plus-S，CPU）。

用法源自 spike/model/smoke_test.py（M0 车道② 已验证），M1 转正。
模型加载**不重新下载**：PADDLE_PDX_CACHE_HOME 指向 spike/model/paddlex_cache
（参数名核实自 paddlex/utils/cache.py:29 `CACHE_DIR = os.environ.get("PADDLE_PDX_CACHE_HOME", ...)`，
须在 import paddlex 之前设置；本模块在 import 时即设置，用户已显式设置的环境变量优先）。

接口：FormulaRecognizer().recognize(image_path) -> (latex, infer_seconds)
异常：RecognitionError（空结果 / 超长串 / **复读循环熔断** / 图片不存在等低质量情形）。
"""
from __future__ import annotations

import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

# 模型 cache 解析（包内优先、开发机回退；用户显式环境变量最高优先）：
#   1) <pkgroot>/paddle_cache        便携包布局（app/ 的兄弟目录）
#   2) <src>/paddle_cache            备用：cache 直接放 app 旁
#   3) ../spike/model/paddlex_cache  开发机布局（_HERE/tests/recognizer.py 时上溯两级也试）
def _resolve_cache_home() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    model = os.path.join("official_models", "PP-FormulaNet_plus-S")
    for base in (os.path.join(here, os.pardir, "paddle_cache"),
                 os.path.join(here, "paddle_cache"),
                 os.path.join(here, os.pardir, os.pardir, "spike", "model", "paddlex_cache"),
                 os.path.join(here, os.pardir, "spike", "model", "paddlex_cache")):
        if os.path.isdir(os.path.join(base, model)):
            return os.path.abspath(base)
    # 全不存在：给默认值，_ensure_loaded 会报"pipeline 配置/模型缺失"类错误
    return os.path.abspath(os.path.join(here, os.pardir, "spike", "model", "paddlex_cache"))


DEFAULT_CACHE_HOME = _resolve_cache_home()
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", DEFAULT_CACHE_HOME)

PIPELINE_YAML = os.path.join(_HERE, "formula_pipeline_S.yaml")

MAX_LATEX_LEN = 600  # 超长串视为识别退化（整页误吞/复读），M1 经验阈值

# ---- M3b：解码复读循环熔断（阈值集中在此便于调参）----
# 依据 spike/model/report_v2.md §延迟/失败模式：real060（955×54 长条）解码复读
# 3.19s、输出 904 字符的 `\ \ \ \ …` 垃圾；现有"超长>600"只拦更长的串，本熔断
# 覆盖 600 以内的循环形态。
REP_WINDOW = 10            # 子串滑窗长度（≥10 字符）
REP_MAX_REPEAT = 8         # 同一窗口子串全文出现次数上限（连续/总体均计入）
REP_TOKEN_GUARD_LEN = 300  # token 去重比检查仅对更长字符串启用（短式天然高重复）
REP_MIN_UNIQUE_RATIO = 0.35  # 去重 token 数 / 总 token 数 低于此判复读

_TOKEN_RE = None  # 懒编译，见 _looks_repetitive


def _looks_repetitive(s: str) -> str | None:
    """检出"解码复读循环"形态则返回命中说明，否则 None。

    两条件任一命中即判（任务口径）：
      A. 同一 ≥REP_WINDOW 字符子串在全文出现 ≥REP_MAX_REPEAT 次；
      B. len(s) > REP_TOKEN_GUARD_LEN 且 去重token数/总token数 < REP_MIN_UNIQUE_RATIO。
    """
    n = len(s)
    # A. 滑窗子串计数
    if n >= REP_WINDOW:
        seen: dict[str, int] = {}
        for i in range(n - REP_WINDOW + 1):
            w = s[i:i + REP_WINDOW]
            c = seen.get(w, 0) + 1
            if c >= REP_MAX_REPEAT:
                return (f"子串 {w!r} 全文出现 {c} 次"
                        f"（窗口≥{REP_WINDOW} 阈值≥{REP_MAX_REPEAT}）")
            seen[w] = c
    # B. 长串 token 去重比
    if n > REP_TOKEN_GUARD_LEN:
        global _TOKEN_RE
        if _TOKEN_RE is None:
            import re
            _TOKEN_RE = re.compile(r"[A-Za-z]+|[0-9]+|\\[A-Za-z]+|\S")
        toks = _TOKEN_RE.findall(s)
        if toks:
            ratio = len(set(toks)) / len(toks)
            if ratio < REP_MIN_UNIQUE_RATIO:
                return (f"长串 token 去重比 {len(set(toks))}/{len(toks)}"
                        f"={ratio:.2f} < {REP_MIN_UNIQUE_RATIO}")
    return None


class RecognitionError(RuntimeError):
    pass


class FormulaRecognizer:
    """懒加载的公式识别器；进程内复用 pipeline 实例（load 只发生一次）。"""

    def __init__(self, pipeline_yaml: str = PIPELINE_YAML):
        self._pipeline_yaml = pipeline_yaml
        self._pipe = None
        self.load_seconds: float | None = None

    def _ensure_loaded(self) -> None:
        if self._pipe is not None:
            return
        if not os.path.isfile(self._pipeline_yaml):
            raise RecognitionError(f"pipeline 配置不存在: {self._pipeline_yaml}")
        from paddlex import create_pipeline  # 重依赖，延迟 import

        t0 = time.perf_counter()
        self._pipe = create_pipeline(self._pipeline_yaml)
        self.load_seconds = time.perf_counter() - t0

    def recognize(self, image_path: str) -> tuple[str, float]:
        """图片路径 → (原始 LaTeX, 推理耗时秒)。preprocess 计在耗时内（PaddleX 内部）。"""
        if not image_path or not os.path.isfile(image_path):
            raise RecognitionError(f"图片文件不存在: {image_path!r}")
        if os.path.getsize(image_path) == 0:
            raise RecognitionError(f"图片文件为空: {image_path!r}")
        self._ensure_loaded()
        t0 = time.perf_counter()
        outputs = list(self._pipe.predict(image_path, batch_size=1))
        infer_s = time.perf_counter() - t0

        if not outputs:
            raise RecognitionError("识别无输出（pipeline 未返回结果）")
        res_list = outputs[0]["formula_res_list"]
        if not res_list:
            raise RecognitionError("识别结果为空列表 — 截图可能不含公式或质量过差")
        latex = str(res_list[0]["rec_formula"]).strip()
        if not latex:
            raise RecognitionError("识别结果为空串 — 截图可能不含公式或质量过差")
        rep = _looks_repetitive(latex)
        if rep:
            raise RecognitionError(
                f"识别结果疑似复读循环（{rep}）— 建议裁剪更小的公式选区重试，"
                "或等 v1.1 难例升档（L2）"
            )
        if len(latex) > MAX_LATEX_LEN:
            raise RecognitionError(
                f"识别结果超长（{len(latex)} > {MAX_LATEX_LEN}）— 疑似误吞整页或模型复读，判低质量"
            )
        return latex, infer_s
