"""文件系统操作层：路径安全、目录列表、搜索、缩略图、打包、磁盘用量。

只依赖标准库 + 系统 ffmpeg（缩略图）。
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from . import config as C
from . import history as _hist

_THUMB_SEM = threading.Semaphore(C.THUMB_CONCURRENCY)
_usage_lock = threading.Lock()
_usage_cache = {"ts": 0.0, "total": 0, "files": 0}

ICONS = {
    "dir": "📁",
    "image": "🖼", "video": "🎬", "audio": "🎵",
    "pdf": "📕", "doc": "📄", "sheet": "📊", "slide": "📽",
    "zip": "📦", "code": "🐍", "web": "🌐", "text": "📝",
    "script": "⚙", "data": "📋", "other": "📄",
}

_EXT_KIND = {}
for _e in C.INLINE_IMAGE:
    _EXT_KIND[_e] = "image"
for _e in C.VIDEO_EXTS:
    _EXT_KIND[_e] = "video"
for _e in C.AUDIO_EXTS:
    _EXT_KIND[_e] = "audio"
for _e in (".pdf",):
    _EXT_KIND[_e] = "pdf"
for _e in (".doc", ".docx", ".odt", ".rtf"):
    _EXT_KIND[_e] = "doc"
for _e in (".xls", ".xlsx", ".csv", ".ods"):
    _EXT_KIND[_e] = "sheet"
for _e in (".ppt", ".pptx", ".odp"):
    _EXT_KIND[_e] = "slide"
for _e in (".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz", ".tgz"):
    _EXT_KIND[_e] = "zip"
for _e in (".py", ".pyw", ".rb", ".pl", ".php", ".lua", ".go", ".rs", ".java"):
    _EXT_KIND[_e] = "code"
for _e in (".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svg"):
    _EXT_KIND[_e] = "web"
for _e in (".txt", ".md", ".log", ".rst"):
    _EXT_KIND[_e] = "text"
for _e in (".sh", ".bash", ".zsh", ".bat", ".cmd", ".ini", ".conf", ".cfg", ".toml", ".service"):
    _EXT_KIND[_e] = "script"
for _e in (".json", ".yaml", ".yml", ".xml", ".sql"):
    _EXT_KIND[_e] = "data"


def kind_of(name: str, is_dir: bool) -> str:
    if is_dir:
        return "dir"
    return _EXT_KIND.get(os.path.splitext(name)[1].lower(), "other")


def icon_of(name: str, is_dir: bool) -> str:
    return ICONS.get(kind_of(name, is_dir), "📄")


# ---------------- 路径安全 ----------------
def safe_path(rel_path: str):
    """把资源库相对路径解析成绝对 Path；越界返回 None。"""
    try:
        root = Path(C.RESOURCE_DIR).resolve()
        p = (root / rel_path.lstrip("/")).resolve()
        p.relative_to(root)
        return p
    except (ValueError, OSError):
        return None


def rel_of(path: Path) -> str:
    rel = path.resolve().relative_to(Path(C.RESOURCE_DIR).resolve())
    s = str(rel)
    return "" if s == "." else s


# ---------------- 图片尺寸（瀑布流布局要提前知道比例） ----------------
_dim_cache = {}
_dim_lock = threading.Lock()


def image_size(p: Path):
    """只读文件头拿宽高，不加载整图。失败返回 None。"""
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _dim_lock:
        if key in _dim_cache:
            return _dim_cache[key]
    result = None
    try:
        with open(p, "rb") as f:
            head = f.read(32)
            if len(head) >= 26 and head[:2] == b"\xff\xd8":          # JPEG
                f.seek(2)
                while True:
                    b = f.read(1)
                    if not b:
                        break
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        break
                    m = marker[0]
                    if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        f.read(3)
                        h, w = struct.unpack(">HH", f.read(4))
                        result = (w, h)
                        break
                    if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                        continue
                    ln = struct.unpack(">H", f.read(2))[0]
                    if ln < 2:
                        break
                    f.seek(ln - 2, 1)
            elif head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                result = (w, h)
            elif head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                result = (w, h)
    except (OSError, struct.error):
        result = None
    with _dim_lock:
        if len(_dim_cache) > 5000:
            _dim_cache.clear()
        _dim_cache[key] = result
    return result


# ---------------- 目录列表 ----------------
_SORT_KEYS = {
    "name": lambda e: e["name"].lower(),
    "size": lambda e: e["size"],
    "time": lambda e: e["mtime"],
    "type": lambda e: (e["kind"], e["name"].lower()),
}


def scan_entries(path: Path):
    """读一个目录的条目（不排序）。"""
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                is_dir = e.is_dir()
                st = e.stat()
            except OSError:
                continue
            kind = kind_of(e.name, is_dir)
            children = None
            if is_dir:
                try:                                   # 子项数量（只读一层，够快）
                    with os.scandir(e.path) as sub:
                        children = sum(1 for _ in sub)
                except OSError:
                    children = None
            dim = None
            if kind == "image" and not is_dir:         # 瀑布流要按原始比例排版
                dim = image_size(Path(e.path))
            out.append({
                "name": e.name,
                "is_dir": is_dir,
                "children": children,
                "dim": list(dim) if dim else None,
                "size": 0 if is_dir else st.st_size,
                "mtime": st.st_mtime,
                "kind": kind,
                "icon": ICONS.get(kind, "📄"),
                "ext": os.path.splitext(e.name)[1].lower().lstrip("."),
                "editable": (not is_dir) and os.path.splitext(e.name)[1].lower() in C.EDITABLE_EXTS,
            })
    return out


def sort_entries(entries, sort="name", order="asc"):
    key = _SORT_KEYS.get(sort, _SORT_KEYS["name"])
    # 目录永远在前，组内按选择字段排
    dirs = [e for e in entries if e["is_dir"]]
    files = [e for e in entries if not e["is_dir"]]
    rev = order == "desc"
    dirs.sort(key=key, reverse=rev)
    files.sort(key=key, reverse=rev)
    return dirs + files


def search_recursive(base: Path, query: str):
    """在 base 下递归找名称含 query 的条目，深度和结果数都设上限。"""
    q = query.lower()
    base_depth = len(base.parts)
    results = []
    for root, dirnames, filenames in os.walk(base):
        depth = len(Path(root).parts) - base_depth
        if depth >= C.MAX_SEARCH_DEPTH:
            dirnames[:] = []
        dirnames.sort()
        for dn in dirnames:
            if q in dn.lower():
                p = Path(root) / dn
                results.append(_entry_from_path(p, is_dir=True))
                if len(results) >= C.MAX_SEARCH_RESULTS:
                    return results
        for fn in sorted(filenames):
            if q in fn.lower():
                p = Path(root) / fn
                results.append(_entry_from_path(p, is_dir=False))
                if len(results) >= C.MAX_SEARCH_RESULTS:
                    return results
    return results


def _entry_from_path(p: Path, is_dir: bool):
    try:
        st = p.stat()
    except OSError:
        st = None
    kind = kind_of(p.name, is_dir)
    return {
        "name": p.name,
        "is_dir": is_dir,
        "size": 0 if is_dir else (st.st_size if st else 0),
        "mtime": st.st_mtime if st else 0.0,
        "kind": kind,
        "icon": ICONS.get(kind, "📄"),
        "ext": os.path.splitext(p.name)[1].lower().lstrip("."),
        "editable": (not is_dir) and os.path.splitext(p.name)[1].lower() in C.EDITABLE_EXTS,
        "rel": rel_of(p),
    }


# ---------------- 磁盘与容量 ----------------
def disk_usage():
    try:
        total, used, free = shutil.disk_usage(C.RESOURCE_DIR)
        return {"total": total, "used": used, "free": free,
                "percent": round(used * 100.0 / total, 1) if total else 0.0}
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}


def library_stats(max_age=60.0):
    """资源库概览：文件数、总字节、图片数、今日更新数。

    缓存 max_age 秒，避免每次刷新都全盘扫；顺带把图片数和今日改动数一起算出来
    （同一趟遍历，不额外增加 IO）。
    """
    with _usage_lock:
        now = time.time()
        if now - _usage_cache["ts"] < max_age:
            return {
                "files": _usage_cache["files"],
                "bytes": _usage_cache["total"],
                "images": _usage_cache.get("images", 0),
                "today": _usage_cache.get("today", 0),
            }
        files = 0
        total = 0
        images = 0
        today = 0
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        for root, dirs, fnames in os.walk(C.RESOURCE_DIR):
            dirs[:] = [d for d in dirs if d != ".trash"]
            for fn in fnames:
                try:
                    st = os.stat(os.path.join(root, fn))
                except OSError:
                    continue
                total += st.st_size
                files += 1
                if os.path.splitext(fn)[1].lower() in C.IMAGE_EXTS:
                    images += 1
                if st.st_mtime >= midnight:
                    today += 1
        _usage_cache.update({"ts": now, "total": total, "files": files,
                             "images": images, "today": today})
        return {"files": files, "bytes": total, "images": images, "today": today}


# ---------------- 缩略图 ----------------
def _thumb_key(src: Path, width: int) -> str:
    st = src.stat()
    raw = "{}|{}|{}|{}".format(src, st.st_mtime_ns, st.st_size, width)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def thumb_file(src: Path, width: int):
    """返回缓存缩略图路径（可能尚不存在）。"""
    return os.path.join(C.THUMB_DIR, "{}.jpg".format(_thumb_key(src, width)))


def ensure_thumb(src: Path, width: int):
    """确保缩略图存在，返回路径；失败返回 None。ffmpeg 并发受限。

    4K 大图用 -lowres 2 抽取（约 1/4 尺寸解码，快一倍）；视频取第 1 秒的非关键帧
    里第一帧（避免开场黑帧）。
    """
    if width not in C.THUMB_WIDTHS:
        width = C.THUMB_WIDTHS[0]
    try:
        os.makedirs(C.THUMB_DIR, exist_ok=True)   # 目录被误删时自动重建
    except OSError:
        pass
    out = thumb_file(src, width)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    with _THUMB_SEM:
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        tmp = "{}.{}.{}.tmp.jpg".format(out, os.getpid(), threading.get_ident())
        ext = os.path.splitext(src.name)[1].lower()
        cmd = ["ffmpeg", "-v", "error", "-y"]
        if ext in C.VIDEO_EXTS:
            cmd += ["-ss", "1", "-skip_frame", "nokey"]
        else:
            cmd += ["-lowres", "2"]
        cmd += ["-i", str(src), "-an", "-sn", "-dn",
                "-vf", "scale={}:-2:flags=fast_bilinear".format(width),
                "-frames:v", "1", "-q:v", str(C.THUMB_QUALITY), tmp]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and (proc.returncode != 0 or not os.path.exists(tmp)
                                 or os.path.getsize(tmp) == 0):
            # -lowres 对某些解码器无效（报错时重试一次不带它）
            cmd2 = [c for c in cmd if c not in ("-lowres", "2")]
            try:
                subprocess.run(cmd2, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            try:
                os.replace(tmp, out)
                return out
            except OSError:
                return None
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return None


def prune_thumbs(max_files=3000):
    """缓存超过 max_files 时按 mtime 删旧的。"""
    try:
        entries = []
        with os.scandir(C.THUMB_DIR) as it:
            for e in it:
                try:
                    entries.append((e.stat().st_mtime, e.path))
                except OSError:
                    continue
        if len(entries) <= max_files:
            return 0
        entries.sort()
        removed = 0
        for _mt, p in entries[:len(entries) - max_files]:
            try:
                os.unlink(p)
                removed += 1
            except OSError:
                pass
        return removed
    except OSError:
        return 0


def unique_copy_name(directory: Path, name: str) -> Path:
    """同目录复制时生成不冲突的名字：xxx (1).ext / xxx (2).ext …"""
    stem, ext = os.path.splitext(name)
    for i in range(1, 1000):
        cand = directory / "{} ({}){}".format(stem, i, ext)
        if not cand.exists():
            return cand
    return directory / "{} (copy){}".format(stem, ext)


# ---------------- 打包下载 ----------------
def zip_to_tmp(paths, tmp_dir):
    """把多个路径打包到临时文件，返回 (临时文件路径, 建议下载名)。避免全塞内存。"""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
    os.close(fd)
    root_res = os.path.realpath(C.RESOURCE_DIR) + os.sep

    def _inside(f: Path) -> bool:
        """软链与越界防护：只打包真实落在资源库内的普通文件。"""
        try:
            if f.is_symlink():
                return False
            return os.path.realpath(f).startswith(root_res)
        except OSError:
            return False

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for p in paths:
            base = p.name
            if p.is_dir():
                for child in p.rglob("*"):
                    if child.is_file() and _inside(child):
                        try:
                            zf.write(child, str(Path(base) / child.relative_to(p)))
                        except OSError:
                            continue
            elif _inside(p):
                try:
                    zf.write(p, base)
                except OSError:
                    continue
    name = "resource-{}.zip".format(time.strftime("%Y%m%d-%H%M%S"))
    return tmp, name


# ---------------- 上传临时区 ----------------
def tmp_dir():
    d = os.path.join(C.DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return d


def cleanup_tmp(max_age=3600):
    """清理超龄上传临时文件（进程崩溃留下的残留）。"""
    d = tmp_dir()
    now = time.time()
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if now - e.stat().st_mtime > max_age:
                        os.unlink(e.path)
                except OSError:
                    continue
    except OSError:
        pass


# ---------------- 回收站 ----------------
# 结构：data/trash/<id>/payload（原文件或目录）+ meta.json
# 用 shutil.move 走同盘 rename，删大目录也是瞬时的。
def trash_dir() -> str:
    d = os.path.join(C.DATA_DIR, "trash")
    os.makedirs(d, exist_ok=True)
    return d


def _trash_id() -> str:
    return "{}-{}".format(int(time.time() * 1000), os.urandom(3).hex())


def _dir_size(p: Path) -> int:
    total = 0
    try:
        if p.is_dir() and not p.is_symlink():
            for root, _dirs, fnames in os.walk(p):
                for fn in fnames:
                    try:
                        total += os.lstat(os.path.join(root, fn)).st_size
                    except OSError:
                        continue
        else:
            total = p.lstat().st_size
    except OSError:
        pass
    return total


def trash_move(paths):
    """把若干路径移进回收站。返回 (moved_metas, errors)。"""
    root = Path(C.RESOURCE_DIR).resolve()
    base = Path(trash_dir())
    moved, errors = [], []
    for p in paths:
        slot = None                      # 每轮重置：否则异常时拿到的是上一轮的 slot，会误删
        try:
            if not p or not p.exists() or p.resolve() == root:
                continue
            rel = rel_of(p)              # 必须在移动前算（移动后原路径已不存在）
            is_dir = p.is_dir()
            size = _dir_size(p)
            tid = _trash_id()
            slot = base / tid
            slot.mkdir(parents=True)
            payload = slot / "payload"
            shutil.move(str(p), str(payload))
            meta = {"id": tid, "orig": rel, "name": p.name,
                    "deleted_at": time.time(), "is_dir": is_dir, "size": size}
            with open(slot / "meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            moved.append(meta)
        except OSError as exc:
            errors.append("{}: {}".format(getattr(p, "name", p), exc))
            try:
                if slot is not None and slot.exists():
                    shutil.rmtree(slot, ignore_errors=True)
            except OSError:
                pass
    return moved, errors


def trash_list():
    """回收站条目，按删除时间倒序。"""
    base = trash_dir()
    items = []
    try:
        with os.scandir(base) as it:
            for e in it:
                if not e.is_dir():
                    continue
                mp = os.path.join(e.path, "meta.json")
                if not os.path.isfile(mp):
                    continue
                try:
                    with open(mp, "r", encoding="utf-8") as f:
                        m = json.load(f)
                except (OSError, ValueError):
                    continue
                m["exists"] = os.path.exists(os.path.join(e.path, "payload"))
                items.append(m)
    except OSError:
        pass
    items.sort(key=lambda m: m.get("deleted_at", 0), reverse=True)
    return items


def trash_restore(ids):
    """把条目还原回原路径；原位置被占用时自动加「(还原N)」。"""
    root = Path(C.RESOURCE_DIR).resolve()
    base = Path(trash_dir())
    restored, errors = [], []
    for tid in ids:
        slot = base / str(tid)
        try:
            slot.relative_to(base)
        except ValueError:
            errors.append("{}: 非法 ID".format(tid))
            continue
        mp, payload = slot / "meta.json", slot / "payload"
        if not mp.is_file() or not payload.exists():
            errors.append("{}: 条目不存在".format(tid))
            continue
        try:
            with open(mp, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            errors.append("{}: 元数据损坏".format(tid))
            continue
        orig = meta.get("orig") or meta.get("name") or "restored"
        target = (root / orig).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            errors.append("{}: 原路径越界".format(tid))
            continue
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for i in range(1, 1000):
                cand = target.with_name("{} (还原{}){}".format(stem, i, suffix))
                if not cand.exists():
                    target = cand
                    break
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(payload), str(target))
            shutil.rmtree(slot, ignore_errors=True)
            restored.append(meta.get("name", ""))
        except OSError as exc:
            errors.append("{}: {}".format(meta.get("name", tid), exc))
    return restored, errors


def _trash_meta(slot):
    try:
        with open(os.path.join(str(slot), "meta.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def trash_purge(ids):
    """彻底删除指定条目，返回 (数量, 释放字节)。

    顺带清掉这些文件的编辑历史 —— 文件都彻底删了，
    历史里还留着 20 版全文就等于没删干净。
    """
    base = Path(trash_dir())
    n, freed = 0, 0
    for tid in ids:
        slot = base / str(tid)
        try:
            slot.relative_to(base)
        except ValueError:
            continue
        if slot.is_dir():
            meta = _trash_meta(slot)
            freed += _dir_size(slot / "payload")
            try:
                shutil.rmtree(slot)
                n += 1
                if meta.get("orig"):
                    _hist.drop(meta["orig"])
            except OSError:
                pass
    return n, freed


def trash_empty():
    """清空回收站，返回 (数量, 释放字节)。"""
    base = trash_dir()
    n, freed = 0, 0
    try:
        with os.scandir(base) as it:
            for e in list(it):
                if not e.is_dir():
                    continue
                meta = _trash_meta(e.path)
                freed += _dir_size(Path(e.path) / "payload")
                try:
                    shutil.rmtree(e.path)
                    n += 1
                    if meta.get("orig"):
                        _hist.drop(meta["orig"])
                except OSError:
                    pass
    except OSError:
        pass
    return n, freed


def trash_auto_clean(days=None):
    """清掉超过保留期的条目（后台线程每天跑一次）。"""
    cutoff = time.time() - (C.TRASH_KEEP_DAYS if days is None else days) * 86400
    base = Path(trash_dir())
    n = 0
    for m in trash_list():
        if m.get("deleted_at", 0) < cutoff:
            try:
                shutil.rmtree(base / m["id"])
                n += 1
                if m.get("orig"):
                    _hist.drop(m["orig"])       # 过期清掉的文件，历史也一起清
            except OSError:
                pass
    return n


# ---------------- 全文搜索 ----------------
TEXT_SEARCH_EXTS = {".txt", ".md", ".markdown", ".html", ".htm", ".css", ".js",
                    ".mjs", ".json", ".py", ".sh", ".bash", ".zsh", ".yaml",
                    ".yml", ".xml", ".log", ".csv", ".ini", ".conf", ".cfg",
                    ".sql", ".toml", ".service", ".env", ".rst"}


def search_content(base: Path, q: str, max_files: int = 2000,
                   max_size: int = 2 * 1024 * 1024):
    """在 base 下递归搜文本文件内容。返回 (results, complete)。

    每个文件只取第一处命中，附带行号和片段；超过扫描上限返回 complete=False。
    """
    ql = q.lower()
    results = []
    scanned = 0
    base_depth = len(base.parts)
    for root, dirs, fnames in os.walk(base):
        depth = len(Path(root).parts) - base_depth
        if depth >= C.MAX_SEARCH_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        dirs.sort()
        for fn in sorted(fnames):
            if os.path.splitext(fn)[1].lower() not in TEXT_SEARCH_EXTS:
                continue
            p = Path(root) / fn
            try:
                if p.stat().st_size > max_size:
                    continue
            except OSError:
                continue
            scanned += 1
            if scanned > max_files:
                return results, False
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if ql in line.lower():
                            e = _entry_from_path(p, False)
                            e["hit"] = {"line": i, "text": line.strip()[:160]}
                            results.append(e)
                            break
            except OSError:
                continue
    return results, True
