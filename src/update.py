"""update.py — SnipEq 检查更新（M3c，零第三方依赖：urllib + 系统代理环境）。

设计（§托盘"检查更新"）：
  - 端点：GitHub Releases latest API（owner/repo 常量集中在此，**换名只改一处**）；
  - UA 头 + 超时 8s + urllib 默认 opener（自动读 http(s)_proxy 环境变量/系统设置）；
  - 结果三态区分：no_update / new_version / error(网络失败 | 仓库未就绪 404 文案不同)，
    任何情形**不抛异常**（调用方只在用户点击菜单时反馈，全程静默失败不打扰）；
  - semver 三元组比较：`v1.2.3` / `1.2.3-rc1`（后缀忽略，未知位按 0）；非法版本
    parse_version 返回 None，compare_versions 非法输入抛 ValueError（调用方以
    parse_version 预检）。

用法：
    from update import check_latest, REPO_URL
    r = check_latest()          # {"status": "up_to_date"|"new_version"|"error", ...}
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

# ---- 仓库常量（换名只改这两行）----
GITHUB_OWNER = "WuJerry9376"
GITHUB_REPO = "SnipEq"
LATEST_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
USER_AGENT = "SnipEq-updater"
TIMEOUT_S = 8.0

_TAG_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+].*)?$")


def parse_version(s: str) -> tuple[int, int, int] | None:
    """'v1.2.3' / '1.2' / '1.2.3-rc1' → (1,2,3)；不可解析返回 None。"""
    if not s:
        return None
    m = _TAG_RE.match(s.strip())
    if not m:
        return None
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    return (int(major), int(minor), int(patch or 0))


def compare_versions(a: str, b: str) -> int:
    """a>b → 1；a<b → -1；相等 → 0。不可解析抛 ValueError。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        raise ValueError(f"无法解析版本号: {a!r} vs {b!r}")
    return (pa > pb) - (pa < pb)


def _get_json(url: str, timeout: float):
    """GET + JSON 解码（urllib 默认 opener 继承系统代理环境）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_latest(current: str | None = None, url: str = LATEST_API,
                 timeout: float = TIMEOUT_S) -> dict:
    """检查最新版本。绝不抛异常，返回：
      {"status": "up_to_date", "latest": "vX", "current": "vY"}
      {"status": "new_version", "latest": "vX", "current": "vY", "url": html_url,
       "name": release_title}
      {"status": "error", "kind": "not_ready"|"network"|"bad_payload"|"bad_version",
       "message": 人话}
    """
    if current is None:
        from version import __version__ as current  # 延迟避免环依赖
    try:
        data = _get_json(url, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "error", "kind": "not_ready",
                    "message": "发布仓库尚未就绪（还没有正式版）"}
        return {"status": "error", "kind": "network",
                "message": f"GitHub 返回 HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 URLError/timeout/断网
        return {"status": "error", "kind": "network",
                "message": f"网络不可达: {type(e).__name__}"}
    if not isinstance(data, dict):
        return {"status": "error", "kind": "bad_payload", "message": "响应格式异常"}
    tag = str(data.get("tag_name") or "")
    url_html = str(data.get("html_url") or REPO_URL)
    if not tag:
        return {"status": "error", "kind": "bad_payload",
                "message": "最新版本号解析失败"}
    if parse_version(tag) is None or parse_version(current) is None:
        return {"status": "error", "kind": "bad_version",
                "message": f"版本号无法比较（本地 {current} / 最新 {tag}）"}
    try:
        newer = compare_versions(tag, current) > 0
    except ValueError as e:
        return {"status": "error", "kind": "bad_version", "message": str(e)}
    if not newer:
        return {"status": "up_to_date", "latest": tag, "current": current}
    return {"status": "new_version", "latest": tag, "current": current,
            "url": url_html, "name": str(data.get("name") or tag)}


if __name__ == "__main__":
    print(json.dumps(check_latest(), ensure_ascii=False, indent=1))
