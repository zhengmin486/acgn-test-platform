"""资讯正文的行级结构：纯文本 + ``[图]`` / ``[媒体]`` 占位行。

正文在库里**始终是纯文本**（导入器只做文本化，不存 HTML，避免 XSS 面）：
- ``[图] URL``   —— 图片占位行。导入时按 URL 下载本地化后改写成站内路径，
  下载失败则保留源站外链（源站与文章同域，页面照样能显示）；
- ``[媒体] URL`` —— 内嵌视频/音频（iframe）占位行，沿袭既有风格。

本模块只做「行 -> 块」的解析与占位行改写，供导入（scripts/import_articles）、
图片本地化（app/article_media）与页面渲染（app/business_pages）共用；
纯函数，不依赖数据库，也不做任何网络请求。
"""

from __future__ import annotations

IMAGE_PREFIX = "[图]"
MEDIA_PREFIX = "[媒体]"

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_MEDIA = "media"


def _placeholder_url(line: str, prefix: str) -> str | None:
    """取占位行的 URL；不是该前缀或没有 URL 时返回 None。"""
    text = (line or "").strip()
    if not text.startswith(prefix):
        return None
    return text[len(prefix):].strip() or None


def extract_image_urls(body: str) -> list[str]:
    """按出现顺序提取正文 ``[图]`` 行的 URL（去重保序）。"""
    urls: list[str] = []
    for line in (body or "").split("\n"):
        url = _placeholder_url(line, IMAGE_PREFIX)
        if url and url not in urls:
            urls.append(url)
    return urls


def replace_image_urls(body: str, mapping: dict[str, str]) -> str:
    """把 ``[图]`` 行的 URL 换成映射后的地址；没命中的行原样保留。"""
    lines: list[str] = []
    for line in (body or "").split("\n"):
        url = _placeholder_url(line, IMAGE_PREFIX)
        replacement = mapping.get(url) if url else None
        if replacement:
            lines.append(f"{IMAGE_PREFIX} {replacement}")
        else:
            lines.append(line)
    return "\n".join(lines)


def parse_blocks(body: str) -> list[dict]:
    """把正文解析成模板可直接循环的块列表。

    - 图片行 -> ``{"kind": "image", "url": ...}``
    - 媒体行 -> ``{"kind": "media", "url": ...}``
    - 其余非空行 -> ``{"kind": "text", "text": ...}``
    空行是段落分隔，不产出块（模板用段落间距表达）。
    """
    blocks: list[dict] = []
    for line in (body or "").split("\n"):
        text = line.rstrip()
        if not text.strip():
            continue
        url = _placeholder_url(text, IMAGE_PREFIX)
        if url:
            blocks.append({"kind": KIND_IMAGE, "url": url})
            continue
        url = _placeholder_url(text, MEDIA_PREFIX)
        if url:
            blocks.append({"kind": KIND_MEDIA, "url": url})
            continue
        blocks.append({"kind": KIND_TEXT, "text": text.strip()})
    return blocks
