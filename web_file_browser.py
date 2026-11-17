#!/usr/bin/env python3
"""aicbbuu网页文件浏览 · 服务端入口

纯 Python 标准库。模块划分：
  app/config   环境变量与常量
  app/auth     会话（落盘）与登录限速
  app/files    路径安全、目录扫描、缩略图、打包
  app/upload   流式 multipart 解析
  app/views    模板渲染
"""
import html
import json
import mimetypes
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import config as C             # noqa: E402
from app import files as F              # noqa: E402
from app import history as HIST         # noqa: E402
from app import share as SH           # noqa: E402
from app import views as V              # noqa: E402
from app.auth import SESSIONS, THROTTLE  # noqa: E402
from app.upload import parse_multipart, extract_boundary  # noqa: E402

MAX_JSON_BODY = 16 * 1024 * 1024        # 文本编辑等 JSON 接口上限

def _root() -> Path:
    return Path(C.RESOURCE_DIR)


def _rel_join(dir_rel, name):
    return "{}/{}".format(dir_rel, name) if dir_rel else name


class DualServer(ThreadingHTTPServer):
    """同一端口同时接受 HTTP 与 HTTPS：探测首字节分流。

    TLS 记录以 0x16(handshake) 开头，HTTP 方法均为 ASCII 字母；
    MSG_PEEK 只看不取，不消费数据。
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, ssl_ctx=None):
        self.ssl_ctx = ssl_ctx
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = self.socket.accept()
        if self.ssl_ctx is None:
            return sock, addr
        head = b""
        try:
            sock.settimeout(15)
            head = sock.recv(1, socket.MSG_PEEK)
        except OSError:
            head = b""
        finally:
            # 必须恢复阻塞模式：否则这条明文连接整个生命周期都被 15 秒超时约束，
            # 慢速上传或长时间 keep-alive 会莫名断连。
            try:
                sock.settimeout(None)
            except OSError:
                pass
        if head == b"\x16":
            return self.ssl_ctx.wrap_socket(sock, server_side=True), addr
        return sock, addr


def _plain_http_redirect(handler) -> bool:
    """明文 HTTP → 307 跳到 HTTPS。已处理返回 True。"""
    if getattr(handler.server, "ssl_ctx", None) is None:
        return False
    if isinstance(handler.connection, ssl.SSLSocket):
        return False
    host = handler.headers.get("Host") or "%s:%d" % handler.server.server_address[:2]
    handler.send_response(307)  # 用 307 不用 308：308 会被浏览器永久缓存，配置变动后甩不掉
    handler.send_header("Location", "https://" + host + handler.path)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AicbbuuWebFileBrowser/2.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=C.RESOURCE_DIR, **kwargs)

    # ================= 认证 =================
    def _cookie_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith(C.COOKIE_NAME + "="):
                return part[len(C.COOKIE_NAME) + 1:]
        return None

    def _authed(self) -> bool:
        return SESSIONS.valid(self._cookie_token())

    def _set_session_cookie(self, token):
        self.send_header("Set-Cookie", "{}={}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={}".format(
            C.COOKIE_NAME, token, C.SESSION_TTL))

    def _logout(self):
        SESSIONS.revoke(self._cookie_token())
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "{}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0".format(
            C.COOKIE_NAME))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login_post(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 65536:
            return self._send_login("请求过大")
        body = self.rfile.read(length).decode("utf-8", "replace")
        q = urllib.parse.parse_qs(body, keep_blank_values=True)
        username = (q.get("username") or [""])[0]
        password = (q.get("password") or [""])[0]
        ip = self.client_address[0]

        if THROTTLE.verify(username, password):
            THROTTLE.reset(ip)
            token = SESSIONS.issue()
            self.send_response(302)
            self.send_header("Location", "/")
            self._set_session_cookie(token)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        delay = THROTTLE.check(ip)
        if delay > 0:
            time.sleep(delay)
        self._send_login("用户名或密码错误")

    # ================= 输出辅助 =================
    def _send_html(self, text, status=200, cache=False):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache" if cache
                         else "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json_error(self, status, msg):
        self._send_json({"error": msg}, status)

    def _err(self, status, msg):
        """带中文说明的错误页。

        HTTP 状态行只能是 latin-1，直接给 send_error 传中文会抛 UnicodeEncodeError
        并掐断连接（浏览器显示空白页）。所以状态行只放数字，说明写进 UTF-8 body。
        """
        body = ("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>{0}</title><style>body{{margin:0;height:100vh;display:grid;"
                "place-items:center;background:#05080e;color:#e6edf3;"
                "font-family:system-ui,-apple-system,'PingFang SC',sans-serif}}"
                ".box{{text-align:center;padding:32px}}.code{{font-size:44px;"
                "font-weight:700;background:linear-gradient(135deg,#22d3ee,#a855f7);"
                "-webkit-background-clip:text;background-clip:text;color:transparent}}"
                "p{{color:#9db0c8;margin-top:12px;font-size:14px}}"
                "a{{color:#22d3ee;text-decoration:none;font-size:13px;"
                "display:inline-block;margin-top:18px}}"
                "</style></head><body><div class=\"box\"><div class=\"code\">{0}</div>"
                "<p>{1}</p><a href=\"/\">← 回到文件库</a></div></body></html>"
                ).format(status, html.escape(msg))
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_login(self, error=""):
        self._send_html(V.render_login(error))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return None
        if length > MAX_JSON_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # ================= 静态资源（免认证） =================
    def _serve_static(self, rel, head_only=False):
        rel = rel.lstrip("/")
        if not rel or ".." in rel.split("/"):
            return self.send_error(404)
        path = os.path.join(V.STATIC_DIR, rel)
        if not os.path.isfile(path):
            return self.send_error(404)
        ext = os.path.splitext(path)[1].lower()
        mime = {".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon",
                ".woff2": "font/woff2"}.get(ext, "application/octet-stream")
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if head_only:
            return
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, C.CHUNK_SIZE)

    # ================= 文件输出（支持 Range） =================
    def _send_file(self, path: Path):
        ext = os.path.splitext(path.name)[1].lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
                "bmp": "image/bmp", "svg": "image/svg+xml",
                "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime",
                "m4v": "video/x-m4v", "mkv": "video/x-matroska",
                "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
                "flac": "audio/flac", "m4a": "audio/mp4", "opus": "audio/opus",
                "pdf": "application/pdf"}.get(ext)
        if not mime:
            mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if mime.startswith("text/") or mime in {
                    "application/json", "application/xml", "application/javascript",
                    "application/x-javascript", "application/typescript",
                    "application/xhtml+xml"}:
                mime += "; charset=utf-8"

        size = path.stat().st_size
        cache = ("no-cache, no-store, must-revalidate"
                 if "." + ext in C.EDITABLE_EXTS else "max-age=3600")

        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes=") and size > 0:
            try:
                spec = rng[6:].split(",")[0].strip()
                a, _, b = spec.partition("-")
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */{}".format(size))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
            except (ValueError, IndexError):
                start, end, partial = 0, size - 1, False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(C.CHUNK_SIZE, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _send_attachment(self, path: Path, name: str):
        encoded = urllib.parse.quote(name, safe="")
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"download\"; filename*=UTF-8''{}".format(encoded))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(C.CHUNK_SIZE):
                self.wfile.write(chunk)

    # ================= 缩略图 =================
    def _handle_thumb(self, query):
        params = dict(urllib.parse.parse_qsl(query))
        rel = params.get("path", "")
        try:
            width = int(params.get("w", "320"))
        except ValueError:
            width = 320
        if width not in C.THUMB_WIDTHS:
            width = C.THUMB_WIDTHS[0]
        src = F.safe_path(rel)
        if not src or not src.is_file():
            return self.send_error(404)
        ext = os.path.splitext(src.name)[1].lower()
        if ext not in C.IMAGE_EXTS and ext not in C.VIDEO_EXTS:
            return self.send_error(415)
        if ext == ".svg":                     # 矢量图无需缩略，直接给原文件
            return self._send_file(src)
        try:                                  # 小图直接给原文件，省掉一次转换
            if os.path.getsize(src) <= C.SMALL_IMAGE_DIRECT:
                return self._send_file(src)
        except OSError:
            pass
        out = F.ensure_thumb(src, width)
        if not out:
            return self._err(500, "缩略图生成失败")
        size = os.path.getsize(out)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        with open(out, "rb") as f:
            shutil.copyfileobj(f, self.wfile, C.CHUNK_SIZE)

    # ================= JSON API =================
    def _handle_api_list(self, query):
        params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        rel = params.get("path", "").strip("/")
        sort = params.get("sort", "name")
        order = params.get("order", "asc")
        q = params.get("q", "").strip()
        recursive = params.get("recursive") == "1"
        search_content = params.get("content") == "1"
        try:
            page = int(params.get("page", "1"))
        except ValueError:
            page = 1

        base = F.safe_path(rel)
        if base is None or not base.exists():
            return self._send_json_error(404, "目录不存在")

        parts = [seg for seg in rel.split("/") if seg]
        crumbs = V.crumbs_for(parts)

        complete = True
        if q and recursive and search_content:
            start_dir = base if base.is_dir() else base.parent
            entries, complete = F.search_content(start_dir, q)
            entries = F.sort_entries(entries, sort, order)
            total, total_pages, page = len(entries), 1, 1
        elif q and recursive:
            entries = F.search_recursive(base if base.is_dir() else base.parent, q)
            entries = F.sort_entries(entries, sort, order)
            total, total_pages, page = len(entries), 1, 1
        else:
            if not base.is_dir():
                return self._send_json_error(400, "不是目录")
            entries = [e for e in F.scan_entries(base) if q.lower() in e["name"].lower()] if q \
                else F.scan_entries(base)
            entries = F.sort_entries(entries, sort, order)
            for e in entries:
                e["rel"] = _rel_join(rel, e["name"])
            total = len(entries)
            if q:
                total_pages, page = 1, 1
            else:
                total_pages = max(1, (total + C.PAGE_SIZE - 1) // C.PAGE_SIZE)
                page = max(1, min(page, total_pages))
                start = (page - 1) * C.PAGE_SIZE
                entries = entries[start:start + C.PAGE_SIZE]

        return self._send_json({
            "path": rel,
            "crumbs": crumbs,
            "entries": entries,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "recursive": bool(q and recursive),
            "content_search": bool(q and recursive and search_content),
            "complete": complete,
            "stats": F.library_stats(),
            "disk": F.disk_usage(),
        })

    def _handle_api_stat(self, query):
        params = dict(urllib.parse.parse_qsl(query))
        p = F.safe_path(params.get("path", ""))
        if not p or not p.exists():
            return self._send_json_error(404, "不存在")
        st = p.stat()
        data = {"rel": F.rel_of(p), "name": p.name, "is_dir": p.is_dir(),
                "size": 0 if p.is_dir() else st.st_size, "mtime": st.st_mtime,
                "mode": oct(st.st_mode & 0o777)}
        if p.is_dir():
            try:
                data["children"] = sum(1 for _ in os.scandir(p))
            except OSError:
                data["children"] = 0
        return self._send_json(data)

    # ================= GET =================
    def do_GET(self):
        if _plain_http_redirect(self):
            return
        parsed = urllib.parse.urlparse(self.path)
        raw_path, query = parsed.path, parsed.query

        if raw_path == "/logout":
            return self._logout()
        if raw_path.startswith("/static/"):
            return self._serve_static(raw_path[len("/static/"):])
        if raw_path.startswith("/s/"):
            return self._handle_shared(raw_path[3:].strip("/"))
        if raw_path == "/favicon.ico":
            self.send_response(302)
            self.send_header("Location", "/static/favicon.svg")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self._authed():
            if raw_path.startswith("/api/") or raw_path in ("/thumb", "/edit"):
                return self._send_json_error(401, "Unauthorized")
            return self._send_login()

        if raw_path == "/api/list":
            try:
                return self._handle_api_list(query)
            except Exception as exc:                      # noqa: BLE001
                return self._send_json_error(500, "列目录失败: {}".format(exc))
        if raw_path == "/api/stat":
            return self._handle_api_stat(query)
        if raw_path == "/api/trash":
            return self._handle_api_trash()
        if raw_path == "/api/shares":
            return self._handle_share_list()
        if raw_path == "/api/history":
            return self._handle_history_list(query)
        if raw_path == "/api/history/get":
            return self._handle_history_get(query)
        if raw_path == "/thumb":
            try:
                return self._handle_thumb(query)
            except Exception as exc:                      # noqa: BLE001
                return self._err(500, "缩略图生成失败：{}".format(exc))
        if raw_path == "/edit":
            try:
                return self._handle_get_edit(query)
            except Exception as exc:                      # noqa: BLE001
                return self._err(500, "编辑器渲染失败：{}".format(exc))

        rel = urllib.parse.unquote(raw_path).strip("/")
        if rel == "":
            return self._send_html(V.render_list())
        safe = F.safe_path(rel)
        if not safe or not safe.exists():
            return self.send_error(404)
        if safe.is_dir():
            if not raw_path.endswith("/"):
                target = V.url_path(rel, trailing_slash=True)
                if query:
                    target += "?" + query
                self.send_response(301)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send_html(V.render_list())
        return self._send_file(safe)

    def _handle_get_edit(self, query):
        params = dict(urllib.parse.parse_qsl(query))
        rel = params.get("path", "")
        if not rel:
            return self._err(400, "缺少 path 参数")
        f = F.safe_path(rel)
        if not f or not f.is_file():
            return self._err(404, "文件不存在")
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in C.EDITABLE_EXTS:
            return self._err(400, "该类型（{}）不支持在线编辑".format(ext or "无扩展名"))
        try:
            fsize = f.stat().st_size
        except OSError:
            fsize = 0
        if fsize > C.MAX_EDIT_BYTES:
            return self._err(
                413, "文件 {} 超过在线编辑上限 {}，请下载后编辑".format(
                    C.fmt_size(fsize), C.fmt_size(C.MAX_EDIT_BYTES)))
        try:
            content = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._err(400, "文件不是 UTF-8 文本，无法在线编辑")
        except OSError as exc:
            return self._err(500, "读取失败：{}".format(exc))
        return self._send_html(V.render_editor(rel.lstrip("/"), f.name, content), cache=True)

    # ================= HEAD =================
    def do_HEAD(self):
        """只回响应头。

        必须自己实现：基类 SimpleHTTPRequestHandler 的 do_HEAD 会按 directory
        参数直接服务文件，完全绕过这里的会话校验（未登录也能探测文件是否存在、多大）。
        """
        if _plain_http_redirect(self):
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/static/"):
            return self._serve_static(parsed.path[len("/static/"):], head_only=True)
        if not self._authed():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # 已登录也只回固定头，不暴露文件大小/存在性
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ================= POST =================
    def do_POST(self):
        if _plain_http_redirect(self):
            return
        raw_path = urllib.parse.urlparse(self.path).path

        if raw_path == "/login":
            return self._handle_login_post()
        if raw_path == "/logout":
            return self._logout()
        if not self._authed():
            return self._send_json_error(401, "Unauthorized")

        routes = {
            "/upload": self._handle_upload,
            "/mkdir": self._handle_mkdir,
            "/delete": self._handle_delete,
            "/rename": self._handle_rename,
            "/edit": self._handle_edit,
            "/download": self._handle_download,
            "/copy": lambda: self._handle_copy_or_move(True),
            "/move": lambda: self._handle_copy_or_move(False),
            "/trash/restore": self._handle_trash_restore,
            "/trash/purge": self._handle_trash_purge,
            "/trash/empty": self._handle_trash_empty,
            "/share": self._handle_share_create,
            "/share/revoke": self._handle_share_revoke,
            "/history/restore": self._handle_history_restore,
        }
        fn = routes.get(raw_path)
        if not fn:
            return self.send_error(404)
        try:
            return fn()
        except ValueError as exc:
            return self._send_json_error(400, str(exc))
        except Exception as exc:                          # noqa: BLE001
            return self._send_json_error(500, "操作失败: {}".format(exc))

    # ---------- 上传 ----------
    def _handle_upload(self):
        boundary = extract_boundary(self.headers.get("Content-Type", ""))
        if not boundary:
            return self._send_json_error(400, "需要 multipart/form-data")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return self._send_json_error(400, "空请求")

        tmpd = F.tmp_dir()
        fields, files = {}, []
        try:
            fields, files = parse_multipart(self.rfile, length, boundary, tmpd)
            overwrite = str(fields.get("overwrite", "")).lower() in ("1", "true", "yes")
            target_rel = fields.get("dir", "")
            target_dir = F.safe_path(target_rel)
            if target_dir is None:
                return self._send_json_error(400, "目标目录非法")
            if not target_dir.is_dir():
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    return self._send_json_error(400, "目标目录不存在")

            saved, conflicts = 0, []
            for _field, filename, tmp, _size in files:
                name = os.path.basename(filename.replace("\\", "/"))
                if not name:
                    continue
                dest = target_dir / name
                try:
                    dest.resolve().relative_to(target_dir.resolve())
                except (ValueError, OSError):
                    continue
                if dest.exists() and dest.is_dir():
                    conflicts.append(name + "（同名文件夹，跳过）")
                    continue
                if dest.exists() and not overwrite:
                    conflicts.append(name)
                    continue
                try:
                    shutil.move(tmp, str(dest))
                    saved += 1
                except OSError:
                    continue

            if saved == 0 and not conflicts:
                return self._send_json_error(400, "没有有效的文件")
            resp = {"msg": "已上传 {} 个文件".format(saved), "saved": saved}
            if conflicts:
                resp["conflict"] = conflicts
            return self._send_json(resp)
        finally:
            for _f, _n, tmp, _s in files:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # ---------- 新建目录 ----------
    def _handle_mkdir(self):
        data = self._read_json() or {}
        parent_rel = data.get("dir", "") or ""
        name = data.get("name", "") or ""
        if not isinstance(parent_rel, str) or not isinstance(name, str):
            return self._send_json_error(400, "参数类型错误")
        name = name.strip()
        if not name:
            return self._send_json_error(400, "缺少文件夹名")
        if (name in (".", "..") or os.path.basename(name) != name
                or "/" in name or "\\" in name or "\0" in name):
            return self._send_json_error(400, "名称非法")
        parent = F.safe_path(parent_rel)
        if not parent or not parent.is_dir():
            return self._send_json_error(400, "父目录不存在")
        try:
            (parent / name).mkdir(exist_ok=False)
            return self._send_json({"msg": "文件夹已创建"})
        except FileExistsError:
            return self._send_json_error(400, "同名文件夹已存在")
        except OSError as exc:
            return self._send_json_error(500, str(exc))

    # ---------- 删除（进回收站，可还原） ----------
    def _handle_delete(self):
        data = self._read_json() or {}
        paths = data.get("paths") or []
        if not isinstance(paths, list):
            return self._send_json_error(400, "paths 必须是数组")
        root = _root().resolve()
        targets = []
        for rel in paths:
            p = F.safe_path(rel)
            if p and p.exists() and p.resolve() != root:
                targets.append(p)
        if not targets:
            return self._send_json_error(400, "没有可删除的项")
        moved, errors = F.trash_move(targets)
        if not moved:
            return self._send_json_error(400, "删除失败：{}".format("；".join(errors[:3])))
        msg = "已移入回收站 {} 项".format(len(moved))
        if errors:
            msg += "（{} 项失败）".format(len(errors))
        return self._send_json({"msg": msg, "deleted": len(moved), "trashed": True})

    # ---------- 编辑历史 ----------
    def _handle_history_list(self, query):
        params = dict(urllib.parse.parse_qsl(query))
        rel = params.get("path", "")
        f = F.safe_path(rel)
        if not f:
            return self._send_json_error(400, "路径非法")
        return self._send_json({"path": rel, "versions": HIST.list_versions(F.rel_of(f))})

    def _handle_history_get(self, query):
        params = dict(urllib.parse.parse_qsl(query))
        rel = params.get("path", "")
        try:
            ts = int(params.get("ts", "0"))
        except ValueError:
            return self._send_json_error(400, "时间戳非法")
        f = F.safe_path(rel)
        if not f:
            return self._send_json_error(400, "路径非法")
        text = HIST.read_version(F.rel_of(f), ts)
        if text is None:
            return self._send_json_error(404, "该版本不存在")
        return self._send_json({"path": rel, "ts": ts, "content": text})

    def _handle_history_restore(self):
        data = self._read_json() or {}
        rel = data.get("path", "")
        try:
            ts = int(data.get("ts", 0))
        except (TypeError, ValueError):
            return self._send_json_error(400, "时间戳非法")
        if not isinstance(rel, str) or not rel:
            return self._send_json_error(400, "缺少路径")
        f = F.safe_path(rel)
        if not f:
            return self._send_json_error(400, "路径非法")
        text = HIST.read_version(F.rel_of(f), ts)
        if text is None:
            return self._send_json_error(404, "该版本不存在")
        try:
            if f.exists():                        # 还原前把当前内容也存一份
                try:
                    HIST.note(F.rel_of(f), f.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    pass
            f.write_text(text, encoding="utf-8")
        except OSError as exc:
            return self._send_json_error(500, str(exc))
        return self._send_json({"msg": "已还原到该版本"})

    # ---------- 分享链接 ----------
    def _base_url(self):
        host = self.headers.get("Host") or "{}:{}".format(*self.server.server_address[:2])
        scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        return "{}://{}".format(scheme, host)

    def _handle_share_create(self):
        data = self._read_json() or {}
        rel = data.get("path", "")
        if not isinstance(rel, str) or not rel:
            return self._send_json_error(400, "缺少路径")
        p = F.safe_path(rel)
        if not p or not p.exists():
            return self._send_json_error(404, "文件不存在")
        token, expires = SH.create(F.rel_of(p), data.get("days", 7))
        return self._send_json({
            "token": token,
            "url": "{}/s/{}".format(self._base_url(), token),
            "expires": expires,
        })

    def _handle_share_list(self):
        items = SH.list_active()
        for it in items:
            it["url"] = "{}/s/{}".format(self._base_url(), it["token"])
        return self._send_json({"items": items, "total": len(items)})

    def _handle_share_revoke(self):
        data = self._read_json() or {}
        token = data.get("token", "")
        if not isinstance(token, str) or not SH.revoke(token):
            return self._send_json_error(400, "链接不存在")
        return self._send_json({"msg": "分享链接已撤销"})

    def _handle_shared(self, token):
        """免登录只读访问（分享链接入口，不走会话）。"""
        rec = SH.resolve(token)
        if not rec:
            return self._err(404, "链接无效或已过期")
        p = F.safe_path(rec.get("path", ""))
        if not p or not p.exists():
            return self._err(404, "文件已被移动或删除")
        if p.is_file():
            return self._send_attachment(p, p.name)
        # 目录 → 打包下载
        tmp_zip = None
        try:
            tmp_zip, name = F.zip_to_tmp([p], F.tmp_dir())
            size = os.path.getsize(tmp_zip)
            encoded = urllib.parse.quote(name, safe="")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"download.zip\"; filename*=UTF-8''{}".format(encoded))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(tmp_zip, "rb") as f:
                shutil.copyfileobj(f, self.wfile, C.CHUNK_SIZE)
        except OSError as exc:
            return self._err(500, "打包失败：{}".format(exc))
        finally:
            if tmp_zip and os.path.exists(tmp_zip):
                try:
                    os.unlink(tmp_zip)
                except OSError:
                    pass

    # ---------- 回收站 ----------
    def _handle_api_trash(self):
        items = F.trash_list()
        total_size = sum(m.get("size", 0) for m in items)
        return self._send_json({
            "items": items,
            "total": len(items),
            "bytes": total_size,
            "keep_days": C.TRASH_KEEP_DAYS,
        })

    def _handle_trash_restore(self):
        data = self._read_json() or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return self._send_json_error(400, "未指定条目")
        restored, errors = F.trash_restore(ids)
        msg = "已还原 {} 项".format(len(restored))
        if errors:
            msg += "（{} 项失败：{}）".format(len(errors), errors[0])
        return self._send_json({"msg": msg, "restored": len(restored), "errors": errors})

    def _handle_trash_purge(self):
        data = self._read_json() or {}
        ids = data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return self._send_json_error(400, "未指定条目")
        n, freed = F.trash_purge(ids)
        return self._send_json({"msg": "已彻底删除 {} 项，释放 {}".format(n, C.fmt_size(freed)),
                                "purged": n})

    def _handle_trash_empty(self):
        n, freed = F.trash_empty()
        return self._send_json({"msg": "回收站已清空：{} 项，释放 {}".format(n, C.fmt_size(freed)),
                                "purged": n})

    # ---------- 重命名 ----------
    def _handle_rename(self):
        data = self._read_json() or {}
        src = data.get("src", "")
        dst = data.get("dst", "")
        overwrite = bool(data.get("overwrite", False))
        if not isinstance(src, str) or not isinstance(dst, str):
            return self._send_json_error(400, "参数类型错误")
        if not src or not dst:
            return self._send_json_error(400, "缺少 src/dst")
        sp, dp = F.safe_path(src), F.safe_path(dst)
        if not sp or not dp:
            return self._send_json_error(400, "路径非法")
        root = _root().resolve()
        if sp.resolve() == root or dp.resolve() == root:
            return self._send_json_error(400, "不能重命名根目录")
        if not sp.exists():
            return self._send_json_error(404, "源不存在")
        if dp.exists() and not overwrite:
            return self._send_json({"conflict": True, "src": src, "dst": dst})
        try:
            dp.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if overwrite and dp.exists():
            try:
                shutil.rmtree(dp) if dp.is_dir() else dp.unlink()
            except OSError as exc:
                return self._send_json_error(500, str(exc))
        try:
            sp.rename(dp)
            return self._send_json({"msg": "已重命名为 {}".format(dp.name)})
        except OSError as exc:
            return self._send_json_error(500, str(exc))

    # ---------- 编辑 / 新建文件 ----------
    def _handle_edit(self):
        data = self._read_json()
        if data is None:
            return self._send_json_error(400, "缺少请求体")
        rel = data.get("path", "")
        content = data.get("content", "")
        if not isinstance(rel, str) or not isinstance(content, str):
            return self._send_json_error(400, "参数类型错误")
        if not rel:
            return self._send_json_error(400, "缺少路径")
        f = F.safe_path(rel)
        if not f:
            return self._send_json_error(400, "路径非法")
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in C.EDITABLE_EXTS:
            return self._send_json_error(400, "该类型不支持在线编辑")
        created = not f.exists()
        if created and not f.parent.is_dir():
            return self._send_json_error(400, "父目录不存在")
        try:
            if not created:                       # 覆盖前留一份旧版本
                try:
                    old = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    old = None
                if old is not None:
                    HIST.note(F.rel_of(f), old)
            f.write_text(content, encoding="utf-8")
            return self._send_json({"msg": "已创建" if created else "已保存"})
        except OSError as exc:
            return self._send_json_error(500, str(exc))

    # ---------- 复制 / 移动 ----------
    def _handle_copy_or_move(self, copy_mode):
        data = self._read_json() or {}
        paths = data.get("paths") or []
        dest = data.get("dest", "") or ""
        if not isinstance(paths, list) or not paths:
            return self._send_json_error(400, "未指定条目")
        dest_dir = F.safe_path(dest)
        if not dest_dir or not dest_dir.is_dir():
            return self._send_json_error(400, "目标目录非法")
        root = _root().resolve()
        dest_res = dest_dir.resolve()
        done, errors = 0, []
        for rel in paths:
            src = F.safe_path(rel)
            if not src or not src.exists():
                errors.append("{}: 不存在".format(rel))
                continue
            src_res = src.resolve()
            if src_res == root:
                errors.append("{}: 不能操作根目录".format(rel))
                continue
            target = dest_dir / src.name
            if target.resolve() == src_res:
                if copy_mode:                      # 同目录复制 → 自动改名生成副本
                    target = F.unique_copy_name(dest_dir, src.name)
                else:
                    errors.append("{}: 源与目标相同".format(rel))
                    continue
            if src.is_dir() and (src_res == dest_res or src_res in dest_res.parents):
                errors.append("{}: 不能放入自身内部".format(rel))
                continue
            if target.exists():
                errors.append("{}: 目标已存在".format(rel))
                continue
            try:
                if copy_mode:
                    shutil.copytree(src, target) if src.is_dir() else shutil.copy2(src, target)
                else:
                    shutil.move(str(src), str(target))
                done += 1
            except OSError as exc:
                errors.append("{}: {}".format(rel, exc))
        verb = "复制" if copy_mode else "移动"
        msg = "{} {} 项".format(verb, done)
        if errors:
            msg += "；跳过 {} 项".format(len(errors))
        return self._send_json({"msg": msg, "done": done, "errors": errors})

    # ---------- 下载 ----------
    def _handle_download(self):
        data = self._read_json() or {}
        paths = data.get("paths") or []
        if not isinstance(paths, list) or not paths:
            return self._send_json_error(400, "未指定条目")
        root = _root().resolve()
        valid = [p for p in (F.safe_path(r) for r in paths)
                 if p and p.exists() and p.resolve() != root]
        if not valid:
            return self._send_json_error(400, "没有有效条目")
        if len(valid) == 1 and valid[0].is_file():
            return self._send_attachment(valid[0], valid[0].name)

        tmpd = F.tmp_dir()
        try:
            tmp_zip, name = F.zip_to_tmp(valid, tmpd)
        except OSError as exc:
            return self._send_json_error(500, "打包失败: {}".format(exc))
        try:
            size = os.path.getsize(tmp_zip)
            encoded = urllib.parse.quote(name, safe="")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"download.zip\"; filename*=UTF-8''{}".format(encoded))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(tmp_zip, "rb") as f:
                shutil.copyfileobj(f, self.wfile, C.CHUNK_SIZE)
        finally:
            try:
                os.unlink(tmp_zip)
            except OSError:
                pass

    # ---------- 断连兜底 / 日志 ----------
    def handle_one_request(self):
        """客户端中途断开（关标签页、取消下载）是常态，不该刷堆栈。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.client_address[0], self.log_date_time_string(), fmt % args))


def _janitor():
    """后台清理：上传中转残留 + 缩略图缓存超额。

    原来只在启动时清一次，长期运行的服务会一直攒。
    """
    while True:
        time.sleep(C.CLEANUP_INTERVAL)
        try:
            F.cleanup_tmp()
            F.prune_thumbs()
            F.trash_auto_clean()
            HIST.prune_orphans(str(C.RESOURCE_DIR), F.trash_dir())
        except Exception:                                 # noqa: BLE001
            pass


def main():
    os.makedirs(C.RESOURCE_DIR, exist_ok=True)
    ctx = None
    if C.TLS_ENABLE and os.path.isfile(C.TLS_CERT) and os.path.isfile(C.TLS_KEY):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(C.TLS_CERT, C.TLS_KEY)
        except Exception as exc:                          # noqa: BLE001
            print("TLS 初始化失败，只用 HTTP: {}".format(exc))
            ctx = None

    F.cleanup_tmp()
    F.prune_thumbs()
    threading.Thread(target=_janitor, daemon=True).start()

    server = DualServer((C.HOST, C.PORT), Handler, ctx)
    scheme = "https" if ctx else "http"
    print("🚀 aicbbuu网页文件浏览 {}://{}:{}".format(scheme, C.HOST, C.PORT))
    if ctx:
        print("   HTTP 请求自动 307 跳转 HTTPS")
    print("   Serving: {}".format(C.RESOURCE_DIR))
    print("   Auth: user {} (password from RESOURCE_PASSWORD)".format(C.USER))
    print("   会话文件: {}".format(C.SESSION_FILE))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
