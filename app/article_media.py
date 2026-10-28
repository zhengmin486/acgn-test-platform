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
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app import article_body, media
from app.config import settings

# 2026-10-25：旧 UA 是 `Mozilla/5.0 ... ACGNContentPlatform/1.0`，自曝脚本身份；
# 统一成与抓取层（scripts.import_articles.DEFAULT_USER_AGENT）相同的主流浏览器
# UA，并补 Accept-Language。实测各源图床两种 UA 都能下（防盗链不拦），但服务器
# 出口与本地代理不是同一套风控，不留在文件里的自曝 UA 没有任何好处。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.5",
}
DEFAULT_TIMEOUT = 20.0

_EXT_BY_FORMAT = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}

# 原图增强白名单（2026-10-25，实测可拿原图后才收）：
# - gcores 的图床在 URL 上带 OSS 处理参数（resize/m_fill/quality），feed 给的是
#   626×292 缩图；去掉 query 即 1600×900 原图（实测 200）。
# - lisani 是 WordPress，上传目录自动生成 `name-300x300.jpg` 缩略图；
#   去掉 `-WxH` 后缀即原图（实测 200）。
# 只对这两个域名生效：其它域的 URL 形态没实测过，不猜。
_OSS_QUERY_HOSTS = ("image.gcores.com",)
_WP_SIZE_HOSTS = ("www.lisani.jp", "lisani.jp")
_WP_SIZE_SUFFIX_RE = re.compile(r"-\d{2,5}x\d{2,5}(?=\.[A-Za-z]{2,5}$)")


def upgraded_image_variants(url: str) -> list[str]:
    """返回「更可能是原图」的候选地址（可能为空）。

    调用方先试变体、失败再回退原 URL——增强只提成功率，不改变失败时的行为。
    """
    text = (url or "").strip()
    if not text.startswith(("http://", "https://")):
        return []
    parsed = urlsplit(text)
    host = parsed.netloc.lower()
    if host in _OSS_QUERY_HOSTS and parsed.query:
        return [urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))]
    if host in _WP_SIZE_HOSTS:
        stripped = _WP_SIZE_SUFFIX_RE.sub("", parsed.path)
        if stripped != parsed.path:
            return [urlunsplit((parsed.scheme, parsed.netloc, stripped, "", ""))]
    return []


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
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
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

    def download_prefer_original(url: str, key: str, index: int) -> str:
        """先试原图变体，失败回退 feed 给的地址（增强不改失败语义）。"""
        variants = upgraded_image_variants(url)
        last_error: Exception | None = None
        for candidate in [*variants, url]:
            try:
                return download(candidate, key, index)
            except Exception as exc:  # noqa: BLE001 - 逐个变体尝试，全失败抛最后一个
                last_error = exc
        raise last_error if last_error else ArticleImageError("下载失败")

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
            mapping[url] = download_prefer_original(url, str(key), index)
            saved += 1
        except Exception:  # noqa: BLE001 —— 单张失败不阻塞，保留外链
            failed += 1

    return LocalizeResult(
        body=article_body.replace_image_urls(body, mapping),
        head_image=mapping.get(head_image, head_image) if head_image else None,
        saved=saved,
        failed=failed,
    )
