"""分享链接：生成免登录只读地址，可设有效期。

存储：data/shares.json（600），结构 {token: {path, created, expires}}
token 本身就是凭证（bearer），知道链接即可访问，不做额外鉴权。
"""
import json
import os
import secrets
import threading
import time

from . import config as C

_FILE = os.path.join(C.DATA_DIR, "shares.json")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict):
    tmp = _FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _FILE)
    except OSError:
        pass


def create(rel: str, days: int = 7):
    """生成分享 token，返回 (token, 过期时间戳)。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, C.SHARE_MAX_DAYS))
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        d = {k: v for k, v in _load().items() if v.get("expires", 0) > now}   # 顺手清过期
        d[token] = {"path": rel, "created": now, "expires": now + days * 86400}
        _save(d)
    return token, now + days * 86400


def resolve(token: str):
    """校验 token，返回记录（含 path）；无效/过期返回 None。"""
    if not token or len(token) > 64:
        return None
    with _lock:
        d = _load()
        v = d.get(token)
        if not v:
            return None
        if v.get("expires", 0) <= time.time():
            d.pop(token, None)
            _save(d)
            return None
        return v


def revoke(token: str) -> bool:
    with _lock:
        d = _load()
        if d.pop(token, None) is not None:
            _save(d)
            return True
    return False


def list_active():
    now = time.time()
    with _lock:
        d = _load()
        items = [dict(token=k, **v) for k, v in d.items() if v.get("expires", 0) > now]
    items.sort(key=lambda x: x.get("created", 0), reverse=True)
    return items
