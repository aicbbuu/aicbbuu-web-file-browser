"""模板渲染：页面骨架从 templates/ 读取，用 @@TOKEN@@ 占位替换。

不引模板引擎（JS 里 ${} 太多，容易和模板语法打架）。
"""
import html
import json
import os
import urllib.parse

from . import config as C

TEMPLATES_DIR = os.path.join(C.BASE_DIR, "templates")
STATIC_DIR = os.path.join(C.BASE_DIR, "static")
_crumb_lock_seen = {}


def asset_version() -> str:
    """静态资源指纹：取 static/ 下最新 mtime，改动后浏览器自动拿新文件。"""
    newest = 0.0
    try:
        with os.scandir(STATIC_DIR) as it:
            for e in it:
                try:
                    newest = max(newest, e.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return format(int(newest), "x") if newest else "0"


_sprite_cache = {"mtime": -1.0, "text": ""}


def icons_sprite() -> str:
    """内联 SVG 图标 sprite：同源 <use> 兼容性最好，也不占额外请求。"""
    path = os.path.join(STATIC_DIR, "icons.svg")
    try:
        mt = os.path.getmtime(path)
        if _sprite_cache["mtime"] != mt:
            with open(path, "r", encoding="utf-8") as f:
                _sprite_cache["text"] = f.read()
            _sprite_cache["mtime"] = mt
        return _sprite_cache["text"]
    except OSError:
        return ""


def js_json(obj) -> str:
    """安全地嵌进 <script> 的 JSON：转义 </ 防止提前闭合标签。"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def url_path(rel: str, trailing_slash: bool = False) -> str:
    """把资源库相对路径转成安全的 URL 路径（逐段编码，不会被 # ? 等字符截断）。"""
    segs = [urllib.parse.quote(s, safe="") for s in rel.split("/") if s]
    out = "/" + "/".join(segs) if segs else "/"
    if trailing_slash and segs:
        out += "/"
    return out


def _render(tpl_name: str, **tokens) -> str:
    path = os.path.join(TEMPLATES_DIR, tpl_name)
    with open(path, "r", encoding="utf-8") as f:
        tpl = f.read()
    for k, v in tokens.items():
        tpl = tpl.replace("@@" + k.upper() + "@@", str(v))
    # 兜底：任何没被替换的占位符一律清空，避免把 "@@ICONS@@" 这类字样渲到页面上
    import re
    return re.sub(r"@@[A-Z0-9_]+@@", "", tpl)


def render_login(error: str = "") -> str:
    err_html = ('<div class="login-error">{}</div>'.format(html.escape(error))
                if error else "")
    return _render("login.html", v=asset_version(), error=err_html, icons=icons_sprite())


def render_list() -> str:
    return _render("list.html", v=asset_version(), title="文件库", icons=icons_sprite())


def render_editor(file_path: str, file_name: str, content: str) -> str:
    """返回按钮指向所在目录：必须带尾斜杠，且每段 URL 编码
    （目录名含 # ? 空格 中文时，未编码/无斜杠会跳到错误地址）。"""
    dir_part = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    back = url_path(dir_part, trailing_slash=True) if dir_part else "/"
    return _render(
        "editor.html",
        v=asset_version(),
        name=html.escape(file_name, quote=True),
        back=html.escape(back, quote=True),
        pathdisp=html.escape(file_path),
        content=html.escape(content),
        pathsv=js_json(file_path),
        icons=icons_sprite(),
    )


def crumbs_for(parts):
    """把路径分段转成面包屑数据结构给前端。"""
    out = []
    for i, p in enumerate(parts):
        out.append({"name": p, "rel": "/".join(parts[:i + 1]),
                    "cur": i == len(parts) - 1})
    return out
