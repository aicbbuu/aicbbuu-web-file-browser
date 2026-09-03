#!/usr/bin/env python3
"""Resource server with auth, listing, upload per directory, mkdir, delete, rename, edit.
Uses only Python standard library.
"""
import base64, hashlib, hmac, html, io, json, os, mimetypes, secrets, shutil, threading, time, urllib.parse, zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RESOURCE_DIR = os.path.expanduser(os.environ.get("RESOURCE_DIR", "~/resource"))
HOST = os.environ.get("RESOURCE_HOST", "0.0.0.0")
PORT = int(os.environ.get("RESOURCE_PORT", "60010"))

USER = os.environ.get("RESOURCE_USER", "admin")
_PASSWORD_ENV = os.environ.get("RESOURCE_PASSWORD", "").strip()
if not _PASSWORD_ENV:
    raise SystemExit("RESOURCE_PASSWORD environment variable is required. Set a strong password before starting.")
PASSWORD = _PASSWORD_ENV
PASS_HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()

CHUNK_SIZE = 64 * 1024

# 可在线编辑的文本扩展名（集中定义，避免多处硬编码导致的漏改/不一致）
EDITABLE_EXTS = {".txt", ".html", ".css", ".js", ".json", ".md", ".yaml", ".yml",
                 ".toml", ".xml", ".svg", ".log", ".py", ".pyw", ".sh", ".bash",
                 ".zsh", ".bsh", ".pl", ".rb", ".php", ".bat", ".cmd", ".awk",
                 ".sed", ".sql", ".ini", ".conf", ".cfg"}

# 会话存储: token -> 过期时间戳
SESSION_TTL = 12 * 3600          # 会话有效期 12 小时
_SESSIONS = {}                   # token -> expiry (ts)
_COOKIE_NAME = "resource_session"

# 目录列表分页:每页条数(超过则翻页)
PAGE_SIZE = 200

# 登录失败限速:同一 IP 连续失败超过阈值后,每次失败递增延迟(指数退避摊薄)
LOGIN_MAX_ATTEMPTS = 5           # 阈值:连续失败 5 次后才开始限速
LOGIN_WINDOW = 900               # 连续失败窗口(秒),超时后计数重置,避免误锁正常用户
_LOGIN_ATTEMPTS = {}             # client_ip -> (count, window_start_ts)
_LOGIN_LOCK = threading.Lock()


def _login_delay(count: int) -> float:
    """连续失败 count 次后应施加的延迟;低于阈值返回 0,超过后指数递增(0.5→1→2s)、封顶 2s。"""
    if count <= LOGIN_MAX_ATTEMPTS:
        return 0.0
    return min(2.0, 0.5 * (2 ** (count - LOGIN_MAX_ATTEMPTS - 1)))


class AuthFileHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=RESOURCE_DIR, **kwargs)

    # ---------- Auth (Cookie session) ----------
    def _get_token(self):
        """从 Cookie 头解析会话 token"""
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            part = part.strip()
            if part.startswith(_COOKIE_NAME + "="):
                return part[len(_COOKIE_NAME) + 1:]
        return None

    def _is_authenticated(self):
        """检查当前请求是否持有有效会话"""
        token = self._get_token()
        if not token:
            return False
        exp = _SESSIONS.get(token)
        if exp is None:
            return False
        if time.time() > exp:
            _SESSIONS.pop(token, None)
            return False
        return True

    def _check_auth(self):
        # 保持外部接口不变：返回 True/False
        return self._is_authenticated()

    def _issue_session(self):
        """生成会话 token 并返回（调用方负责在 send_response 后写 Set-Cookie）"""
        token = secrets.token_urlsafe(24)
        _SESSIONS[token] = time.time() + SESSION_TTL
        return token

    def _logout(self):
        token = self._get_token()
        if token:
            _SESSIONS.pop(token, None)
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie",
                          "{}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0".format(_COOKIE_NAME))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login(self):
        """处理登录 POST：校验账号密码，成功则种 cookie 并 302 到首页"""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        fields = {}
        for pair in body.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                fields[k] = urllib.parse.unquote_plus(v)
        username = fields.get("username", "")
        password = fields.get("password", "")
        ok = (username == USER and
              hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), PASS_HASH))
        client_ip = self.client_address[0]
        if ok:
            # 登录成功:清空该 IP 的失败计数
            with _LOGIN_LOCK:
                _LOGIN_ATTEMPTS.pop(client_ip, None)
            token = self._issue_session()
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                              "{}={}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}".format(
                                  _COOKIE_NAME, token, SESSION_TTL))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            # 登录失败:记录并限速(连续失败超阈值后,失败响应前 sleep,摊薄爆破速率)
            count = self._record_login_failure(client_ip)
            delay = _login_delay(count)
            if delay > 0:
                time.sleep(delay)
            self._send_login(error="用户名或密码错误")

    def _record_login_failure(self, ip: str) -> int:
        """记录一次登录失败,返回该 IP 连续失败次数(超过窗口自动重置)。"""
        with _LOGIN_LOCK:
            count, window_start = _LOGIN_ATTEMPTS.get(ip, (0, time.time()))
            if time.time() - window_start > LOGIN_WINDOW:
                count, window_start = 0, time.time()
            count += 1
            _LOGIN_ATTEMPTS[ip] = (count, window_start)
            return count

    def _send_login(self, error=""):
        """返回暗黑风格登录页"""
        err_html = '<div class="err">{}</div>'.format(html.escape(error)) if error else ""
        page = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>aicbbuu-web-file-browser - 登录</title>
<style>
html{font-size:90%}
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Roboto,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login-card{background:#161616;border:1px solid #2a2a2a;border-radius:20px;padding:40px 44px;width:380px;box-shadow:0 24px 60px rgba(0,0,0,0.6)}
.login-card h1{font-size:1.5rem;color:#ffffff;margin-bottom:40px;text-align:center;font-weight:700}
.login-card .sub{color:#9e9e9e;font-size:0.9rem;text-align:center;margin-bottom:28px}
.field{margin-bottom:18px}
.field label{display:block;color:#cfcfcf;font-size:1rem;margin-bottom:8px;font-weight:600}
.field input{width:100%;padding:12px 14px;border:1px solid #2e2e2e;border-radius:12px;background:#0a0a0a;color:#f5f5f5;font-size:1.05rem}
.field input:focus{outline:none;border-color:#e63946;box-shadow:0 0 0 3px rgba(230,57,70,0.15)}
button{width:100%;padding:13px;border:none;border-radius:12px;background:#e63946;color:#fff;font-size:1.05rem;font-weight:700;cursor:pointer;margin-top:6px;transition:background 0.2s}
button:hover{background:#d62839}
.err{color:#e63946;text-align:center;margin-bottom:14px}
</style>
</head>
<body>
<div class="login-card">
<h1>aicbbuu-web-file-browser</h1>
""" + err_html + """
<form method="post" action="/login">
<div class="field"><label>用户名</label><input type="text" name="username" autofocus required></div>
<div class="field"><label>密码</label><input type="password" name="password" required></div>
<button type="submit">登 录</button>
</form>
</div>
</body>
</html>"""
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- Path safety ----------
    def _safe_path(self, rel_path: str):
        try:
            p = (Path(RESOURCE_DIR) / rel_path.lstrip("/")).resolve()
            p.relative_to(Path(RESOURCE_DIR).resolve())
            return p
        except (ValueError, OSError):
            return None

    # ---------- Icons & formatting ----------
    def _file_icon(self, name: str, is_dir: bool) -> str:
        if is_dir:
            return "📁"
        ext = os.path.splitext(name)[1].lower()
        icons = {
            ".jpg": "🖼", ".jpeg": "🖼", ".png": "🖼", ".gif": "🖼", ".webp": "🖼",
            ".mp4": "🎬", ".webm": "🎬", ".mov": "🎬", ".avi": "🎬",
            ".mp3": "🎵", ".wav": "🎵", ".ogg": "🎵", ".flac": "🎵",
            ".pdf": "📄", ".doc": "📄", ".docx": "📄",
            ".zip": "📦", ".tar": "📦", ".gz": "📦", ".7z": "📦",
            ".py": "🐍", ".pyw": "🐍", ".js": "📜", ".ts": "📜", ".html": "🌐", ".css": "🎨",
            ".json": "📋", ".yaml": "📋", ".yml": "📋", ".toml": "📋",
            ".txt": "📝", ".md": "📝", ".log": "📝",
            ".sh": "⚙", ".bash": "⚙", ".zsh": "⚙", ".bsh": "⚙", ".pl": "⚙", ".rb": "⚙", ".php": "⚙", ".awk": "⚙", ".sed": "⚙", ".sql": "🗄",
            ".bat": "🖥", ".cmd": "🖥", ".bin": "⚙", ".exe": "⚙", ".ini": "⚙", ".conf": "⚙", ".cfg": "⚙",
        }
        return icons.get(ext, "📄")

    def _fmt_size(self, size: int) -> str:
        for unit in ["B", "K", "M", "G"]:
            if size < 1024:
                return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
            size /= 1024
        return f"{size:.1f}T"

    def _fmt_time(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    # ---------- Directory listing ----------
    def _list_dir(self, path: Path, page: int = 1):
        # 用 os.scandir 一次拿到 is_dir/stat 元数据(避免每条两次 stat),并跳过失效链接/竞态删除
        entries = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    st = e.stat()
                except OSError:
                    continue
                entries.append((e.name, is_dir, st))
        entries.sort(key=lambda t: (not t[1], t[0].lower()))
        total = len(entries)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * PAGE_SIZE
        page_entries = entries[start:start + PAGE_SIZE]

        rel = path.relative_to(RESOURCE_DIR)
        parts = list(rel.parts) if str(rel) != "." else []
        crumbs = '<a href="/">🏠 ~/resource</a>'
        for i, p in enumerate(parts):
            href = "/" + "/".join(parts[:i + 1]) + "/"
            crumbs += ' / <a href="{}">{}</a>'.format(
                urllib.parse.quote(href, safe="/"), html.escape(p))
        rows = []
        for name, is_dir, stat in page_entries:
            href = "/" + "/".join(parts + [name]) if parts else "/" + name
            if is_dir:
                href += "/"
            href = urllib.parse.quote(href, safe="/")
            rel_item = str(rel / name)
            checkbox = f'<input type="checkbox" class="file-checkbox" value="{html.escape(rel_item)}">'
            actions = self._row_actions(rel, name, is_dir)
            rows.append("""<tr>
                <td class="td checkbox-col">{checkbox}</td>
                <td class="td icon">{icon}</td>
                <td class="td name"><a href="{href}">{name}</a></td>
                <td class="td size">{size}</td>
                <td class="td time">{time}</td>
                <td class="td actions">{actions}</td>
            </tr>""".format(
                checkbox=checkbox,
                icon=self._file_icon(name, is_dir),
                href=html.escape(href),
                name=html.escape(name),
                size="" if is_dir else self._fmt_size(stat.st_size),
                time=self._fmt_time(stat.st_mtime),
                actions=actions
            ))
        if not rows:
            rows.append('<tr><td colspan="6">📂 目录为空</td></tr>')
        dir_url = "/" + "/".join(parts) + "/" if parts else "/"
        dir_url = urllib.parse.quote(dir_url, safe="/")
        pager = self._pager_html(page, total_pages, total, dir_url)
        return rel, parts, crumbs, "".join(rows), pager

    def _pager_html(self, page: int, total_pages: int, total: int, dir_url: str) -> str:
        """生成分页导航;无需分页时返回空串。dir_url 为已 URL 编码的目录路径(如 /sub/ 或 /)。"""
        if total_pages <= 1:
            return ""
        parts = ['<nav class="pager">',
                 '<span class="pager-info">第 {}/{} 页 · 共 {} 项</span>'.format(page, total_pages, total)]
        if page > 1:
            parts.append('<a href="{}?page={}">« 上一页</a>'.format(dir_url, page - 1))
        else:
            parts.append('<span class="pager-disabled">« 上一页</span>')
        if page < total_pages:
            parts.append('<a href="{}?page={}">下一页 »</a>'.format(dir_url, page + 1))
        else:
            parts.append('<span class="pager-disabled">下一页 »</span>')
        parts.append('</nav>')
        return "".join(parts)
    def _row_actions(self, rel_dir: Path, name: str, is_dir: bool) -> str:
        rel_str = str(rel_dir)
        if rel_str == ".":
            rel_str = ""
        else:
            rel_str = rel_str + "/"
        # actions: 只有文本文件给编辑按钮(重命名/删除已收录在下方操作栏,行内不重复)
        edit_btn = ""
        if not is_dir:
            ext = os.path.splitext(name)[1].lower()
            if ext in EDITABLE_EXTS:
                edit_url = "/edit?path=" + urllib.parse.quote(rel_str + name, safe="/")
                edit_btn = '<a href="{}" class="btn btn-sm btn-edit-new">编辑</a>'.format(html.escape(edit_url))
        return edit_btn

    # ---------- HTTP methods ----------
    def do_GET(self):
        # 注销:GET /logout(清除会话并跳登录页)
        if self.path.rstrip("?/") == "/logout" or self.path.startswith("/logout?"):
            return self._logout()
        if not self._check_auth():
            # 未登录 → 显示登录页（而非浏览器弹窗）
            return self._send_login()
        rel_path = urllib.parse.unquote(self.path.split("?")[0]).lstrip("/")
        # 编辑请求: /edit?path=xxx —— 优先处理, 不依赖 rel_path 是真实目录
        if self.path.startswith("/edit?"):
            query = urllib.parse.urlparse(self.path).query
            params = dict(urllib.parse.parse_qsl(query))
            file_rel = params.get("path", "")
            if not file_rel:
                self.send_error(400, "Missing path")
                return
            file_safe = self._safe_path(file_rel)
            if not file_safe or not file_safe.is_file():
                self.send_error(404, "File not found")
                return
            ext = os.path.splitext(file_safe.name)[1].lower()
            if ext not in EDITABLE_EXTS:
                self.send_error(400, "File type not editable")
                return
            try:
                content = file_safe.read_text(encoding="utf-8")
            except Exception:
                self.send_error(500, "Cannot read file")
                return
            html_page = self._build_editor_html(file_rel.lstrip("/"), file_safe.name, content)
            body_bytes = html_page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return
        if rel_path == "":
            rel_path = "."
        safe = self._safe_path(rel_path)
        if not safe or not safe.exists():
            self.send_error(404)
            return
        if safe.is_dir():
            try:
                page = int(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query)).get("page", "1"))
            except (TypeError, ValueError):
                page = 1
            rel, parts, crumbs, rows, pager = self._list_dir(safe, page)
            html_content = self._build_html(rel, parts, crumbs, rows, pager)
            body_bytes = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return
        else:
            self._send_file(safe)
            return

    def do_POST(self):
        # 登录/登出无需会话
        if self.path == "/login":
            return self._handle_login()
        if self.path == "/logout":
            return self._logout()
        if not self._check_auth():
            return self._send_json_error(401, "Unauthorized")
        if self.path == "/upload":
            self._handle_upload()
        elif self.path == "/mkdir":
            self._handle_mkdir()
        elif self.path == "/delete":
            self._handle_delete()
        elif self.path == "/rename":
            self._handle_rename()
        elif self.path == "/edit":
            self._handle_edit()
        elif self.path == "/download":
            self._handle_download()
        elif self.path == "/copy":
            self._handle_copy_or_move(copy_mode=True)
        elif self.path == "/move":
            self._handle_copy_or_move(copy_mode=False)
        else:
            self.send_error(404)

    # ---------- Handlers ----------
    def _parse_multipart(self):
        """Parse multipart/form-data, return dict of fields and list of files.
        Each file is (filename, fileobj) where fileobj is a BytesIO of the content.
        Simple implementation for our use case.
        """
        ctype = self.headers.get('Content-Type', '')
        if not ctype.startswith('multipart/form-data'):
            return {}, []
        boundary = ctype.split("boundary=")[-1].encode()
        if not boundary:
            return {}, []
        length = int(self.headers.get('Content-Length', 0))
        data = self.rfile.read(length)
        # Split by boundary
        parts = data.split(b"--" + boundary)
        fields = {}
        files = []
        for part in parts:
            if part.strip() in (b"", b"--"):
                continue
            if b"\r\n\r\n" not in part:
                continue
            head, body = part.split(b"\r\n\r\n", 1)
            if body.endswith(b"\r\n"):
                body = body[:-2]
            # Parse headers
            head_dict = {}
            for line in head.decode().split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    head_dict[k.strip().lower()] = v.strip()
            # Content-Disposition
            disp = head_dict.get('content-disposition', '')
            name = None
            filename = None
            for part in disp.split(";"):
                part = part.strip()
                if part.startswith("name="):
                    name = part[5:].strip(' "\'')
                elif part.startswith("filename="):
                    filename = part[10:].strip(' "\'')
            if name is None:
                continue
            if filename:
                # file upload
                files.append((name, filename, body))
            else:
                # regular field
                fields[name] = body.decode()
        return fields, files

    def _handle_upload(self):
        fields, files = self._parse_multipart()
        # 是否允许覆盖重名文件(前端确认后,再带 overwrite=1 重新上传冲突项)
        overwrite = str(fields.get('overwrite', '')).lower() in ('1', 'true', 'yes')
        # Get target directory from field 'dir' (relative to RESOURCE_DIR)
        target_rel = fields.get('dir', "")
        target_dir = self._safe_path(target_rel)
        if not target_dir or not target_dir.is_dir():
            # If not exists, try to create it (but only if it's a subpath of RESOURCE_DIR)
            # We'll attempt to create directories recursively.
            try:
                target_dir = self._safe_path(target_rel)
                if target_dir:
                    target_dir.mkdir(parents=True, exist_ok=True)
                else:
                    self._send_json_error(400, "Invalid directory")
                    return
            except Exception:
                self._send_json_error(400, "Invalid directory")
                return
        saved = 0
        conflicts = []
        for _name, filename, body in files:
            if not filename:
                continue
            # sanitize
            safe_name = os.path.basename(filename)
            safe_path = target_dir / safe_name
            # Ensure it's still within target_dir (should be)
            try:
                safe_path.resolve().relative_to(target_dir.resolve())
            except Exception:
                continue
            if safe_path.exists() and not overwrite:
                # 重名且未确认覆盖 → 报冲突,不写入
                conflicts.append(safe_name)
                continue
            try:
                with open(safe_path, 'wb') as f:
                    f.write(body)
                saved += 1
            except Exception:
                pass
        if saved == 0 and not conflicts:
            self._send_json_error(400, "No valid files")
            return
        resp = {"msg": "Uploaded {} file(s)".format(saved)}
        if conflicts:
            resp["conflict"] = conflicts
        self._send_json(resp)

    def _handle_mkdir(self):
        # Accept both JSON and form-urlencoded body
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        ctype = self.headers.get('Content-Type', '')
        fields = {}
        if 'application/json' in ctype:
            try:
                fields = json.loads(body)
            except Exception:
                self._send_json_error(400, "Invalid JSON")
                return
        else:
            for pair in body.split('&'):
                if '=' in pair:
                    k, v = pair.split('=', 1)
                    fields[k] = urllib.parse.unquote_plus(v)
        parent_rel = fields.get('dir', "") or ""
        name = fields.get('name', "") or ""
        if not name:
            self._send_json_error(400, "Missing folder name")
            return
        parent_dir = self._safe_path(parent_rel) if parent_rel else Path(RESOURCE_DIR)
        if not parent_dir or not parent_dir.is_dir():
            self._send_json_error(400, "Invalid parent directory")
            return
        safe_name = os.path.basename(name)
        if safe_name != name or '/' in safe_name or '\\' in safe_name:
            self._send_json_error(400, "Invalid folder name")
            return
        new_dir = parent_dir / safe_name
        try:
            new_dir.mkdir(exist_ok=False)
            self._send_json({"msg": "Folder created"})
        except FileExistsError:
            self._send_json_error(400, "Folder already exists")
        except Exception as e:
            self._send_json_error(500, str(e))

    def _handle_delete(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        try:
            data = json.loads(body)
            paths = data.get('paths', [])
        except Exception:
            self._send_json_error(400, "Invalid JSON")
            return
        deleted = 0
        root = Path(RESOURCE_DIR).resolve()
        for rel in paths:
            safe = self._safe_path(rel)
            if not safe or safe.resolve() == root:
                continue
            try:
                if safe.is_dir():
                    shutil.rmtree(safe)
                else:
                    safe.unlink()
                deleted += 1
            except Exception:
                pass
        if deleted:
            self._send_json({"msg": "Deleted {} item(s)".format(deleted)})
        else:
            self._send_json_error(400, "Nothing deleted")

    def _handle_rename(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        try:
            data = json.loads(body)
            src = data.get('src', "")
            dst = data.get('dst', "")
            overwrite = bool(data.get('overwrite', False))
        except Exception:
            self._send_json_error(400, "Invalid JSON")
            return
        if not src or not dst:
            self._send_json_error(400, "Missing src or dst")
            return
        src_safe = self._safe_path(src)
        dst_safe = self._safe_path(dst)
        if not src_safe or not dst_safe:
            self._send_json_error(400, "Invalid path")
            return
        root = Path(RESOURCE_DIR).resolve()
        if src_safe.resolve() == root or dst_safe.resolve() == root:
            self._send_json_error(400, "Cannot rename the root directory")
            return
        # 重名且未确认覆盖 → 报告冲突,由前端提示"覆盖/取消"
        if dst_safe.exists() and not overwrite:
            self._send_json({"conflict": True, "src": src, "dst": dst})
            return
        # 确认覆盖:先删除目标再重命名
        if overwrite and dst_safe.exists():
            try:
                if dst_safe.is_dir():
                    shutil.rmtree(dst_safe)
                else:
                    dst_safe.unlink()
            except Exception as e:
                self._send_json_error(500, str(e))
                return
        # Ensure dst parent exists
        dst_parent = dst_safe.parent
        try:
            dst_parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            src_safe.rename(dst_safe)
            self._send_json({"msg": "Renamed"})
        except FileExistsError:
            self._send_json({"conflict": True, "src": src, "dst": dst})
        except Exception as e:
            self._send_json_error(500, str(e))

    def _handle_edit(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        try:
            data = json.loads(body)
            path = data.get('path', "")
            content = data.get('content', "")
        except Exception:
            self._send_json_error(400, "Invalid JSON")
            return
        if not path:
            self._send_json_error(400, "Missing path")
            return
        file_safe = self._safe_path(path)
        if not file_safe:
            self._send_json_error(400, "Invalid path")
            return
        # 新建文件：文件不存在时创建（父目录需存在）
        if not file_safe.is_file():
            parent_dir = file_safe.parent
            if not parent_dir.is_dir():
                self._send_json_error(400, "Parent directory does not exist")
                return
            ext = os.path.splitext(file_safe.name)[1].lower()
            if ext not in EDITABLE_EXTS:
                self._send_json_error(400, "File type not supported")
                return
            try:
                file_safe.touch()
                file_safe.write_text(content, encoding="utf-8")
                self._send_json({"msg": "Created"})
                return
            except Exception as e:
                self._send_json_error(500, str(e))
                return
        # 编辑已有文件
        ext = os.path.splitext(file_safe.name)[1].lower()
        if ext not in EDITABLE_EXTS:
            self._send_json_error(400, "File type not editable")
            return
        try:
            file_safe.write_text(content, encoding="utf-8")
            self._send_json({"msg": "Saved"})
        except Exception as e:
            self._send_json_error(500, str(e))


    def _handle_copy_or_move(self, copy_mode: bool):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        try:
            data = json.loads(body)
            paths = data.get('paths', [])
            dest = data.get('dest', "") or ""
        except Exception:
            self._send_json_error(400, "Invalid JSON")
            return
        if not paths:
            self._send_json_error(400, "No paths given")
            return
        # 目标必须是目录；空表示根目录
        dest_dir = self._safe_path(dest) if dest else Path(RESOURCE_DIR)
        if not dest_dir or not dest_dir.is_dir():
            self._send_json_error(400, "Invalid destination directory")
            return
        done = 0
        errors = []
        root = Path(RESOURCE_DIR).resolve()
        dest_dir_res = dest_dir.resolve()
        for rel in paths:
            src = self._safe_path(rel)
            if not src or not src.exists():
                errors.append(rel + ": 不存在")
                continue
            src_res = src.resolve()
            if src_res == root:
                errors.append(rel + ": 不能移动/复制根目录")
                continue
            target = dest_dir / src.name
            if target.resolve() == src_res:
                errors.append(rel + ": 源和目标相同")
                continue
            # 防止把目录移动到它自己内部(或复制时无限递归)
            if src.is_dir() and (src_res == dest_dir_res or src_res in dest_dir_res.parents):
                errors.append(rel + ": 不能将其放入自身内部")
                continue
            try:
                if copy_mode:
                    if src.is_dir():
                        # 复制目录前确保目标不存在
                        if target.exists():
                            errors.append(rel + ": 目标已存在")
                            continue
                        shutil.copytree(src, target)
                    else:
                        if target.exists():
                            errors.append(rel + ": 目标已存在")
                            continue
                        shutil.copy2(src, target)
                else:
                    # 移动：把源移动到目标内
                    if target.exists():
                        errors.append(rel + ": 目标已存在")
                        continue
                    shutil.move(str(src), str(target))
                done += 1
            except Exception as e:
                errors.append(rel + ": " + str(e))
        verb = "Copied" if copy_mode else "Moved"
        msg = "{} {} item(s)".format(verb, done)
        if errors:
            msg += "; errors: " + "; ".join(errors)
        self._send_json({"msg": msg})

    def _handle_download(self):
        """下载选中项:单个文件→直接流式下载;多个或含目录→zip 递归打包下载。"""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode()
        try:
            data = json.loads(body)
            paths = data.get('paths', [])
        except Exception:
            self._send_json_error(400, "Invalid JSON")
            return
        if not paths or not isinstance(paths, list):
            self._send_json_error(400, "No paths given")
            return
        valid = []
        for rel in paths:
            p = self._safe_path(rel)
            if not p or not p.exists():
                continue
            valid.append(p)
        if not valid:
            self._send_json_error(400, "No valid paths")
            return
        # 单个文件:保持"单独下载"体验
        if len(valid) == 1 and valid[0].is_file():
            self._send_attachment(valid[0], valid[0].name)
            return
        # 含多个或目录 → 打包 zip(内存中生成,直接以附件形式返回)
        try:
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in valid:
                    base = p.name
                    if p.is_dir():
                        for child in p.rglob("*"):
                            if child.is_file():
                                zf.write(child, str(Path(base) / child.relative_to(p)))
                    else:
                        zf.write(p, base)
        except Exception as e:
            self._send_json_error(500, "Zip failed: " + str(e))
            return
        data = bio.getvalue()
        fname = "download-{}.zip".format(time.strftime("%Y%m%d%H%M%S"))
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="{}"'.format(fname))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_attachment(self, path: Path, name: str):
        """以附件形式流式下载单个文件,处理非 ASCII 文件名。"""
        encoded = urllib.parse.quote(name, safe="")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"download\"; filename*=UTF-8''{}".format(encoded))
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                self.wfile.write(chunk)

    # ---------- Utilities ----------
    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json_error(self, status, msg):
        self._send_json({"error": msg}, status)

    def _send_file(self, path: Path):
        mime = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        # 给文本类 MIME 自动补 charset=utf-8，防止中文乱码
        if mime.startswith('text/') or mime in {
            'application/json', 'application/xml', 'application/javascript',
            'application/x-javascript', 'application/typescript',
            'application/x-httpd-php', 'application/xhtml+xml',
        }:
            mime = f'{mime}; charset=utf-8'
        ext = os.path.splitext(path.name)[1].lower()
        # 可编辑文本文件不缓存(避免编辑保存后浏览器仍显示旧内容),静态资源保留1小时缓存
        if ext in EDITABLE_EXTS:
            cache_ctl = 'no-cache, no-store, must-revalidate'
        else:
            cache_ctl = 'max-age=3600'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('Cache-Control', cache_ctl)
        self.end_headers()
        with open(path, 'rb') as f:
            while chunk := f.read(CHUNK_SIZE):
                self.wfile.write(chunk)

    def _build_editor_html(self, file_path: str, file_name: str, content: str) -> str:
        """渲染网页文本编辑器页面(新建/编辑均用)。file_path 为文件根目录相对路径。"""
        esc_path = html.escape(file_path, quote=True)
        esc_name = html.escape(file_name, quote=True)
        esc_content = html.escape(content)
        back_url = ""
        dir_part = file_path.rsplit("/", 1)[0]
        if dir_part:
            back_url = "/" + html.escape(dir_part, quote=True)
        else:
            back_url = "/"
        template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>aicbbuu-web-file-browser - 编辑 - {esc_name}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #0a0a0a; margin: 0; color: #f5f5f5; }}
  .bar {{ display:flex; align-items:center; gap:12px; padding:12px 20px;
         background:#161616; border-bottom:1px solid #2a2a2a; position:sticky; top:0; flex-wrap:wrap; }}
  .bar .file {{ color:#ffffff; font-weight:600; font-size:0.95rem; overflow:hidden; text-overflow:ellipsis; }}
  .btn {{ padding:8px 16px; border-radius:12px; border:none; cursor:pointer; font-size:0.9rem; font-weight:600; }}
  .btn-save {{ background:#e63946; color:#fff; }}
  .btn-back {{ background:#2a2a2a; color:#f5f5f5; border:1px solid #3d3d3d; text-decoration:none; }}
  .btn-save:active, .btn-back:active {{ opacity:.8; }}
  .wrap {{ padding:16px 20px; }}
  textarea {{ width:100%; min-height:60vh; font-family:ui-monospace,monospace; font-size:1rem;
              border:1px solid #2e2e2e; border-radius:12px; padding:12px; line-height:1.5; resize:vertical;
              background:#0a0a0a; color:#f5f5f5; }}
  textarea:focus {{ outline:none; border-color:#e63946; box-shadow:0 0 0 3px rgba(230,57,70,0.15); }}
  .err {{ background:#1f1f1f; color:#e63946; padding:8px 16px; border-radius:12px; display:none; border:1px solid #e6394655; }}
  #status {{ font-size:0.85rem; color:#9e9e9e; }}
</style>
</head>
<body>
<div class="bar">
  <button class="btn btn-save" id="btnSave">💾 保存</button>
  <a class="btn btn-back" href="{back_url}">← 返回</a>
  <span class="file">{esc_path}</span>
  <span id="status"></span>
</div>
<div class="wrap">
  <div class="err" id="err"></div>
  <textarea id="editor" spellcheck="false" placeholder="在此输入内容...">{esc_content}</textarea>
</div>
<script>
  const FILE_PATH = {path_value};
  const saveBtn = document.getElementById('btnSave');
  const editor = document.getElementById('editor');
  const status = document.getElementById('status');
  const errBox = document.getElementById('err');
  const dirty = {{ current:false }};
  editor.addEventListener('input', () => {{ dirty.current = true; status.textContent = '● 未保存'; }});
  async function save() {{
    saveBtn.disabled = true; errBox.style.display = 'none';
    status.textContent = '保存中...';
    try {{
      const r = await fetch('/edit', {{ method:'POST',
        headers:{{ 'Content-Type':'application/json;charset=UTF-8' }},
        body: JSON.stringify({{ path: FILE_PATH, content: editor.value }}) }});
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      dirty.current = false;
      status.textContent = '✔ 已保存 ' + new Date().toLocaleTimeString();
    }} catch (e) {{
      errBox.style.display = 'block'; errBox.textContent = '保存失败: ' + e.message;
      status.textContent = '✖ 未保存';
    }} finally {{
      saveBtn.disabled = false;
    }}
  }}
  saveBtn.addEventListener('click', save);
  document.addEventListener('keydown', e => {{
    if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {{ e.preventDefault(); save(); }}
  }});
  const beforeUnload = e => {{ if (dirty.current) {{ e.preventDefault(); e.returnValue = ''; }} }};
  window.addEventListener('beforeunload', beforeUnload);
</script>
</body>
</html>"""
        return template.format(
            esc_name=esc_name, esc_path=esc_path, back_url=back_url,
            esc_content=esc_content,
            path_value=json.dumps(file_path, ensure_ascii=False),
        )

    def _build_html(self, rel_dir: Path, parts: list, crumbs: str, rows: str, pager: str = "") -> str:
        current_dir = '' if str(rel_dir) == '.' else str(rel_dir) + '/'
        template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>aicbbuu-web-file-browser - __TITLE__</title>
<style>
html{font-size:90%}
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Roboto,sans-serif;min-height:100vh}
.container{max-width:1500px;margin:0 auto;padding:34px 40px}
h1{font-size:1.8rem;margin-bottom:28px;color:#ffffff;font-weight:700;letter-spacing:0.01em}
.page-title{display:flex;align-items:center}
.logout-btn{font-size:1rem;font-weight:600;color:#fff;background:#e63946;padding:10px 20px;border-radius:12px;text-decoration:none;display:inline-block;margin-left:auto;border:none;cursor:pointer;transition:background 0.2s}
.logout-btn:hover{background:#d62839}
.breadcrumbs{margin:16px 0 22px;color:#c9c9d1;font-size:0.95rem;font-weight:500}
.breadcrumbs a{color:#e63946;text-decoration:none;font-weight:500}
.breadcrumbs a:hover{text-decoration:underline}
.table{width:100%;border-collapse:collapse;background:#161616;border:1px solid #2a2a2a;border-radius:16px;overflow:hidden;box-shadow:0 20px 50px rgba(0,0,0,0.5)}
.tr{transition:background 0.15s}
.tr:hover{background:#222222}
.td{border-bottom:1px solid #2b2b2b;vertical-align:middle;font-size:1.05rem;padding:16px 24px}
.th{background:#1b1b1b;color:#f5f5f5;font-weight:600;text-align:left;font-size:0.95rem;letter-spacing:0.03em;padding:16px 24px;border-bottom:1px solid #2b2b2b}
tbody .tr:last-child .td{border-bottom:none}
.icon{width:60px;text-align:center;font-size:1.4rem;color:#e0e0e0}
.name{word-break:break-all;font-size:1.05rem;font-weight:500;padding-left:14px}
.name a{color:#f5f5f5;text-decoration:none;transition:color 0.15s}
.name a:hover{color:#e63946}
.time{color:#9e9e9e;font-size:0.92rem;white-space:nowrap}
.size{color:#9e9e9e;font-size:0.92rem;text-align:left;white-space:nowrap}
.actions{white-space:nowrap;text-align:center}
.actions .btn-edit-new{display:inline-block;padding:8px 16px;font-size:1rem;border-radius:20px;border:none;cursor:pointer;text-decoration:none;font-weight:500;color:#fff;background:#e63946}
.actions .btn-edit-new:hover{background:#d62839}
.checkbox-col{width:52px;text-align:center}
.checkbox-col input{width:20px;height:20px;cursor:pointer;accent-color:#e63946}
.toolbar{margin-top:26px;padding:22px 26px;background:#161616;border:1px solid #2a2a2a;border-radius:16px;box-shadow:0 20px 50px rgba(0,0,0,0.5);display:flex;flex-wrap:wrap;gap:16px;align-items:center}
.toolbar-actions{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.toolbar-actions button{padding:13px 26px;font-size:1.05rem;border-radius:12px;border:none;cursor:pointer;font-weight:500;transition:transform 0.1s,background 0.2s;color:#fff}
.toolbar-actions button:active{transform:translateY(1px)}
.toolbar-actions button.primary{background:#e63946}
.toolbar-actions button.primary:hover{background:#d62839}
.toolbar-actions button.danger{background:#ad1f2e}
.toolbar-actions button.danger:hover{background:#8f1a27}
.toolbar-actions button.secondary{background:#2a2a2a;border:1px solid #3d3d3d;color:#f5f5f5}
.toolbar-actions button.secondary:hover{background:#383838}
.toolbar-actions button:disabled{opacity:0.4;cursor:not-allowed;transform:none}
.clipboard-hint{margin-top:14px;width:100%;padding:12px 16px;background:#1f1f1f;border:1px solid #3d3d3d;border-radius:12px;color:#e63946;font-size:0.92rem;display:none;font-weight:500}
.clipboard-hint.show{display:block}
.progress{width:100%;background:#2a2a2a;border-radius:8px;overflow:hidden;height:8px;margin-top:18px;display:none}
.progress-bar{height:100%;background:#e63946;width:0%;transition:width 0.2s}
.modal{display:none;position:fixed;z-index:1000;left:0;top:0;width:100%;height:100%;overflow:auto;background:rgba(0,0,0,0.7);backdrop-filter:blur(3px)}
.modal-content{background:#161616;margin:10% auto;padding:28px;border-radius:20px;width:92%;max-width:540px;box-shadow:0 16px 50px rgba(0,0,0,0.6);border:1px solid #2a2a2a}
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;border-bottom:1px solid #2b2b2b;padding-bottom:14px}
.modal-title{font-size:1.2rem;color:#ffffff;font-weight:600}
.close{color:#9e9e9e;font-size:1.5rem;font-weight:bold;cursor:pointer;line-height:1;transition:color 0.2s}
.close:hover{color:#ffffff}
.modal-body{margin:18px 0}
.modal-body label{display:block;margin-bottom:10px;font-weight:600;color:#cfcfcf;font-size:1rem}
.modal-body input,.modal-body textarea{width:100%;padding:12px 14px;border:1px solid #2e2e2e;border-radius:12px;background:#0a0a0a;color:#f5f5f5;font-size:1.05rem}
.modal-body input:focus,.modal-body textarea:focus{outline:none;border-color:#e63946;box-shadow:0 0 0 3px rgba(230,57,70,0.15)}
.modal-body textarea{height:280px;resize:vertical;font-family:monospace;font-size:1rem;line-height:1.5}
.modal-footer{text-align:right;margin-top:24px;padding-top:16px;border-top:1px solid #2b2b2b}
.modal-footer button{padding:11px 24px;margin-left:10px;border-radius:12px;border:none;cursor:pointer;font-size:1rem;font-weight:500;color:#fff}
.modal-footer button.primary{background:#e63946}
.modal-footer button.primary:hover{background:#d62839}
.modal-footer button.danger{background:#ad1f2e}
.modal-footer button.danger:hover{background:#9f1a27}
.modal-footer button.secondary{background:#2a2a2a;color:#f5f5f5}
.modal-footer button.secondary:hover{background:#383838}
.toast{position:fixed;bottom:30px;right:30px;background:#e63946;color:#fff;padding:16px 28px;border-radius:14px;opacity:0;transform:translateY(20px);transition:all 0.3s;z-index:2000;box-shadow:0 8px 30px rgba(0,0,0,0.3);font-size:1.05rem;font-weight:600}
.toast.show{opacity:1;transform:translateY(0)}
.toast.error{background:#ad1f2e}
.toast.warning{background:#8f1a27}
.pager{display:flex;align-items:center;gap:16px;margin-top:20px;flex-wrap:wrap}
.pager a{color:#e63946;text-decoration:none;font-weight:500;padding:6px 14px;border:1px solid #3d3d3d;border-radius:12px;background:#161616}
.pager a:hover{background:#222222}
.pager .pager-disabled{color:#5a5a5a;padding:6px 14px;border:1px solid #2a2a2a;border-radius:12px}
.pager .pager-info{color:#9e9e9e;font-size:0.9rem}
</style>
</head>
<body>
<div class="container">
<h1 class="page-title">aicbbuu-web-file-browser <a href="/logout" class="logout-btn" title="注销登录">⏻ 注销登录</a></h1>

<div class="breadcrumbs">__Crumbs__</div>

<table class="table">
<thead>
<tr>
<th class="th checkbox-col"><input type="checkbox" id="selectAllCheckbox"></th>
<th class="th">图标</th>
<th class="th">名称</th>
<th class="th">大小</th>
<th class="th">修改时间</th>
<th class="th">操作</th>
</tr>
</thead>
<tbody>
__Rows__
</tbody>
</table>
__Pager__
<div class="progress" id="progressBar"><div class="progress-bar" id="progressBarInner"></div></div>
<div class="toolbar">
<div class="toolbar-actions">
<button class="primary" id="btnNewFile">+ 新建文件</button>
<button class="primary" id="btnNewFolder">+ 新建文件夹</button>
<button class="primary" id="btnUpload">⬆ 上传文件</button>
<input type="file" id="fileInput" multiple style="display:none" />
<button class="secondary" id="btnCutSelected" disabled>✂ 剪切</button>
<button class="secondary" id="btnCopySelected" disabled>📋 复制</button>
<button class="secondary" id="btnPaste" disabled>📥 粘贴</button>
<button class="secondary" id="btnDownloadSelected" disabled>⬇ 下载</button>
<button class="secondary" id="btnRenameSelected" disabled>✏ 重命名</button>
<button class="danger" id="btnDeleteSelected" disabled>🗑 删除选中</button>
</div>
<div class="clipboard-hint" id="clipboardHint"></div>
</div>

</div>

<!-- 重命名 Modal -->
<div id="renameModal" class="modal">
<div class="modal-content">
<div class="modal-header">
<span class="modal-title" id="renameModalTitle">重命名</span>
<span class="close">&times;</span>
</div>
<div class="modal-body">
<label>新名称：</label>
<input type="text" id="renameInput" placeholder="输入新名称（含扩展名）" />
</div>
<div class="modal-footer">
<button class="secondary" id="btnRenameCancel">取消</button>
<button class="primary" id="btnRenameConfirm">确认重命名</button>
</div>
</div>
</div>

<!-- 删除确认 Modal -->
<div id="deleteConfirmModal" class="modal">
<div class="modal-content">
<div class="modal-header">
<span class="modal-title" id="confirmModalTitle">确认删除</span>
<span class="close">&times;</span>
</div>
<div class="modal-body">
<p id="deleteConfirmMsg" style="color:#c9d1d9;line-height:1.6"></p>
</div>
<div class="modal-footer">
<button class="secondary" id="btnDeleteCancel">取消</button>
<button type="button" class="danger" id="btnDeleteConfirm">确定删除</button>
</div>
</div>
</div>

<!-- 新建输入 Modal -->
<div id="newNameModal" class="modal">
<div class="modal-content">
<div class="modal-header">
<span class="modal-title" id="newNameModalTitle">新建</span>
<span class="close">&times;</span>
</div>
<div class="modal-body">
<label id="newNameLabel">请输入名称：</label>
<input type="text" id="newNameInput" placeholder="输入名称" />
</div>
<div class="modal-footer">
<button class="secondary" id="btnNewNameCancel">取消</button>
<button type="button" class="primary" id="btnNewNameConfirm">确定</button>
</div>
</div>
</div>

<script>
// Utility
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const toast = (msg, type='success') => {
    const t = document.createElement('div');
    t.className = 'toast ' + (type==='error'?'error':type==='warning'?'warning':'');
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 300);
    }, 3000);
};
const api = (url, method, data, cb) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    if (data && !(data instanceof FormData)) {
        xhr.setRequestHeader('Content-Type', 'application/json;charset=UTF-8');
    }
    xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
            let resp;
            try { resp = JSON.parse(xhr.responseText); } catch { resp = xhr.responseText; }
            cb(null, resp);
        } else {
            let err = xhr.statusText;
            try { err = JSON.parse(xhr.responseText).error; } catch {}
            cb(err || '请求失败', null);
        }
    };
    xhr.onerror = () => cb('网络错误', null);
    if (data instanceof FormData) xhr.send(data);
    else xhr.send(data ? JSON.stringify(data) : null);
};

// 当前目录
const pathName = window.location.pathname;
if (pathName === '/' || pathName === '') {
    window.currentDir = '';
} else {
    window.currentDir = decodeURIComponent(pathName.replace(/^\\/|\\/$/g, ''));
}

// 选中状态管理
function updateSelectionUI() {
    const checkboxes = $$('.file-checkbox');
    const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
    const allChecked = checkboxes.length > 0 && checkedCount === checkboxes.length;
    $('#selectAllCheckbox').checked = allChecked;
    $('#selectAllCheckbox').indeterminate = checkedCount > 0 && !allChecked;
    $('#btnDeleteSelected').disabled = checkedCount === 0;
    $('#btnRenameSelected').disabled = checkedCount === 0;
    $('#btnCopySelected').disabled = checkedCount === 0;
    $('#btnCutSelected').disabled = checkedCount === 0;
    $('#btnDownloadSelected').disabled = checkedCount === 0;
    const hasClipboard = window.clipboardPaths && window.clipboardPaths.length > 0;
    $('#btnPaste').disabled = !hasClipboard;
}

// ---- 剪贴板 (复制 / 剪切 / 粘贴) ----
window.clipboardPaths = [];
window.clipboardIsCut = false;

// 用 sessionStorage 持久化剪贴板，跨页面导航保留
function loadClipboard() {
    try {
        const raw = sessionStorage.getItem('clipboardData');
        if (!raw) {
            window.clipboardPaths = [];
            window.clipboardIsCut = false;
        } else {
            const obj = JSON.parse(raw);
            window.clipboardPaths = obj.paths || [];
            window.clipboardIsCut = !!obj.cut;
        }
    } catch (e) {
        window.clipboardPaths = [];
        window.clipboardIsCut = false;
    }
}
function saveClipboard() {
    try {
        sessionStorage.setItem('clipboardData', JSON.stringify({
            paths: window.clipboardPaths,
            cut: window.clipboardIsCut
        }));
    } catch (e) {}
}
loadClipboard();

function renderClipboardHint() {
    saveClipboard();
    const hint = $('#clipboardHint');
    if (window.clipboardPaths.length) {
        const mode = window.clipboardIsCut ? '剪切' : '复制';
        hint.textContent = `📋 待${mode} ${window.clipboardPaths.length} 项 → 点击「粘贴」放入当前目录`;
        hint.classList.add('show');
    } else {
        hint.classList.remove('show');
    }
    updateSelectionUI();
}
renderClipboardHint();

$('#btnCopySelected').onclick = () => {
    const checkboxes = $$('.file-checkbox');
    window.clipboardPaths = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
    window.clipboardIsCut = false;
    renderClipboardHint();
    toast(`已复制 ${window.clipboardPaths.length} 项，请到目标文件夹粘贴`);
};

$('#btnCutSelected').onclick = () => {
    const checkboxes = $$('.file-checkbox');
    window.clipboardPaths = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
    window.clipboardIsCut = true;
    renderClipboardHint();
    toast(`已剪切 ${window.clipboardPaths.length} 项，请到目标文件夹粘贴`);
};

$('#btnPaste').onclick = () => {
    if (!window.clipboardPaths.length) return;
    const dest = window.currentDir || '';
    const url = window.clipboardIsCut ? '/move' : '/copy';
    const mode = window.clipboardIsCut ? '移动' : '复制';
    api(url, 'POST', {paths: window.clipboardPaths, dest: dest}, (err, res) => {
        if (err) return alert(mode + '失败: ' + err);
        toast(`${mode}成功: ${res.msg}`);
        if (window.clipboardIsCut) {
            window.clipboardPaths = [];
            window.clipboardIsCut = false;
            renderClipboardHint();
        }
        location.reload();
    });
};

// 全选 / 取消全选
$('#selectAllCheckbox').onchange = e => {
    $$('.file-checkbox').forEach(cb => cb.checked = e.target.checked);
    updateSelectionUI();
};

document.addEventListener('change', e => {
    if (e.target.matches('.file-checkbox')) updateSelectionUI();
});

// 新建文件
let newNameMode = 'file';
let pendingNewName = null;
$('#btnNewFile').onclick = () => {
    newNameMode = 'file';
    $('#newNameModalTitle').textContent = '新建文件';
    $('#newNameLabel').textContent = '请输入新文件名（含扩展名，如 note.txt）：';
    $('#newNameInput').value = '';
    $('#newNameInput').placeholder = '如 note.txt';
    $('#newNameModal').style.display = 'block';
    setTimeout(() => $('#newNameInput').focus(), 100);
};
$('#btnNewFolder').onclick = () => {
    newNameMode = 'folder';
    $('#newNameModalTitle').textContent = '新建文件夹';
    $('#newNameLabel').textContent = '请输入新文件夹名：';
    $('#newNameInput').value = '';
    $('#newNameInput').placeholder = '如 images';
    $('#newNameModal').style.display = 'block';
    setTimeout(() => $('#newNameInput').focus(), 100);
};
$('#btnNewNameCancel').onclick = () => { $('#newNameModal').style.display = 'none'; };
$('#newNameModal .close').onclick = () => { $('#newNameModal').style.display = 'none'; };
$('#newNameInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') $('#btnNewNameConfirm').click();
});
$('#btnNewNameConfirm').onclick = () => {
    const name = $('#newNameInput').value.trim();
    if (!name) return alert('请输入名称');
    $('#newNameModal').style.display = 'none';
    const dir = window.currentDir || '';
    // 修正：补回目录分隔符，避免 to-files/ 拼接成 to-files123.txt
    const fullPath = dir ? dir + '/' + name : name;
    if (newNameMode === 'file') {
        fetch('/edit', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json;charset=UTF-8'
            },
            body: JSON.stringify({path: fullPath, content: ''})
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) return alert('错误: ' + data.error);
            toast(data.msg || '文件创建成功');
            location.reload();
        })
        .catch(err => alert('网络错误: ' + err));
    } else {
        api('/mkdir', 'POST', {dir: dir, name: name}, (err, res) => {
            if (err) return alert('错误: ' + err);
            toast(res.msg);
            location.reload();
        });
    }
};

// 新建文件夹
// 上传文件：点击按钮选文件，选中后弹确认框再上传
let pendingUploadFiles = [];
$('#btnUpload').onclick = () => $('#fileInput').click();

$('#fileInput').onchange = e => {
    const files = e.target.files;
    if (!files.length) return;
    pendingUploadFiles = Array.from(files);  // 复制文件列表，避免清空input时被连带清空
    const fileNames = Array.from(files).map(f => f.name).join('<br>');
    $('#deleteConfirmMsg').innerHTML = `确定上传 <strong>${files.length}</strong> 个文件到当前目录？<br><br><code style="font-size:0.8rem;color:#8b949e">${fileNames}</code>`;
    $('#confirmModalTitle').textContent = '确认上传';
    $('#btnDeleteConfirm').textContent = '确定上传';
    $('#deleteConfirmModal').style.display = 'block';
    $('#deleteConfirmModal').dataset.mode = 'upload';
};

// HTML 转义(用于把文件名安全地插入 innerHTML,防注入)
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

$('#btnDeleteCancel').onclick = () => {
    const mode = $('#deleteConfirmModal').dataset.mode;
    $('#deleteConfirmModal').style.display = 'none';
    if (mode === 'conflict' && window._conflictOnSkip) window._conflictOnSkip();
};
$('#deleteConfirmModal .close').onclick = () => { $('#deleteConfirmModal').style.display = 'none'; };

// 通用"覆盖 / 跳过"提示(复用确认 modal,保持一致的暗黑风格)
function showConflictModal(title, msgHtml, onOverwrite, onSkip) {
    window._conflictOnOverwrite = onOverwrite;
    window._conflictOnSkip = onSkip;
    $('#confirmModalTitle').textContent = title;
    $('#deleteConfirmMsg').innerHTML = msgHtml;
    $('#btnDeleteConfirm').textContent = '覆盖';
    $('#btnDeleteCancel').textContent = '跳过';
    $('#deleteConfirmModal').dataset.mode = 'conflict';
    $('#deleteConfirmModal').style.display = 'block';
}

// 上传:overwrite=false 时命中重名返回 conflict 列表 → 前端弹"覆盖/跳过";确认后仅对冲突文件覆盖
function doUpload(files, overwrite) {
    const fileList = files || pendingUploadFiles;
    const form = new FormData();
    const dir = window.currentDir || '';
    form.append('dir', dir);
    for (let f of fileList) form.append('file', f);
    if (overwrite) form.append('overwrite', '1');
    const prog = $('#progressBar');
    const bar = $('.progress-bar');
    prog.style.display = 'block';
    bar.style.width = '0%';
    let p = 0;
    const interval = setInterval(() => {
        p += Math.random() * 15;
        if (p > 95) p = 95;
        bar.style.width = p + '%';
    }, 150);
    $('#fileInput').value = '';
    api('/upload', 'POST', form, (err, res) => {
        clearInterval(interval);
        prog.style.display = 'none';
        if (err) return alert('上传失败: ' + err);
        if (!overwrite && res.conflict && res.conflict.length) {
            const conflictNames = res.conflict;
            const conflictFiles = Array.from(pendingUploadFiles).filter(f => conflictNames.indexOf(f.name) !== -1);
            const preview = conflictNames.slice(0, 10).map(esc).join('<br>');
            const more = conflictNames.length > 10 ? `<br>... 等共 ${conflictNames.length} 个` : '';
            showConflictModal(
                '存在重名文件',
                `以下文件已存在，是否覆盖？<br><br><code style="font-size:0.8rem;color:#8b949e">${preview}${more}</code>`,
                () => doUpload(conflictFiles, true),
                () => { toast('已跳过重名文件'); location.reload(); }
            );
            return;
        }
        toast(res.msg || '上传成功');
        location.reload();
    });
}

$('#btnDeleteConfirm').onclick = () => {
    const mode = $('#deleteConfirmModal').dataset.mode;
    $('#deleteConfirmModal').style.display = 'none';
    if (mode === 'upload') {
        doUpload(null, false);
    } else if (mode === 'delete') {
        if (!window.pendingDeletePaths.length) return;
        api('/delete', 'POST', {paths: window.pendingDeletePaths}, (err, res) => {
            if (err) return alert('错误: ' + err);
            toast(res.msg);
            window.pendingDeletePaths = [];
            location.reload();
        });
    } else if (mode === 'conflict' && window._conflictOnOverwrite) {
        window._conflictOnOverwrite();
    }
};

// 删除选中
$('#btnDeleteSelected').onclick = () => {
    const checkboxes = $$('.file-checkbox');
    window.pendingDeletePaths = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
    if (!window.pendingDeletePaths.length) return;
    const count = window.pendingDeletePaths.length;
    const preview = window.pendingDeletePaths.slice(0, 5).join('<br>');
    const more = count > 5 ? `<br>... 等共 ${count} 项` : '';
    $('#deleteConfirmMsg').innerHTML = `确定删除选中的 <strong>${count}</strong> 项？<br><br><code style="font-size:0.8rem;color:#8b949e">${preview}${more}</code>`;
    $('#confirmModalTitle').textContent = '确认删除';
    $('#btnDeleteConfirm').textContent = '确定删除';
    $('#deleteConfirmModal').dataset.mode = 'delete';
    $('#deleteConfirmModal').style.display = 'block';
};

// 下载选中:单个文件→单文件下载;含目录/多个→后端打包 zip 下载
$('#btnDownloadSelected').onclick = () => {
    const checkboxes = $$('.file-checkbox');
    const paths = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
    if (!paths.length) return;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/download', true);
    xhr.responseType = 'blob';
    xhr.setRequestHeader('Content-Type', 'application/json;charset=UTF-8');
    xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
            let name = 'download';
            const dispo = xhr.getResponseHeader('Content-Disposition') || '';
            let m = dispo.match(/filename\\*=UTF-8''([^;]+)/i);
            if (m) { try { name = decodeURIComponent(m[1]); } catch (e) {} }
            else { m = dispo.match(/filename="?([^";]+)"?/i); if (m) name = m[1]; }
            const url = URL.createObjectURL(xhr.response);
            const a = document.createElement('a');
            a.href = url;
            a.download = name;
            a.style.display = 'none';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(url), 3000);
            toast(`已开始下载 ${paths.length} 个项目`);
        } else {
            alert('下载失败: ' + (xhr.statusText || xhr.status));
        }
    };
    xhr.onerror = () => alert('网络错误');
    xhr.send(JSON.stringify({paths: paths}));
};

// 重命名选中
let pendingRenamePaths = [];
$('#btnRenameSelected').onclick = () => {
    const checkboxes = $$('.file-checkbox');
    pendingRenamePaths = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
    if (!pendingRenamePaths.length) return;
    if (pendingRenamePaths.length === 1) {
        $('#renameModalTitle').textContent = '重命名';
        $('#renameInput').value = pendingRenamePaths[0].split('/').pop();
        $('#renameInput').placeholder = '输入新名称（含扩展名）';
    } else {
        $('#renameModalTitle').textContent = `批量重命名 (${pendingRenamePaths.length} 项)`;
        $('#renameInput').value = '';
        $('#renameInput').placeholder = '输入基础名称，自动添加序号';
    }
    $('#renameModal').style.display = 'block';
    setTimeout(() => $('#renameInput').focus(), 100);
};

$('#btnRenameCancel').onclick = () => { $('#renameModal').style.display = 'none'; };
$('#renameModal .close').onclick = () => { $('#renameModal').style.display = 'none'; };

$('#btnRenameConfirm').onclick = () => {
    const newBaseName = $('#renameInput').value.trim();
    if (!newBaseName) return alert('请输入新名称');
    const doRename = (src, dst, overwrite) => {
        const payload = {src: src, dst: dst};
        if (overwrite) payload.overwrite = true;
        api('/rename', 'POST', payload, (err, res) => {
            if (err) return alert('错误: ' + err);
            if (res.conflict) {
                showConflictModal(
                    '目标已存在',
                    '目标文件/目录已存在，是否覆盖？<br><br><code style="font-size:0.8rem;color:#8b949e">' + esc(dst) + '</code>',
                    () => doRename(src, dst, true),
                    () => { $('#renameModal').style.display = 'none'; pendingRenamePaths = []; }
                );
                return;
            }
            toast(res.msg);
            $('#renameModal').style.display = 'none';
            pendingRenamePaths = [];
            location.reload();
        });
    };
    if (pendingRenamePaths.length === 1) {
        const src = pendingRenamePaths[0];
        const parts = src.split('/');
        parts.pop();
        parts.push(newBaseName);
        doRename(src, parts.join('/'), false);
    } else {
        let completed = 0;
        const total = pendingRenamePaths.length;
        function doNext(index) {
            if (index >= total) {
                toast(`已重命名 ${total} 个项目`);
                $('#renameModal').style.display = 'none';
                pendingRenamePaths = [];
                location.reload();
                return;
            }
            const src = pendingRenamePaths[index];
            const lastDot = src.lastIndexOf('.');
            const ext = (lastDot > src.lastIndexOf('/')) ? src.substring(lastDot) : '';
            const parts = src.split('/');
            parts.pop();
            parts.push(newBaseName + '_' + (index + 1) + ext);
            api('/rename', 'POST', {src: src, dst: parts.join('/')}, (err, res) => {
                if (err) toast('重命名失败: ' + err, 'error');
                else if (res.conflict) toast('已跳过重名项: ' + res.dst, 'warning');
                doNext(index + 1);
            });
        }
        doNext(0);
    }
};

// 点击模态框外部关闭
window.onclick = e => {
    if (e.target.classList.contains('modal')) {
        e.target.style.display = 'none';
    }
};

// ESC 关闭模态框
document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        $$('.modal').forEach(m => m.style.display = 'none');
    }
});
</script>
</body>
</html>"""
        result_html = template.replace('__TITLE__', html.escape(str(rel_dir) if str(rel_dir) != "." else "根目录"))
        result_html = result_html.replace('__Crumbs__', crumbs)
        result_html = result_html.replace('__Rows__', rows)
        result_html = result_html.replace('__Pager__', pager)
        return result_html
if __name__ == "__main__":
    os.makedirs(RESOURCE_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), AuthFileHandler)
    print("🚀 Resource Server: http://{}:{}".format(HOST, PORT))
    print("   Serving: {}".format(RESOURCE_DIR))
    print("   Auth: user {} (password from RESOURCE_PASSWORD)".format(USER))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()