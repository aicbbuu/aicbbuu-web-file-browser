"""配置与常量：所有环境变量解析集中在这里，其它模块只 import 常量。"""
import hashlib
import os
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RESOURCE_DIR = os.path.realpath(os.path.expanduser(os.environ.get("RESOURCE_DIR", "~/resource")))
HOST = os.environ.get("RESOURCE_HOST", "0.0.0.0")
# 默认 443：局域网里直接 https://<这台机器的IP>/ 就能打开，不用记端口。
# 443 是特权端口，普通用户绑不上 —— install.sh 会用 systemd 的
# AmbientCapabilities 单独授权（程序本身仍以普通用户身份运行）。
PORT = int(os.environ.get("RESOURCE_PORT", "443"))

# TLS：证书放项目内 certs/。没有证书就自动降级成 HTTP，不会启动失败。
TLS_ENABLE = os.environ.get("RESOURCE_TLS", "1") != "0"
TLS_CERT = os.path.expanduser(
    os.environ.get("RESOURCE_TLS_CERT", os.path.join(BASE_DIR, "certs", "server.crt")))
TLS_KEY = os.path.expanduser(
    os.environ.get("RESOURCE_TLS_KEY", os.path.join(BASE_DIR, "certs", "server.key")))

USER = os.environ.get("RESOURCE_USER", "admin")

# 安全：禁止硬编码默认口令。未设置 RESOURCE_PASSWORD 时直接拒绝启动（fail-closed），
# 避免服务以弱口令裸奔在 0.0.0.0 上。
PASSWORD = os.environ.get("RESOURCE_PASSWORD")
if not PASSWORD:
    raise SystemExit(
        "拒绝启动：没有设置登录密码。\n\n"
        "本程序不内置任何默认口令 —— 密码必须由环境变量 RESOURCE_PASSWORD 提供。\n\n"
        "临时跑一次：\n"
        "    RESOURCE_PASSWORD='你的密码' python3 web_file_browser.py\n\n"
        "装成系统服务（推荐）：\n"
        "    sudo bash install.sh\n"
        "    （会引导你设密码，写进 ~/.config/web-file-browser.env，权限 600）"
    )
PASS_HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()

# 数据目录：会话持久化、缩略图缓存
DATA_DIR = os.environ.get("RESOURCE_DATA_DIR", os.path.join(BASE_DIR, "data"))
THUMB_DIR = os.path.join(BASE_DIR, "thumbs")
SESSION_FILE = os.path.join(DATA_DIR, "sessions.json")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

CHUNK_SIZE = 64 * 1024

# 可在线编辑的文本扩展名（集中定义，避免多处硬编码导致的漏改/不一致）
EDITABLE_EXTS = {".txt", ".html", ".css", ".js", ".json", ".md", ".yaml", ".yml",
                 ".toml", ".xml", ".svg", ".log", ".py", ".pyw", ".sh", ".bash",
                 ".zsh", ".bsh", ".pl", ".rb", ".php", ".bat", ".cmd", ".awk",
                 ".sed", ".sql", ".ini", ".conf", ".cfg"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
# 可直接在灯箱里内嵌显示（浏览器原生支持）
INLINE_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
INLINE_VIDEO = {".mp4", ".webm", ".m4v", ".mov"}
INLINE_AUDIO = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".opus"}

# 会话
SESSION_TTL = 12 * 3600
COOKIE_NAME = "resource_session"

# 目录列表分页
PAGE_SIZE = 200
GRID_PAGE_SIZE = 120

# 登录失败限速
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 900

# 缩略图
THUMB_WIDTHS = (320, 720)      # 允许请求的宽度档，防止任人指定尺寸耗 CPU
THUMB_CONCURRENCY = 2          # ffmpeg 并发上限（J1900 双核起步，别开高）
THUMB_QUALITY = 4              # ffmpeg -q:v（2 最好 31 最差）
SMALL_IMAGE_DIRECT = 250 * 1024  # 小于此体积的图片不做转换，直接给原文件

# 递归搜索
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_DEPTH = 12

# 在线编辑单文件上限（超过就只给下载，避免整份读进内存）
MAX_EDIT_BYTES = 8 * 1024 * 1024
# 后台清理节拍（秒）：上传中转残留 + 缩略图缓存 + 回收站过期
CLEANUP_INTERVAL = 3600
# 回收站保留天数（超期由后台清理线程删除）
TRASH_KEEP_DAYS = 30
# 分享链接最长有效期（天）
SHARE_MAX_DAYS = 30


def fmt_size(size: float) -> str:
    for unit in ["B", "K", "M", "G"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
