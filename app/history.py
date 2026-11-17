"""文本编辑历史：每次保存前留一份旧版本，可查看/回滚。

存储：data/history/<sha1(rel)[:16]>/<毫秒时间戳>.txt，每个文件保留最近 KEEP 份。
"""
import hashlib
import json
import os
import shutil
import threading
import time

from . import config as C

KEEP = 20
_root = os.path.join(C.DATA_DIR, "history")
_lock = threading.Lock()


def _slot(rel: str) -> str:
    return os.path.join(_root, hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16])


def note(rel: str, old_content: str):
    """保存前调用：把旧内容落一份（失败静默，不影响主流程）。"""
    if old_content is None:
        return
    try:
        with _lock:
            d = _slot(rel)
            os.makedirs(d, exist_ok=True)
            ts = int(time.time() * 1000)
            with open(os.path.join(d, "{}.txt".format(ts)), "w", encoding="utf-8") as f:
                f.write(old_content)
            with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"rel": rel}, f, ensure_ascii=False)
            files = sorted(f for f in os.listdir(d) if f.endswith(".txt"))
            for old in files[:-KEEP]:
                try:
                    os.unlink(os.path.join(d, old))
                except OSError:
                    pass
    except OSError:
        pass


def list_versions(rel: str):
    d = _slot(rel)
    out = []
    try:
        for fn in sorted((f for f in os.listdir(d) if f.endswith(".txt")), reverse=True):
            p = os.path.join(d, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"ts": int(fn[:-4]), "size": st.st_size})
    except OSError:
        pass
    return out


def read_version(rel: str, ts: int):
    p = os.path.join(_slot(rel), "{}.txt".format(int(ts)))
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def drop(rel: str):
    shutil.rmtree(_slot(rel), ignore_errors=True)


def prune_orphans(root: str, trash_base: str) -> int:
    """清掉「原文件已经不存在」的历史目录。

    文件被外部 rm -rf / 手动删掉时不会走回收站，历史就一直留着 ——
    既占磁盘，也等于删了文件但内容还躺在 data/history 里。后台每小时扫一次。
    回收站里的条目要留着（还原后还能翻历史），所以两边都查。
    """
    in_trash = set()
    try:
        for tid in os.listdir(trash_base):
            mp = os.path.join(trash_base, tid, "meta.json")
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    orig = (json.load(f) or {}).get("orig")
                if orig:
                    in_trash.add(orig)
            except (OSError, ValueError):
                continue
    except OSError:
        pass

    removed = 0
    try:
        for name in os.listdir(_root):
            d = os.path.join(_root, name)
            if not os.path.isdir(d):
                continue
            rel = ""
            try:
                with open(os.path.join(d, "meta.json"), "r", encoding="utf-8") as f:
                    rel = (json.load(f) or {}).get("rel", "")
            except (OSError, ValueError):
                pass
            if not rel:
                shutil.rmtree(d, ignore_errors=True)     # 拿不到原始路径，无从判断
                removed += 1
                continue
            if not os.path.exists(os.path.join(root, rel)) and rel not in in_trash:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed
