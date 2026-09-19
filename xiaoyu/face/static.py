"""表情脸的静态网页伺服（B 阶段第一步）。

为什么要有这个：
    以前这张脸只能在笔记本上双击本地文件打开，平板做不到 —— 所以当时平板那边
    临时起了个 `python -m http.server 8080`（重启就没了）。把网页交给主控自己发，
    平板只要打开 `http://<主机IP>:8765/` 就是脸：和 WebSocket 同一个端口，
    页面里的 location.hostname 天然就是主机地址，**配对零操作**。

只依赖标准库，而且是纯逻辑（给个 URL 路径 -> 给份字节），好单测。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

INDEX = "index.html"

# 明确列出来，不靠 mimetypes 猜：少一个 Content-Type，平板上的图标/字体就变方块。
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

# 页面里那个空 meta：主控伺服它的时候把真 token 填进去，平板就不用带 ?token=...
TOKEN_MARKER = '<meta name="xiaoyu-token" content="">'


def content_type_for(path: Path) -> str:
    kind = CONTENT_TYPES.get(path.suffix.lower())
    if kind:
        return kind
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def resolve(web_root: Path, url_path: str) -> tuple[str, bytes] | None:
    """URL 路径 -> (Content-Type, 内容)。文件不在 / 路径不合法一律返回 None。

    安全边界（v3 §3.1：只在家里 WiFi 用，不映射公网）：这里挡掉跳出 face/ 的
    路径 —— 真让它跑出去，局域网里任何人 `http://<主机IP>:8765/../.env`
    就能把 API Key 和记忆库打包带走。局域网也不是法外之地。
    """
    path = unquote(urlparse(url_path or "/").path or "/")
    if path.endswith("/"):
        path += INDEX
    relative = Path(path.lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts:
        return None

    root = web_root.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        return None
    if not target.is_file():
        return None
    try:
        return content_type_for(target), target.read_bytes()
    except OSError:
        return None


def with_token(html: bytes, token: str) -> bytes:
    """把真 token 填进页面。

    找不到那个 meta 就原样返回 —— 页面还能靠 ?token= 或默认值工作，
    这个函数只负责"能省一步就省一步"，不该因为页面改版就把脸搞挂。
    """
    if not token:
        return html
    replacement = f'<meta name="xiaoyu-token" content="{token}">'.encode("utf-8")
    return html.replace(TOKEN_MARKER.encode("utf-8"), replacement)