"""settings.py — SnipEq M2 集成：设置持久化 / 识别历史 / 开机自启 / 临时产物清理。

- Settings：JSON 文件（默认 %APPDATA%\\SnipEq\\settings.json，env SNIPEQ_SETTINGS_PATH 覆盖，
  测试可注入）。键：hotkey / zero_confirm / unload_idle_min / preview_enabled /
  autostart / l2_endpoint（4090 占位，v1.1 路由用）/ history_enabled。
- History：JSONL 追加（同目录 history.jsonl，env SNIPEQ_HISTORY_PATH 覆盖），
  每条 {ts, latex, sha1(源图前 64KB), src}；recent(n) 取最近 n 条（新→旧）。
- autostart：HKCU\\...\\CurrentVersion\\Run 值名 "SnipEq"；
  **写前 dry-run 打印**（set_autostart(True, log=print) 先打印将写入的命令行）；
  读取用 get_autostart_cmd()，测试不触碰真注册表。
- cleanup_temp()：删除 %TEMP% 下 snipeq_shot_*.png / snipeq_preview*（文件与目录内）
  超过 max_age_hours 的残留，启动时调用。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import winreg
from pathlib import Path

APP_DIR_NAME = "SnipEq"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SnipEq"

DEFAULTS: dict = {
    "hotkey": "ctrl+alt+a",
    "zero_confirm": False,
    "unload_idle_min": 10.0,
    "preview_enabled": True,
    "autostart": False,
    "l2_endpoint": "",          # 4090 MinerU 服务地址占位（v1.1 L2 路由）
    "history_enabled": True,
}


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


class Settings:
    """点分访问的 JSON 设置；save() 原子写。"""

    def __init__(self, path: str | os.PathLike | None = None):
        env = os.environ.get("SNIPEQ_SETTINGS_PATH")
        self.path = Path(path or env or (app_data_dir() / "settings.json"))
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULTS:
                    if k in raw:
                        self.data[k] = raw[k]
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 坏文件不炸启动
            print(f"[settings] 读取失败({self.path}): {e}，使用默认值")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, **kv) -> None:
        self.data.update(kv)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)


class History:
    """识别历史 JSONL 追加存储。"""

    MAX_LINES = 500  # 超限时 recent 只读尾部，写入端定期压缩由 compact() 负责

    def __init__(self, path: str | os.PathLike | None = None):
        env = os.environ.get("SNIPEQ_HISTORY_PATH")
        self.path = Path(path or env or (app_data_dir() / "history.jsonl"))

    def append(self, latex: str, src: str = "", sha1: str = "") -> dict:
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "latex": latex, "src": src, "sha1": sha1}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            print(f"[history] 写入失败: {e}")
        return entry

    def recent(self, n: int = 10) -> list[dict]:
        """最近 n 条，新→旧。坏行跳过。"""
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return out
        for line in reversed(lines[-self.MAX_LINES:]):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and e.get("latex"):
                out.append(e)
                if len(out) >= n:
                    break
        return out

    def compact(self) -> None:
        """只保留最近 MAX_LINES 条（原子重写）。"""
        try:
            keep = self.recent(self.MAX_LINES)
        except Exception:  # noqa: BLE001
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                               for e in reversed(keep)), encoding="utf-8")
        os.replace(tmp, self.path)


def image_sha1(path: str | os.PathLike, nbytes: int = 65536) -> str:
    """源图前 64KB 摘要（识别历史溯源；全文件哈希对截图去重足够但不必要）。"""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            h.update(f.read(nbytes))
    except OSError:
        return ""
    return h.hexdigest()[:12]


# ---------------------------------------------------------------- 开机自启

def autostart_command() -> str:
    """自启命令（布局自适应）：
    - 便携包：python\\pythonw.exe <pkgroot>\\app\\tray_app.py（sys.executable 同目录）
    - 开发机：src\\.venv\\Scripts\\pythonw.exe src\\tray_app.py
    一律取 **当前运行解释器同目录** 的 pythonw.exe + **settings.py 同目录** 的
    tray_app.py（两布局下该组合都正确，且不引用任何 spike 绝对路径）。
    """
    from pathlib import Path as _P
    import sys as _sys
    exe_dir = _P(_sys.executable).resolve().parent
    pyw = exe_dir / "pythonw.exe"
    if not pyw.exists():
        pyw = _P(_sys.executable).resolve()
    script = _P(__file__).resolve().parent / "tray_app.py"
    return f'"{pyw}" "{script}"'


def get_autostart_cmd() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return str(val)
    except OSError:
        return None


def set_autostart(enable: bool, dry_run: bool = False, log=print) -> bool:
    """写/删 HKCU Run 值。写前 dry-run 打印目标命令。返回操作后实际状态。"""
    cmd = autostart_command()
    if dry_run:
        log(f"[autostart] dry-run: {'写入' if enable else '删除'} "
            f"HKCU\\{RUN_KEY}\\{RUN_VALUE} = {cmd}")
        return get_autostart_cmd() is not None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                log(f"[autostart] 写入 HKCU\\{RUN_KEY}\\{RUN_VALUE} = {cmd}")
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                    log("[autostart] 已删除自启项")
                except FileNotFoundError:
                    pass
        return enable
    except OSError as e:
        log(f"[autostart] 注册表操作失败: {e}")
        return get_autostart_cmd() is not None


# ---------------------------------------------------------------- 临时产物

def cleanup_temp(max_age_hours: float = 24.0, log=print) -> int:
    """清理 %TEMP% 下 snipeq 截图/预览残留（>max_age_hours），返回删除条目数。

    - snipeq_shot_*.png：识别完成后原图无用（历史只存 sha1+latex）；
    - snipeq_preview / snipeq_preview*：prev_*.png 与 _edge_profile（Edge 锁
      目录整体删不掉时跳过，不阻塞启动）。
    """
    import glob
    import shutil

    temp = Path(os.environ.get("TEMP", tempfile_gettempdir()))
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    patterns = [str(temp / "snipeq_shot_*.png")]
    for pat in patterns:
        for f in glob.glob(pat):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
                    removed += 1
            except OSError:
                pass
    for d in sorted(glob.glob(str(temp / "snipeq_preview*"))):
        try:
            if os.path.getmtime(d) >= cutoff:
                continue
            if os.path.isdir(d):
                # 目录内逐个删旧文件（Edge profile 可能占用，静默跳过）
                for root, _dirs, files in os.walk(d, topdown=False):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        try:
                            if os.path.getmtime(fp) < cutoff:
                                os.remove(fp)
                                removed += 1
                        except OSError:
                            pass
            else:
                os.remove(d)
                removed += 1
        except OSError:
            pass
    if removed:
        log(f"[temp] 清理过期产物 {removed} 项（>{max_age_hours:.0f}h）")
    return removed


def tempfile_gettempdir() -> str:
    import tempfile
    return tempfile.gettempdir()
