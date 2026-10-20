"""资讯图片本地化：把正文 ``[图]`` 行与头图下载到本地，失败保留外链。

与封面（app/media.py）同一套哲学：
- 只认魔术字节，不信任 URL 后缀与 Content-Type；
- 单张大小上限（``settings.article_image_max_bytes``）；
- 原子写：先写临时文件再 ``os.replace``，读者不会读到半张图；
- **任何失败都不阻塞导入**：占位行保留源站外链（源站与文章同域、服务器可直连，
  页面照样能显示），下次回填脚本还能补。

文件名形如 ``article_<key>_<序号>.<ext>``，``key`` 用详情页 id（同一篇文章
重复导入/回填会覆盖同名文件，不会堆垃圾）。
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app import article_body, media
from app.config import settings

# 与 scripts/import_articles 的抓取 UA 保持一致：源站不带 UA 会 403。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ACGNContentPlatform/1.0"
)
DEFAULT_TIMEOUT = 20.0

_EXT_BY_FORMAT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}


class ArticleImageError(RuntimeError):
    """单张资讯图无法下载或落盘（调用方应降级为外链，不中断导入）。"""


@dataclass
class LocalizeResult:
    """一次本地化的结果：改写后的正文/头图 + 成功与失败计数。"""

    body: str
    head_image: str | None
    saved: int = 0
    failed: int = 0


def article_image_root() -> Path:
    """资讯图片根目录：相对路径按项目根解析（与 main.py 的 /article-images 一致）。"""
    root = Path(settings.article_image_storage_dir)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[1] / root
    return root


def article_image_name(key: str, index: int, image_format: str) -> str:
    """生成站内文件名：只留字母数字下划线横线，防路径穿越。"""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(key))[:60]
    return f"article_{safe or 'x'}_{index}{_EXT_BY_FORMAT.get(image_format, '.jpg')}"


def _atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再 os.replace：并发读到的是完整图。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def save_article_image(data: bytes, *, key: str, index: int) -> str:
    """校验并落盘一张图，返回站内路径（``/article-images/<文件名>``）。"""
    limit = settings.article_image_max_bytes
    if len(data) > limit:
        raise ArticleImageError(f"图片超过大小上限（{len(data)} > {limit} 字节）")
    image_format = media.sniff_image(data)
    if image_format is None:
        raise ArticleImageError("响应不是可识别的图片格式（jpeg/png/webp/gif）")
    name = article_image_name(key, index, image_format)
    _atomic_write(article_image_root() / name, data)
    return f"/article-images/{name}"


def download_image(
    url: str,
    *,
    key: str,
    index: int,
    timeout: float = DEFAULT_TIMEOUT,
    opener=None,
) -> str:
    """下载一张图并落盘，返回站内路径；任何失败抛 ArticleImageError。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": DEFAULT_USER_AGENT}
    )
    limit = settings.article_image_max_bytes
    try:
        if opener is not None:
            response = opener.open(request, timeout=timeout)
        else:
            response = urllib.request.urlopen(request, timeout=timeout)
        with response:
            data = response.read(limit + 1)
    except ArticleImageError:
        raise
    except Exception as exc:  # 网络抖动、403、解码失败等一律降级为外链
        raise ArticleImageError(f"下载失败：{exc}") from exc
    if len(data) > limit:
        raise ArticleImageError(f"图片超过大小上限（{limit} 字节）")
    return save_article_image(data, key=key, index=index)


def localize_media(
    body: str,
    head_image: str | None,
    *,
    key: str,
    downloader=None,
) -> LocalizeResult:
    """本地化正文图与头图，返回改写后的正文/头图与计数。

    downloader 可注入（测试用），签名 ``downloader(url, key, index) -> 站内路径``；
    不传则走真实下载。单张失败只计数，占位行/头图保留原外链。
    """
    download = downloader or (
        lambda url, key, index: download_image(url, key=key, index=index)
    )
    if not settings.article_image_download_enabled and downloader is None:
        # 全局关闭（测试环境 / 明确不想落盘）：原样返回，页面继续用源站外链
        return LocalizeResult(body=body, head_image=head_image)

    urls: list[str] = []
    if head_image:
        urls.append(head_image)
    for url in article_body.extract_image_urls(body):
        if url not in urls:
            urls.append(url)

    mapping: dict[str, str] = {}
    saved = failed = 0
    for index, url in enumerate(urls, start=1):
        try:
            mapping[url] = download(url, str(key), index)
            saved += 1
        except Exception:  # noqa: BLE001 —— 单张失败不阻塞，保留外链
            failed += 1

    return LocalizeResult(
        body=article_body.replace_image_urls(body, mapping),
        head_image=mapping.get(head_image, head_image) if head_image else None,
        saved=saved,
        failed=failed,
    )
