r"""run_m3c_features.py — M3c 新功能回归（修改快捷键热更新 + 检查更新）。

覆盖：
  1) version.py 单一事实源格式；
  2) update.parse_version / compare_versions 表驱动（含 v 前缀 / 两段号 / rc 后缀 / 非法）；
  3) update.check_latest 本地 fixture（monkeypatch _get_json，不联网）四态：
     new_version / up_to_date / 404 仓库未就绪 / 网络失败，文案区分；
  4) hotkey keysym→主键映射 + validate_hotkey_spec（单修饰/未知键等非法文案）；
  5) 真注册链路：_swap_hotkey 成功（ctrl+alt+a→ctrl+shift+q）+ settings round-trip
     + 故意冲突（新键先被旁路 manager 占住）→ 回滚旧键验证；
  6) hotkey_capture_dialog 存在性冒烟（真实交互留给 dogfooding，offscreen 弹框不稳）。

运行：& ..\\.venv\\Scripts\\python.exe run_m3c_features.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 测试期持久化文件全部重定向到临时目录
TMP = Path(tempfile.mkdtemp(prefix="snipeq_m3c_test_"))
os.environ["SNIPEQ_SETTINGS_PATH"] = str(TMP / "settings.json")
os.environ["SNIPEQ_HISTORY_PATH"] = str(TMP / "history.jsonl")

import update  # noqa: E402
import capture  # noqa: E402
from version import __version__  # noqa: E402

FAILURES: list[str] = []


def check(name: str, problems: list[str]) -> None:
    if problems:
        FAILURES.append(name)
        print(f"  [FAIL] {name}")
        for p in problems:
            print(f"         !! {p}")
    else:
        print(f"  [PASS] {name}")


# ---------------------------------------------------------------- 1) version

def t_version() -> None:
    print("\n== 1) version.py")
    import re
    problems = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", __version__):
        problems.append(f"格式非法: {__version__}")
    check("__version__ 语义化格式", problems)


# ---------------------------------------------------------------- 2) semver

def t_semver() -> None:
    print("\n== 2) semver parse/compare 表驱动")
    problems = []
    parse_cases = [
        ("v0.2.0", (0, 2, 0)), ("1.2.3", (1, 2, 3)), ("1.2", (1, 2, 0)),
        ("v10.0.1-rc1", (10, 0, 1)), ("2.0.0+build7", (2, 0, 0)),
        ("", None), ("abc", None), ("1", None), ("1.2.3.4", None), ("v1.x.3", None),
    ]
    for s, want in parse_cases:
        got = update.parse_version(s)
        if got != want:
            problems.append(f"parse({s!r}) = {got} != {want}")
    cmp_cases = [
        ("v0.2.0", "v0.1.0", 1), ("0.1.0", "0.2.0", -1), ("v1.0.0", "1.0.0", 0),
        ("1.9.0", "1.10.0", -1), ("0.1.0", "0.1.0-rc1", 0), ("1.0.1", "1.0", 1),
        ("v2.0.0", "v1.9.9", 1),
    ]
    for a, b, want in cmp_cases:
        got = update.compare_versions(a, b)
        if got != want:
            problems.append(f"compare({a},{b}) = {got} != {want}")
    try:
        update.compare_versions("junk", "0.1.0")
        problems.append("非法输入未抛 ValueError")
    except ValueError:
        pass
    check("parse_version/compare_versions", problems)


# ---------------------------------------------------------------- 3) check_latest fixture

RELEASE_NEW = {"tag_name": "v9.9.9", "html_url": "https://github.com/x/y/releases/tag/v9.9.9",
               "name": "SnipEq v9.9.9"}
RELEASE_OLD = {"tag_name": "v0.0.1", "html_url": "https://github.com/x/y", "name": "old"}


def _with_fetch(fn):
    real = update._get_json
    update._get_json = fn
    try:
        return fn
    finally:
        update._get_json = real


def t_check_latest() -> None:
    print("\n== 3) check_latest 本地 fixture（不联网）")
    problems = []
    real = update._get_json
    try:
        # new_version
        update._get_json = lambda url, timeout: dict(RELEASE_NEW)
        r = update.check_latest("0.1.0")
        if (r.get("status"), r.get("latest"), r.get("url")) != \
           ("new_version", "v9.9.9", RELEASE_NEW["html_url"]):
            problems.append(f"new_version 结果错: {r}")
        # up_to_date
        update._get_json = lambda url, timeout: dict(RELEASE_OLD)
        r = update.check_latest("0.1.0")
        if r.get("status") != "up_to_date":
            problems.append(f"up_to_date 结果错: {r}")
        # 404 仓库未就绪
        def raise404(url, timeout):
            req = __import__("urllib.request", fromlist=["Request"]).Request(url)
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        update._get_json = raise404
        r = update.check_latest("0.1.0")
        if r.get("status") != "error" or r.get("kind") != "not_ready" \
                or "尚未就绪" not in r.get("message", ""):
            problems.append(f"404 文案错: {r}")
        # 网络失败
        def raise_net(url, timeout):
            raise OSError("getaddrinfo failed")
        update._get_json = raise_net
        r = update.check_latest("0.1.0")
        if r.get("status") != "error" or r.get("kind") != "network":
            problems.append(f"network 结果错: {r}")
        # 无 tag → bad_payload
        update._get_json = lambda url, timeout: {"message": "Not Found"}  # dict 但无 tag_name
        r = update.check_latest("0.1.0")
        if r.get("kind") not in ("bad_payload", "not_ready"):
            problems.append(f"空 tag 分类错: {r}")
    finally:
        update._get_json = real
    check("check_latest 三态+异常区分", problems)


# ---------------------------------------------------------------- 4) hotkey 纯逻辑

def t_hotkey_logic() -> None:
    print("\n== 4) keysym 映射 / validate_hotkey_spec")
    from tray_app import _keysym_to_main, SnipEqApp
    problems = []
    ks_cases = [
        ("a", "a"), ("A", "a"), ("F9", "f9"), ("F13", "F_REJECT"), ("1", "1"),
        ("space", "space"), ("Escape", "esc"), ("Return", "enter"),
        ("Control_L", None), ("Alt_R", None), ("Shift_L", None), ("Super_L", None),
        ("Win_L", None), ("Caps_Lock", None), ("dead_grave", None),
    ]
    for ks, want in ks_cases:
        got = _keysym_to_main(ks)
        if got != want:
            problems.append(f"keysym {ks!r} → {got!r} != {want!r}")
    v_cases = [
        ("ctrl+shift+q", True), ("ctrl+alt+a", True), ("f9", True),
        ("win+d", True), ("alt+space", True),
        ("ctrl", False), ("ctrl+", False), ("ctrl+alt", False),
        ("ctrl+dead", False), ("cmd+k", False), ("", False),
    ]
    for spec, want in v_cases:
        ok_, msg = SnipEqApp.validate_hotkey_spec(spec)
        if ok_ != want:
            problems.append(f"validate({spec!r}) = {ok_} != {want} ({msg})")
        if not ok_ and not msg:
            problems.append(f"validate({spec!r}) 非法但无文案")
    check("keysym/validate 纯逻辑", problems)


# ---------------------------------------------------------------- 5) swap + rollback（真注册）

def t_swap_rollback() -> None:
    print("\n== 5) _swap_hotkey 真注册 / settings round-trip / 冲突回滚")
    from tray_app import SnipEqApp
    from settings import Settings
    problems = []
    s = Settings()
    app = SnipEqApp(hotkey="ctrl+alt+a", preview_enabled=False, settings=s)
    app.post = lambda *a, **k: None  # 脱离主环，capture 回调不投递
    try:
        app._start_hotkey()
        if app.hotkey_mgr is None:
            problems.append("初始热键注册失败（环境冲突？请手工核查）")
        else:
            # 成功链：ctrl+alt+a → ctrl+shift+q
            ok_, msg = app._swap_hotkey("ctrl+shift+q")
            if not ok_:
                problems.append(f"换键失败: {msg}")
            if app.hotkey_spec != "ctrl+shift+q":
                problems.append(f"hotkey_spec 未更新: {app.hotkey_spec}")
            s2 = Settings()
            if s2.get("hotkey") == "ctrl+shift+q":
                pass  # save 在 _on_change_hotkey 里做，swap 本身不落盘 → 期望"否"
            # 语义校验：swap 不负责落盘（落盘在 _on_change_hotkey），此处断言未提前写
            if s2.get("hotkey") == "ctrl+shift+q":
                problems.append("swap 不应自行落盘（由 _on_change_hotkey 负责）")
            # 冲突测试：旁路 manager 占住 ctrl+alt+a，再 swap 过去应失败并回滚
            blocker = capture.HotkeyManager("ctrl+alt+a", lambda: None)
            blocker.start()
            try:
                ok_, msg = app._swap_hotkey("ctrl+alt+a")
                if ok_:
                    problems.append("冲突注册竟成功（占位失效？）")
                if "回滚" not in msg and "恢复" not in msg:
                    problems.append(f"冲突文案缺回滚说明: {msg}")
                if app.hotkey_mgr is None or app.hotkey_spec != "ctrl+shift+q":
                    problems.append(f"回滚后当前键异常: {app.hotkey_spec} mgr={app.hotkey_mgr}")
                # 回滚后再验证 ctrl+alt+a 仍不可注册（blocker 还占着），
                # 而 ctrl+shift+q 槽位仍活着 = 当前 mgr 工作正常
                probe = None
                try:
                    probe = capture.HotkeyManager("ctrl+shift+q", lambda: None)
                    probe.start()
                    problems.append("ctrl+shift+q 已被回滚后的 mgr 持有，旁路注册竟成功")
                except RuntimeError:
                    pass
                finally:
                    if probe:
                        probe.stop()
            finally:
                blocker.stop()
            # 落盘 round-trip（模拟 _on_change_hotkey 成功分支）
            app.settings.set(hotkey="ctrl+shift+q")
            app.settings.save()
            s3 = Settings()
            if s3.get("hotkey") != "ctrl+shift+q":
                problems.append(f"settings round-trip 失败: {s3.get('hotkey')}")
    finally:
        if app.hotkey_mgr:
            app.hotkey_mgr.stop()
    check("swap/回滚/落盘 真注册链", problems)


# ---------------------------------------------------------------- 6) 对话框存在性

def t_dialog_import() -> None:
    print("\n== 6) hotkey_capture_dialog 存在性冒烟")
    problems = []
    try:
        import tray_app
        fn = getattr(tray_app, "hotkey_capture_dialog", None)
        if not callable(fn):
            problems.append("hotkey_capture_dialog 缺失")
    except Exception as e:  # noqa: BLE001
        problems.append(f"import tray_app 失败: {e}")
    check("对话框函数存在（交互实测留 dogfooding）", problems)


def main() -> int:
    t_version()
    t_semver()
    t_check_latest()
    t_hotkey_logic()
    t_swap_rollback()
    t_dialog_import()
    print("\n==========================")
    if FAILURES:
        print(f"M3C FEATURES FAIL: {len(FAILURES)} 项 -> {FAILURES}")
        return 1
    print("M3C FEATURES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
