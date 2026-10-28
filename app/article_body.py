"""资讯正文的结构化解析：把库里的 body 变成模板可直接循环渲染的块列表。

正文在库里有两种真实形态，渲染层必须都吃下来：

1. **纯文本 + 占位行**（主流）：导入器只做文本化，不存 HTML（避免 XSS 面）。
   - ``[图] URL``   —— 图片占位行。导入时按 URL 下载本地化后改写成站内路径，
     下载失败则保留源站外链（源站与文章同域，页面照样能显示）；
   - ``[媒体] URL`` —— 内嵌视频/音频（iframe）占位行。
2. **HTML 片段**（部分来源直接以富文本入库）：正文里带 ``<p>`` / ``<h2>`` /
   ``<ul>`` / ``<blockquote>`` / ``<figure>`` 等块级标签。早期直接按行当纯文本
   输出，标签会被转义成字面量显示（页面上出现 ``<p>`` 三个字符），段落与
   标题层级全部丢失。这里用标准库 HTMLParser 解析成结构块。

安全约定：本模块**不输出任何原始 HTML 片段**。HTML 正文被解析成「块 + 行内片段
(runs)」，runs 只携带纯文本与样式标记（加粗 / 斜体 / 链接地址），由模板用 Jinja
自动转义后拼接——JSON 列里再来一次 `|safe` 这条 XSS 面完全不存在。

本模块只做「正文 -> 块」的解析与占位行改写，供导入（scripts/import_articles）、
图片本地化（app/article_media）与页面渲染（app/business_pages）共用；
纯函数，不依赖数据库，也不做任何网络请求。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

IMAGE_PREFIX = "[图]"
MEDIA_PREFIX = "[媒体]"

KIND_TEXT = "text"  # 纯文本段落（无行内样式，模板直出）
KIND_PARA = "para"  # 带行内样式的段落（runs）
KIND_IMAGE = "image"
KIND_MEDIA = "media"
KIND_HEADING = "heading"
KIND_LIST = "list"
KIND_QUOTE = "quote"
KIND_TABLE = "table"
KIND_RULE = "rule"

# 出现以下块级标签才按 HTML 解析；纯文本正文里的「<」不会误判
# （《RO 仙境传说》这类书名号、数学比较符都不带标签名）。
_HTML_BLOCK_RE = re.compile(
    r"<\s*(?:p|div|br|h[1-6]|ul|ol|li|blockquote|figure|figcaption|table|tr|td"
    r"|img|section|article|dl|dt|dd)\b",
    re.IGNORECASE,
)

# 块级边界标签：只用来断段，本身不产出块
_BLOCK_BOUNDARY = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "figure", "dl", "dt", "dd", "body", "html", "center",
}

# 源码换行/缩进产生的空白（软空白）：汉字之间不留空格，其它语言保留一个空格。
# 用占位符 \x00 贯穿解析流程，最后一步才落定成「空串 或 一个空格」——
# 这样「标题 第二季」里作者手写的真空格不会被误删。
SOFT_SPACE = "\x00"
_CJK_RANGES = (
    (0x2E80, 0x9FFF),  # CJK 部首 / 假名 / 统一表意文字
    (0x3000, 0x303F),  # CJK 符号与标点
    (0xFF00, 0xFFEF),  # 全角字符
)

_VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "source", "area", "wbr"}

# 本站自己的媒体端点：正文里出现这些前缀说明图片/链接已经本地化过，
# 不能再用原文地址补全（否则站内图片会被拼成源站域名，变成 404）。
_LOCAL_MEDIA_PREFIXES = (
    "/article-images/",
    "/covers/",
    "/avatars/",
    "/media/",
    "/static/",
)


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


# 站内图片托管域名的尺寸后缀与社交卡片裁切后缀：
# WordPress 生成 `name-300x300.jpg`、`name-1000x630.jpg`（og 专用裁切），
# 同一张图因此有「裁切 jpg」与「原比例 webp」等多个 URL。
_SIZE_SUFFIX_RE = re.compile(r"-\d{2,5}x\d{2,5}(?=\.[A-Za-z]{2,5}$)")


def _normalized_image_path(url: str) -> str:
    """归一化图片路径：去查询串、去 ``-WxH`` 尺寸后缀、去扩展名。

    查询串里常带图片处理参数（gcores 的 ``?x-oss-process=image/resize,...``），
    尺寸后缀是 CDN/后台自动生成的变体，扩展名则可能是同图的不同编码
    （acgch 头图是 ``-1200x630.jpg``、正文首图是 ``-1200x675.webp``，
    同一张 keyvisual）。三样都去掉后才能判出「同一张图」。
    """
    text = (url or "").strip()
    if not text:
        return ""
    path = urlsplit(text).path if "://" in text else text
    path = _SIZE_SUFFIX_RE.sub("", path)
    if "." in path.rsplit("/", 1)[-1]:
        head, _, _name = path.rpartition(".")
        path = head
    return path or text


def image_key(url: str) -> str:
    """图片去重用的比较键：忽略域名、查询串、尺寸后缀与扩展名，只比归一化路径。

    封面字段可能是绝对地址、正文里可能是站内相对路径；同图还常有裁切版 /
    缩放版 / 不同编码（见 ``_normalized_image_path``）。只比原始字符串时，
    这些变体全都判成不同图，头图与正文首图就会同一张图显示两遍。
    """
    text = (url or "").strip()
    if not text:
        return ""
    return _normalized_image_path(text) or text


# ---------- 行内片段（runs）：纯文本 + 样式标记 ----------


def _is_cjk(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _clean_runs(runs: list[dict]) -> list[dict]:
    """整理行内片段：合并同一样式的相邻片段、去首尾空白、落定软空白。"""
    prepared: list[dict] = []
    for run in runs:
        if run.get("br"):
            if not prepared or prepared[-1].get("br"):
                continue  # 连续换行没有意义
            prepared.append({"br": True})
            continue
        text = (run.get("text") or "").replace("\xa0", " ").replace("\t", " ")
        text = re.sub(r"[ ]{2,}", " ", text)
        if not text.strip(" " + SOFT_SPACE):
            if not prepared or prepared[-1].get("br"):
                continue  # 段首空白
            text = SOFT_SPACE
        item = dict(run, text=text)
        if prepared and not prepared[-1].get("br") and _same_style(prepared[-1], item):
            prepared[-1] = dict(prepared[-1], text=prepared[-1]["text"] + text)
            continue
        prepared.append(item)

    while prepared and (
        prepared[0].get("br") or not prepared[0].get("text", "").strip(" " + SOFT_SPACE)
    ):
        prepared.pop(0)
    while prepared and (
        prepared[-1].get("br") or not prepared[-1].get("text", "").strip(" " + SOFT_SPACE)
    ):
        prepared.pop()

    for index, run in enumerate(prepared):
        text = run.get("text", "")
        if SOFT_SPACE not in text:
            continue
        left_run = prepared[index - 1].get("text", "") if index > 0 else ""
        right_run = (
            prepared[index + 1].get("text", "") if index + 1 < len(prepared) else ""
        )
        chars: list[str] = []
        for position, char in enumerate(text):
            if char != SOFT_SPACE:
                chars.append(char)
                continue
            left = chars[-1] if chars else left_run[-1:]
            right = text[position + 1] if position + 1 < len(text) else right_run[:1]
            if _is_cjk(left) and _is_cjk(right):
                continue
            chars.append(" ")
        run["text"] = re.sub(r"[ ]{2,}", " ", "".join(chars))

    if prepared and not prepared[0].get("br"):
        prepared[0] = dict(prepared[0], text=prepared[0]["text"].lstrip())
    if prepared and not prepared[-1].get("br"):
        prepared[-1] = dict(prepared[-1], text=prepared[-1]["text"].rstrip())
    return prepared


def _same_style(left: dict, right: dict) -> bool:
    return (
        left.get("href") == right.get("href")
        and bool(left.get("strong")) == bool(right.get("strong"))
        and bool(left.get("em")) == bool(right.get("em"))
    )


def _runs_text(runs: list[dict]) -> str:
    """片段拼回纯文本（图片说明、去重比较用）。"""
    parts: list[str] = []
    for run in runs:
        parts.append("\n" if run.get("br") else run.get("text", ""))
    return "".join(parts).replace(SOFT_SPACE, " ")


def _is_plain(runs: list[dict]) -> bool:
    """只有一个无样式片段：直接当纯文本段落输出，模板不必走 runs 渲染。"""
    return len(runs) == 1 and not runs[0].get("br") and not (
        runs[0].get("href") or runs[0].get("strong") or runs[0].get("em")
    )


# ---------- HTML 正文 -> 块 ----------


class _BodyHTMLParser(HTMLParser):
    """把正文 HTML 解析成块列表；不保留任何标签属性（链接只留 href）。"""

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.blocks: list[dict] = []
        self._runs: list[dict] = []
        self._href: list[str] = []
        self._strong = 0
        self._em = 0
        self._heading = 0
        self._caption = 0
        self._skip = 0
        self._list: dict | None = None
        self._list_depth = 0
        self._quote: dict | None = None
        self._quote_depth = 0
        self._table: dict | None = None
        self._row: list | None = None
        self._cell: dict | None = None

    # ----- 地址处理 -----

    def _abs(self, url: str) -> str:
        """链接/图片地址补全：正文里的相对路径按原文地址解析。"""
        text = (url or "").strip()
        if not text or text.startswith(("javascript:", "data:")):
            return ""
        if text.startswith(("#", "mailto:")):
            return text if text.startswith("mailto:") else ""
        if text.startswith(_LOCAL_MEDIA_PREFIXES):
            return text
        if self.base_url and not urlsplit(text).scheme:
            return urljoin(self.base_url, text)
        return text

    # ----- 片段构造 -----

    def _push(self, text: str) -> None:
        if not text:
            return
        run: dict = {"text": text}
        if self._strong:
            run["strong"] = True
        if self._em:
            run["em"] = True
        if self._href and self._href[-1]:
            run["href"] = self._href[-1]
        self._runs.append(run)

    def _flush(self) -> None:
        """把累积的行内片段落到当前容器（单元格 / 列表项 / 引用 / 标题 / 段落）。"""
        runs = _clean_runs(self._runs)
        self._runs = []
        if not runs:
            return

        if self._caption:
            text = _runs_text(runs).strip()
            if not text:
                return
            last = self.blocks[-1] if self.blocks else None
            if last is not None and last.get("kind") == KIND_IMAGE:
                # 图注紧贴对应图片，保持语义关系（<figure><img><figcaption>）
                last["caption"] = text
            else:
                self.blocks.append({"kind": KIND_PARA, "runs": runs})
            return

        if self._cell is not None:
            self._cell["runs"].extend(runs)
            return

        if self._list is not None:
            self._list["items"].append(runs)
            return

        if self._quote is not None:
            self._quote["paras"].append(runs)
            return

        if self._heading:
            self.blocks.append(
                {"kind": KIND_HEADING, "level": self._heading, "runs": runs}
            )
            return

        if _is_plain(runs):
            self.blocks.append({"kind": KIND_TEXT, "text": runs[0]["text"]})
        else:
            self.blocks.append({"kind": KIND_PARA, "runs": runs})

    # ----- HTMLParser 回调 -----

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            return
        if self._skip:
            return
        attr = {key.lower(): (value or "") for key, value in attrs}

        if tag == "br":
            self._runs.append({"br": True})
        elif tag == "img":
            url = self._abs(attr.get("src", "") or attr.get("data-src", ""))
            self._flush()
            if url:
                self.blocks.append({"kind": KIND_IMAGE, "url": url})
        elif tag == "hr":
            self._flush()
            self.blocks.append({"kind": KIND_RULE})
        elif tag in ("strong", "b"):
            self._strong += 1
        elif tag in ("em", "i"):
            self._em += 1
        elif tag == "a":
            self._href.append(self._abs(attr.get("href", "")))
        elif tag == "figcaption":
            self._flush()
            self._caption += 1
        elif tag in ("h1", "h2"):
            self._flush()
            self._heading = 2
        elif tag in ("h3", "h4", "h5", "h6"):
            self._flush()
            self._heading = 3
        elif tag in ("ul", "ol"):
            self._flush()
            if self._list is None:
                # 嵌套列表拍平进同一个列表块：保住条目顺序，不引入二级缩进
                self._list = {"kind": KIND_LIST, "ordered": tag == "ol", "items": []}
                self.blocks.append(self._list)
            self._list_depth += 1
        elif tag == "li":
            self._flush()
        elif tag == "blockquote":
            self._flush()
            if self._quote is None:
                self._quote = {"kind": KIND_QUOTE, "paras": []}
                self.blocks.append(self._quote)
            self._quote_depth += 1
        elif tag == "table":
            self._flush()
            self._table = {"kind": KIND_TABLE, "rows": [], "head_row": False}
            self.blocks.append(self._table)
        elif tag == "tr":
            self._flush()
            self._row = []
            if self._table is not None:
                self._table["rows"].append(self._row)
        elif tag in ("td", "th"):
            self._flush()
            self._cell = {"runs": [], "head": tag == "th"}
            if self._row is not None:
                self._row.append(self._cell)
            if (
                tag == "th"
                and self._table is not None
                and len(self._table["rows"]) == 1
            ):
                self._table["head_row"] = True
        elif tag in _BLOCK_BOUNDARY:
            self._flush()

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return

        if tag in ("strong", "b"):
            self._strong = max(0, self._strong - 1)
        elif tag in ("em", "i"):
            self._em = max(0, self._em - 1)
        elif tag == "a":
            if self._href:
                self._href.pop()
        elif tag == "figcaption":
            self._flush()
            self._caption = max(0, self._caption - 1)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self._heading = 0
        elif tag in ("ul", "ol"):
            self._flush()
            self._list_depth = max(0, self._list_depth - 1)
            if self._list_depth == 0:
                self._list = None
        elif tag == "li":
            self._flush()
        elif tag == "blockquote":
            self._flush()
            self._quote_depth = max(0, self._quote_depth - 1)
            if self._quote_depth == 0:
                self._quote = None
        elif tag == "table":
            self._flush()
            self._table = None
        elif tag == "tr":
            self._flush()
            self._row = None
        elif tag in ("td", "th"):
            self._flush()
            self._cell = None
        elif tag in _BLOCK_BOUNDARY:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        text = data.replace("\r", "")
        # 源码换行 / 缩进 -> 软空白占位；单个真空格原样保留
        text = re.sub(r"[ \t]*\n[ \t]*", SOFT_SPACE, text)
        text = re.sub(rf"{SOFT_SPACE}+", SOFT_SPACE, text)
        if not text.strip(" " + SOFT_SPACE):
            if not text:
                return
            if not self._runs:
                return  # 段首空白
            self._runs.append({"text": SOFT_SPACE})
            return
        self._push(text)

    def close(self) -> None:
        super().close()
        self._flush()


def _parse_html(body: str, base_url: str) -> list[dict]:
    parser = _BodyHTMLParser(base_url=base_url)
    parser.feed(body)
    parser.close()
    return _drop_empty_blocks(parser.blocks)


def _drop_empty_blocks(blocks: list[dict]) -> list[dict]:
    """清掉解析过程产生的空容器（空列表 / 空引用 / 空表格 / 连续分隔线）。"""
    cleaned: list[dict] = []
    for block in blocks:
        kind = block.get("kind")
        if kind == KIND_LIST and not block["items"]:
            continue
        if kind == KIND_QUOTE and not block["paras"]:
            continue
        if kind == KIND_TABLE and not block["rows"]:
            continue
        if kind == KIND_RULE and (not cleaned or cleaned[-1].get("kind") == KIND_RULE):
            continue
        cleaned.append(block)
    return cleaned


# ---------- 纯文本正文 -> 块 ----------


def _parse_text(body: str) -> list[dict]:
    """行 -> 块的解析（与 HTML 正文共用同一套块结构）。

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


def parse_blocks(body: str, base_url: str = "") -> list[dict]:
    """把正文解析成模板可直接循环的块列表。

    base_url：原文地址，用于补全 HTML 正文里的相对链接与图片路径
    （相对地址不补全会指向本站，变成死链）。
    """
    text = body or ""
    if _HTML_BLOCK_RE.search(text):
        return _parse_html(text, base_url)
    return _parse_text(text)
