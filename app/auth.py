"""会话与登录限速。

会话持久化到 data/sessions.json：服务重启后已登录的浏览器不掉线。
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config as C


class SessionStore:
    """token -> 过期时间戳。落盘为 JSON，写入用原子替换。"""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._sessions = {}
        self._last_purge = 0.0
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        if isinstance(raw, dict):
            self._sessions = {k: float(v) for k, v in raw.items()
                              if isinstance(v, (int, float)) and float(v) > now}
        self._write_locked()

    def _write_locked(self):
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError:
            pass

    # ---------- 接口 ----------
    def issue(self) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._purge_locked()
            self._sessions[token] = time.time() + C.SESSION_TTL
            self._write_locked()
        return token

    def valid(self, token) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if time.time() > exp:
                self._sessions.pop(token, None)
                self._write_locked()
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            if self._sessions.pop(token, None) is not None:
                self._write_locked()

    def count(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._sessions)

    def _purge_locked(self):
        now = time.time()
        if now - self._last_purge < 300:
            return
        self._last_purge = now
        dead = [k for k, v in self._sessions.items() if v <= now]
        for k in dead:
            self._sessions.pop(k, None)
        if dead:
            self._write_locked()


class LoginThrottle:
    """同一 IP 连续失败超阈值后施加指数退避，摊薄爆破速率。"""

    def __init__(self):
        self._attempts = {}
        self._lock = threading.Lock()

    @staticmethod
    def _delay(count: int) -> float:
        if count <= C.LOGIN_MAX_ATTEMPTS:
            return 0.0
        return min(2.0, 0.5 * (2 ** (count - C.LOGIN_MAX_ATTEMPTS - 1)))

    MAX_ENTRIES = 2000            # 表上限，防被大量伪造 IP 撑爆内存

    def check(self, ip: str) -> float:
        """失败后应施加的延迟（秒）。"""
        now = time.time()
        with self._lock:
            count, start = self._attempts.get(ip, (0, now))
            if now - start > C.LOGIN_WINDOW:
                count, start = 0, now
            count += 1
            self._attempts[ip] = (count, start)
            if len(self._attempts) > self.MAX_ENTRIES:
                dead = [k for k, (c, s) in self._attempts.items()
                        if now - s > C.LOGIN_WINDOW]
                for k in dead:
                    self._attempts.pop(k, None)
                if len(self._attempts) > self.MAX_ENTRIES:
                    self._attempts.clear()
            return self._delay(count)

    def reset(self, ip: str):
        with self._lock:
            self._attempts.pop(ip, None)

    @staticmethod
    def verify(username: str, password: str) -> bool:
        return (username == C.USER and
                hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), C.PASS_HASH))


SESSIONS = SessionStore(C.SESSION_FILE)
THROTTLE = LoginThrottle()
