"""流式 multipart/form-data 解析。

浏览器上传大文件时不再整包读进内存：文件边收边写临时文件。
"""
import os
import tempfile

CHUNK = 256 * 1024


def parse_multipart(rfile, total_len, boundary, tmp_dir):
    """返回 (fields: dict[str, str], files: list[(fieldname, filename, tmp_path, size)])

    文件落在 tmp_dir 下的临时文件，调用方负责移动或删除。
    """
    delim = b"--" + boundary
    sep = b"\r\n" + delim
    fields = {}
    files = []
    remaining = total_len
    buf = b""

    def fill():
        nonlocal buf, remaining
        if remaining <= 0:
            return False
        chunk = rfile.read(min(CHUNK, remaining))
        if not chunk:
            remaining = 0
            return False
        remaining -= len(chunk)
        buf += chunk
        return True

    def consume(marker, sink):
        """读到 marker 为止；中间数据交给 sink；返回是否找到 marker。"""
        nonlocal buf
        while True:
            i = buf.find(marker)
            if i != -1:
                sink(buf[:i])
                buf = buf[i + len(marker):]
                return True
            keep = len(marker) - 1
            if len(buf) > keep:
                sink(buf[:len(buf) - keep])
                buf = buf[len(buf) - keep:]
            if not fill():
                return False

    # 前导（通常是空）
    if not consume(delim, lambda d: None):
        return fields, files

    while True:
        while len(buf) < 2 and fill():
            pass
        if buf.startswith(b"--"):
            break
        if buf.startswith(b"\r\n"):
            buf = buf[2:]
        elif not consume(b"\r\n", lambda d: None):
            break

        head = bytearray()
        if not consume(b"\r\n\r\n", head.extend):
            break
        hdrs = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        disp = hdrs.get("content-disposition", "")
        name = None
        filename = None
        for seg in disp.split(";"):
            seg = seg.strip()
            low = seg.lower()
            if low.startswith("name="):
                name = seg[5:].strip(' "')
            elif low.startswith("filename="):
                filename = seg[9:].strip(' "')

        if filename:
            fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix="up_")
            fh = os.fdopen(fd, "wb")

            def sink(chunk, _fh=fh):
                if chunk:
                    _fh.write(chunk)

            ok = False
            try:
                ok = consume(sep, sink)
            finally:
                fh.close()
            if ok:
                try:
                    size = os.path.getsize(tmp)
                except OSError:
                    size = 0
                files.append((name, filename, tmp, size))
            else:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                break
        else:
            acc = bytearray()
            if not consume(sep, acc.extend):
                break
            fields[name or ""] = acc.decode("utf-8", "replace")

    return fields, files


def extract_boundary(content_type: str):
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        return None
    for seg in content_type.split(";"):
        seg = seg.strip()
        if seg.lower().startswith("boundary="):
            return seg[9:].strip('"').encode()
    return None
