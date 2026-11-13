"""B 站插件的本地状态：凭据、额度、草稿、审计、缓存、ffmpeg/ASR key。"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_dafeiyu_bili"
DATA_DIR = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
CACHE_DIR = DATA_DIR / "cache"
CRED_FILE = DATA_DIR / "credentials.json"
QUOTA_FILE = DATA_DIR / "quota.json"
DRAFTS_FILE = DATA_DIR / "drafts.json"
AUDIT_FILE = DATA_DIR / "audit.jsonl"
CMD_CONFIG = Path(get_astrbot_data_path()) / "cmd_config.json"

DEFAULT_QUOTAS = {"like": 20, "coin": 2, "comment": 3, "dynamic": 1, "share": 1,
                  "favorite": 10, "follow": 3, "dm": 3, "danmaku": 5,
                  "triple": 2, "watch_later": 20}
DEFAULT_GAPS = {"like": 60, "coin": 600, "comment": 300, "dynamic": 21600, "share": 3600,
                "favorite": 30, "follow": 60, "dm": 300, "danmaku": 60,
                "triple": 600, "watch_later": 5}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def _load(path: Path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path: Path, data, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    if private:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def today() -> str:
    return time.strftime("%Y-%m-%d")


def get_credentials() -> dict:
    d = _load(CRED_FILE, {})
    return d if isinstance(d, dict) else {}


def save_credentials(d: dict) -> None:
    _save(CRED_FILE, d, private=True)


def load_quota() -> dict:
    d = _load(QUOTA_FILE, {})
    if not isinstance(d, dict) or d.get("date") != today():
        d = {"date": today(), "counts": {}, "last": {}}
    d.setdefault("counts", {})
    d.setdefault("last", {})
    return d


def check_quota(action: str, cfg) -> tuple[bool, str]:
    q = load_quota()
    limit = int(_cfg(cfg, f"daily_{action}", DEFAULT_QUOTAS.get(action, 0)) or 0)
    gap = float(_cfg(cfg, f"min_gap_{action}", DEFAULT_GAPS.get(action, 0)) or 0)
    used = int(q["counts"].get(action, 0))
    if limit and used >= limit:
        return False, f"今天 {action} 已用 {used}/{limit}"
    last = float(q["last"].get(action, 0) or 0)
    wait = gap - (time.time() - last)
    if wait > 0:
        return False, f"{action} 还要等 {int(wait)} 秒（最小间隔 {int(gap)} 秒）"
    return True, ""


def record_quota(action: str) -> None:
    q = load_quota()
    q["counts"][action] = int(q["counts"].get(action, 0)) + 1
    q["last"][action] = time.time()
    _save(QUOTA_FILE, q)


def quota_summary(cfg) -> dict:
    q = load_quota()
    out = {}
    for a in ("like", "coin", "comment", "dynamic", "share", "favorite",
              "follow", "dm", "danmaku", "triple", "watch_later"):
        out[a] = {
            "used": int(q["counts"].get(a, 0)),
            "limit": int(_cfg(cfg, f"daily_{a}", DEFAULT_QUOTAS.get(a, 0)) or 0),
        }
    return out


def add_draft(kind: str, payload: dict) -> str:
    d = _load(DRAFTS_FILE, {})
    if not isinstance(d, dict):
        d = {}
    seq = int(d.get("_seq", 0)) + 1
    d["_seq"] = seq
    prefix = {"comment": "c", "coin": "k", "dynamic": "d", "share": "s",
              "dm": "m", "danmaku": "b"}.get(kind, "x")
    did = f"{prefix}{seq}"
    payload = dict(payload)
    payload.update({"id": did, "kind": kind, "created": time.time()})
    d[did] = payload
    _save(DRAFTS_FILE, d, private=True)
    return did


def get_draft(did: str) -> dict:
    d = _load(DRAFTS_FILE, {})
    item = d.get(did) if isinstance(d, dict) else None
    return item if isinstance(item, dict) else {}


def drop_draft(did: str) -> None:
    d = _load(DRAFTS_FILE, {})
    if isinstance(d, dict) and did in d:
        d.pop(did, None)
        _save(DRAFTS_FILE, d, private=True)


def audit(action: str, target: str, text: str, result: str, code=0) -> None:
    ensure_dirs()
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
           "target": target, "text": text[:1000], "result": str(result)[:500], "code": code}
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _cfg(cfg, key, default=None):
    try:
        v = cfg.get(key, default) if hasattr(cfg, "get") else None
        return default if v is None else v
    except Exception:
        return default


def find_ffmpeg(configured: str = "") -> str:
    cands = []
    if configured:
        cands.append(configured)
    w = shutil.which("ffmpeg")
    if w:
        cands.append(w)
    root = Path(get_astrbot_data_path()).parent
    cands += [
        str(root.parent / "ffmpeg" / "bin" / "ffmpeg.exe"),
        str(root / "ffmpeg" / "bin" / "ffmpeg.exe"),
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def mimo_key(configured: str = "") -> str:
    if configured:
        return configured.strip()
    env = (os.environ.get("XIAOMI_API_KEY") or "").strip()
    if env:
        return env
    try:
        d = _load(CMD_CONFIG, {})
        for p in d.get("provider", []) or []:
            if not isinstance(p, dict):
                continue
            pid = f"{p.get('id','')} {p.get('type','')}".lower()
            if ("mimo" in pid or "xiaomi" in pid) and p.get("api_key"):
                key = p.get("api_key")
                if isinstance(key, list):
                    key = key[0] if key else ""
                if key:
                    return str(key).strip()
    except Exception:
        pass
    try:
        f = Path(get_astrbot_data_path()).parent.parent / "private" / "xiaomi-key.txt"
        return f.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def clean_cache(hours: int) -> None:
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - max(1, int(hours)) * 3600
    for d in CACHE_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
