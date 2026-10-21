"""多源资讯采集：RSS/RDF/Atom 源 + 站点列表源，统一输出 NewsCandidate。

为什么要多源：单一 Aniplex 官方站每天只有个位数新稿，且国内访问不稳；
按网站定位（动画 / 漫画 / 游戏 / 轻小说 / 综合动态）补几个稳定源，才能凑出「每天 30 条」。

分类口径（scripts/news_daily 统一执行，详见 scripts/news_classify）：
- 主体类型（面向用户的分类）：动画 / 漫画 / 轻小说 / 游戏 / 综合动态；
  音乐 / 创作者 / 业界不另立主体，按关联作品归入四类（如リスアニ 是动画歌曲杂志，默认归动画）；
- 事件类型（辅助分类）：新作发布 / 发售 / 歌曲发布 / 创作者动态 / 健康状况 /
  制作消息 / 行业事件 / 其他；
- 地区（内部权重，不面向用户）：日本 / 国产 / 欧美 / 其他；
- priority_score：主体 + 游戏子类 + 地区 + 事件 + 来源类型 叠加，落库后排序。

源分两类：
- ``rss``：RSS 2.0 / RSS 1.0(RDF) / Atom 订阅源，标题、链接、摘要、时间一次拿全，
  正文用 description/summary/content 转纯文本（站内正文格式：纯文本 + ``[图] URL`` 占位行）；
- ``html``：列表页 + 详情页抓取（游民星空 ACG），列表拿标题链接，
  详情页取发布时间、meta 摘要与正文。

语言：``zh`` 源直接入库；``ja`` 源需要自动翻译，没有可用 LLM Key 时整源跳过
（见 app/news_llm.translate_available）。

所有解析函数都只做纯文本处理、可注入 fetch，测试不触网。
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime

from scripts.import_articles import clean_text, html_to_text

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SUMMARY_MAX = 140  # 摘要长度，与 Aniplex 导入保持一致
BODY_MAX_CHARS = 12000  # 单篇正文上限，防止个别超长页拖垮入库


@dataclass(frozen=True)
class SourceSpec:
    """一个资讯源。kind 决定用哪套解析器，lang 决定是否需要翻译。

    category 是该源的**默认主体类型**（面向用户的分类）；region 是该源的默认地区
    （内部权重）；source_type 是来源类型（媒体编译 / 原文）。逐条资讯会再按标题 /
    摘要细化事件类型、游戏子类与地区（见 scripts/news_classify）。
    """

    key: str
    label: str  # 写入 Article.source
    url: str  # RSS 地址或列表页
    kind: str  # rss / html
    category: str  # 默认主体类型：动画 / 漫画 / 轻小说 / 游戏 / 综合动态
    lang: str  # zh / ja
    region: str = "日本"  # 默认地区（内部权重参考）
    source_type: str = "媒体编译"  # 来源类型
    max_items: int = 20


# 注册顺序不再决定取数顺序：scripts/news_daily.run_import_daily 会按
# priority_score 归集后再填每日软配额。这里仅按语言分组排列，便于阅读。
SOURCES: tuple[SourceSpec, ...] = (
    # —— 日语源（动画 / 漫画 / 轻小说 / 游戏）——
    # natalie_comic（コミックナタリー）是本类最稳的漫画 RSS，但本服务器出网对其返回 405，
    # 需配出网代理后才会真正抓到；先登记占位，配代理即自动启用（与既有的 animeanime/4gamer 同处境）。
    SourceSpec("natalie_comic", "コミックナタリー", "https://natalie.mu/comic/rss", "rss", "漫画", "ja"),
    SourceSpec("animeanime", "アニメ！アニメ！", "https://animeanime.jp/rss/index.rdf", "rss", "动画", "ja"),
    SourceSpec("4gamer", "4Gamer", "https://www.4gamer.net/rss/index.xml", "rss", "游戏", "ja"),
    SourceSpec("inside", "インサイド", "https://www.inside-games.jp/rss/index.rdf", "rss", "游戏", "ja"),
    SourceSpec("gamespark", "GameSpark", "https://www.gamespark.jp/rss/index.rdf", "rss", "游戏", "ja"),
    # リスアニ！是动画歌曲杂志，按「ACGN 相关音乐按关联主体归类」原则默认归动画
    # （事件类型会标 歌曲发布），不另立音乐主体。
    SourceSpec("lisani", "リスアニ！", "https://www.lisani.jp/feed/", "rss", "动画", "ja"),
    # ln-news（ラノベニュースオンライン）是轻小说专门站，Atom 格式、当天更新，服务器可直连。
    SourceSpec("lnnews", "ラノベニュースオンライン", "https://ln-news.com/feed", "rss", "轻小说", "ja"),
    # —— 中文源（综合动态 / 动画，均为日本 ACGN 的中文编译）——
    # ACGCH（acgch.com）简中版：日本媒体的中文编译，标题规范、无水文；
    # RSS 恒 50 条，站方分类（动画/漫画/轻小说/业界/活动周边/虚拟主播）与
    # 本站分类对齐，虚拟主播类落「综合动态」兜底桶。2026-10-21 调查后接入。
    SourceSpec("acgch", "ACGCH", "https://acgch.com/zh-hans/rss.xml", "rss", "综合动态", "zh"),
    SourceSpec("gcores", "机核 GCORES", "https://www.gcores.com/rss", "rss", "综合动态", "zh"),
    # 触乐：中文游戏行业 / 创作者向媒体（人物专访、行业特稿），补中文行业缺口；
    # 「触乐怪话 / 触乐夜话」栏目为编辑随笔，由 news_filters 按标题前缀过滤。
    SourceSpec("chuapp", "触乐", "http://www.chuapp.com/feed", "rss", "游戏", "zh"),
    SourceSpec("gamersky", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh"),
)

SOURCE_BY_KEY = {spec.key: spec for spec in SOURCES}


@dataclass
class NewsCandidate:
    """采集到的一条待入库资讯；needs_detail 为真时由 hydrate 补摘要与正文。"""

    title: str
    url: str
    source: SourceSpec
    published_at: datetime | None = None
    summary: str = ""
    body: str = ""
    image_url: str | None = None
    needs_detail: bool = False
    notes: list[str] = field(default_factory=list)
    # 分类结果缓存（scripts/news_daily._classify 写入）：排序 / 软配额 / 落库会多次取用，
    # 挂在对象自身而非全局字典上，避免按对象 id 缓存时被回收后复用的 id 串号。
    classification: dict | None = None
    # 标题与摘要是否已由 LLM 译成中文：只影响正文尾注，避免把日文原文标成「译文」。
    translated: bool = False


class NewsSourceError(RuntimeError):
    """源站不可用或返回内容无法解析。"""


# ---------- RSS / RDF 解析 ----------

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _local(tag: str) -> str:
    """去掉 XML 命名空间，只看本地名（rss1.0 的 item 带命名空间）。"""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, *names: str) -> str:
    """按本地名找第一个非空子元素文本（支持 media:thumbnail / dc:date 这类前缀）。"""
    wanted = {name.lower() for name in names}
    for child in element:
        if _local(child.tag) in wanted and (child.text or "").strip():
            return child.text.strip()
    return ""


def _attr_by_local(element: ET.Element, name: str, attr: str = "url") -> str:
    """在子树里找本地名为 name 的元素并取属性（media:thumbnail/enclosure）。"""
    for child in element.iter():
        if _local(child.tag) == name.lower():
            value = (child.get(attr) or "").strip()
            if value:
                return value
    return ""


def _to_local_naive(parsed: datetime) -> datetime:
    """带时区的时间换算成本机时区后去掉 tzinfo（入库统一存本地 naive 时间）。"""
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone().replace(tzinfo=None)


def parse_feed_date(raw: str) -> datetime | None:
    """解析订阅源时间：RFC822（RSS2.0）/ ISO-W3C（RDF 的 dc:date、Atom 的 updated）。

    旧版 Python 的 ``datetime.fromisoformat`` 不接受 ``+09:00`` 这类带冒号时区，
    这里兜底去掉时区后缀再解析。

    时区必须**换算**到本机时区后再转 naive：日文源是 JST(+09:00)，直接
    ``replace(tzinfo=None)`` 会让 22:00 的日本新闻在中文站显示成 22:00
    （实际是北京时间 21:00），时效差一小时；GMT 源更会差 8 小时。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return _to_local_naive(parsed)
    except (TypeError, ValueError):
        pass
    candidate = text.replace("Z", "+00:00")
    for attempt in (candidate, re.sub(r"[+-]\d{2}:\d{2}$", "", candidate)):
        try:
            parsed = datetime.fromisoformat(attempt)
        except ValueError:
            continue
        return _to_local_naive(parsed)
    return None


def _link_href(element: ET.Element, rel: str | None = None) -> str:
    """在子树里找 ``<link>``，可选按 ``rel`` 过滤，返回 ``href``（Atom 用）。"""
    for child in element.iter():
        if _local(child.tag) == "link":
            if rel is None or (child.get("rel") or "").lower() == rel:
                href = (child.get("href") or "").strip()
                if href:
                    return href
    return ""


def _entry_image(element: ET.Element, description: str) -> str:
    """优先 Atom 的 ``rel=enclosure`` 图片链接，退回 RSS 的 thumbnail/enclosure，最后正文首图。"""
    for child in element.iter():
        if _local(child.tag) == "link" and (child.get("rel") or "").lower() == "enclosure":
            if (child.get("type") or "").lower().startswith("image"):
                href = (child.get("href") or "").strip()
                if href:
                    return href
    return (
        _attr_by_local(element, "thumbnail")
        or _attr_by_local(element, "enclosure")
        or _first_image(description)
    )


def _entry_url(element: ET.Element, source: SourceSpec) -> str:
    """提取条目链接：Atom 优先 ``rel=alternate`` 的绝对地址，RSS 退回 ``<link>`` 文本/属性。"""
    href = _link_href(element, "alternate") or _link_href(element, "self")
    if href:
        return urljoin(source.url, href)
    text = _child_text(element, "link")
    if text:
        return urljoin(source.url, text)
    return urljoin(source.url, _attr_by_local(element, "link", "href"))


def parse_feed(xml_text: str, source: SourceSpec) -> list[NewsCandidate]:
    """解析 RSS 2.0 / RSS 1.0(RDF) / Atom 订阅源，返回候选条目（按源站顺序）。

    RSS 用 ``<item>``；Atom 用 ``<entry>``，链接取 ``<link rel="alternate" href>``，
    图片优先 ``<link rel="enclosure" type="image/*">``，时间取 ``<updated>/<published>``。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NewsSourceError(f"{source.label} 订阅源解析失败：{exc}") from exc

    nodes = [node for node in root.iter() if _local(node.tag) in ("item", "entry")]
    candidates: list[NewsCandidate] = []
    for node in nodes:
        title = clean_text(_child_text(node, "title"))
        link = _entry_url(node, source)
        if not title or not link:
            continue
        description = _child_text(node, "description", "summary", "encoded", "content")
        published = parse_feed_date(
            _child_text(node, "pubdate", "date", "published", "updated", "pubDate")
        )
        image = _entry_image(node, description)
        candidates.append(
            NewsCandidate(
                title=title,
                url=link,
                source=source,
                published_at=published,
                summary=clean_text(description)[:SUMMARY_MAX],
                body=html_to_text(description)[:BODY_MAX_CHARS],
                image_url=image or None,
            )
        )
    return candidates


def _first_image(html: str) -> str:
    match = _IMG_SRC_RE.search(html or "")
    return match.group(1).strip() if match else ""


def urljoin(base: str, link: str) -> str:
    """把相对链接补成绝对地址（订阅源偶发相对路径）。"""
    return urllib.parse.urljoin(base, link or "")


# ---------- 游民星空 ACG：列表页 + 详情页 ----------

GAMERSKY_LIST_RE = re.compile(
    r'<a[^>]+href="(https?://www\.gamersky\.com/news/\d{6}/(\d+)\.shtml)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
GAMERSKY_PUBLISHED_RE = re.compile(r"发布时间：\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?")
# 文章头部的真实发布时间：<div class="Mid2L_tit"><h1>标题</h1>
#   <div class="detail">2024-04-24 16:21:26 来源：官方 作者：…</div>
# 注意：页面里另有「发布时间：YYYY-MM-DD」（无时刻）出现在**侧栏推荐列表**里，
# 照抄会把文章写成「抓取当天」，2024/9 月的旧文全变成当天的"最新"——时效全错。
GAMERSKY_HEADER_TIME_RE = re.compile(
    r'class="Mid2L_tit".{0,4000}?(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(?::(\d{2}))?',
    re.IGNORECASE | re.DOTALL,
)
GAMERSKY_SUMMARY_RE = re.compile(
    r'<meta[^>]+name="description"[^>]+content="([^"]*)"', re.IGNORECASE
)
# 列表锚点里的真标题：游民自己放在 <div class="tit"> 里。投票数 / 评论数在
# <div class="tem"> 里，clean_text 整体折叠会把「标题 4354 0」这种尾巴混进标题
# （线上实测：武侠动画《杯雪》PV 一文标题带上了浏览/评论数）。
GAMERSKY_TIT_RE = re.compile(r'<div class="tit[^"]*">(.*?)</div>', re.S)
GAMERSKY_COUNT_TAIL_RE = re.compile(r"\s+\d+\s+\d+$")

GAMERSKY_BODY_START = 'class="Mid2L_con">'
GAMERSKY_BODY_STOPS = (
    'class="liPage"',
    'class="Mid2L_page"',
    "id=\"pe100_page_contentpage\"",
    'class="page_c"',
)


def _gamersky_title(inner: str) -> str:
    """取列表锚点的真标题：优先游民自带的 ``<div class="tit">`` 字段。

    tit 缺失时退回整体折叠文本，并剥掉尾部的「浏览数 评论数」计数尾巴
    （仅当确实呈现 ``数字 数字`` 形态才剥，避免误删以数字结尾的正常标题）。
    """
    match = GAMERSKY_TIT_RE.search(inner or "")
    if match:
        return clean_text(match.group(1))
    title = clean_text(inner)
    return GAMERSKY_COUNT_TAIL_RE.sub("", title)


def parse_gamersky_list(html: str, source: SourceSpec, *, limit: int = 20) -> list[NewsCandidate]:
    """解析游民星空 ACG 首页的资讯链接，按文章 id 倒序（约等于最新在前）。"""
    seen: set[str] = set()
    rows: list[tuple[int, str, str]] = []
    for match in GAMERSKY_LIST_RE.finditer(html or ""):
        url, article_id, inner = match.group(1), match.group(2), match.group(3)
        title = _gamersky_title(inner)
        if not title or len(title) < 6 or url in seen:
            continue
        seen.add(url)
        rows.append((int(article_id), title, url))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [
        NewsCandidate(title=title, url=url, source=source, needs_detail=True)
        for _id, title, url in rows[:limit]
    ]


def _gamersky_published_at(text: str) -> datetime | None:
    """取游民详情页的文章发布时间；拿不到就返回 None（由调用方决定兜底）。

    两层：先取文章头部 ``class="Mid2L_tit"`` 里的 ``YYYY-MM-DD HH:MM:SS``（真值），
    再用「发布时间：」兜底——但兜底**必须带上时刻**才算命中。真实页面的
    「发布时间：」出现在侧栏推荐列表里且只有日期，认它会把 9 月的文章写成抓取当天。
    """
    header = GAMERSKY_HEADER_TIME_RE.search(text or "")
    if header:
        stamp = f"{header.group(1)} {header.group(2)}:{header.group(3) or '00'}"
        try:
            return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    fallback = GAMERSKY_PUBLISHED_RE.search(text or "")
    if fallback and fallback.group(2):
        try:
            return datetime.strptime(
                f"{fallback.group(1)} {fallback.group(2)}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return None
    return None


def parse_gamersky_detail(html: str, candidate: NewsCandidate) -> NewsCandidate:
    """补齐游民详情页的发布时间、摘要与正文（正文截到分页/页脚标记处）。"""
    text = html or ""
    published = _gamersky_published_at(text)
    if published is not None:
        candidate.published_at = published
    summary_match = GAMERSKY_SUMMARY_RE.search(text)
    if summary_match:
        candidate.summary = clean_text(summary_match.group(1))[:SUMMARY_MAX]

    start = text.find(GAMERSKY_BODY_START)
    if start >= 0:
        body_html = text[start + len(GAMERSKY_BODY_START):]
        for stop in GAMERSKY_BODY_STOPS:
            index = body_html.find(stop)
            if index > 0:
                body_html = body_html[:index]
        if not candidate.image_url:
            candidate.image_url = _first_image(body_html) or None
        # 正文按行清洗：游民详情的正文区开头混过统计像素链接（[媒体] https://...）
        # 与 HTML 注释残留（-->），直接 html_to_text 会一并塞进正文。
        candidate.body = (
            _clean_body_text(body_html) or html_to_text(body_html)
        )[:BODY_MAX_CHARS]
    candidate.needs_detail = False
    return candidate


# 详情页头图：og:image / twitter:image（两种属性顺序都要认）。
# 日语 RSS（4Gamer / アニメ！アニメ！）的 feed 里不带图，只能从详情页的社交卡片取。
OG_IMAGE_RES = (
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]*'
        r'content=["\']([^"\']+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*'
        r'(?:property|name)=["\'](?:og:image|twitter:image)["\']',
        re.IGNORECASE,
    ),
)


def parse_og_image(html: str) -> str | None:
    """从详情页取社交卡片头图（og:image / twitter:image）；取不到返回 None。"""
    for pattern in OG_IMAGE_RES:
        match = pattern.search(html or "")
        if match:
            url = match.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url
    return None


def hydrate_head_image(candidate: NewsCandidate, *, fetch) -> NewsCandidate:
    """给没有头图的候选补一张：抓详情页读 og:image；失败静默（不阻断入库）。

    RSS 源（4Gamer / アニメ！アニメ！ 等）的 feed 不带图，不补的话整站资讯列表
    会有一半是空白卡片。这里只抓一次页面、只读 meta，不解析正文。
    """
    if candidate.image_url:
        return candidate
    try:
        html = fetch(candidate.url)
    except Exception:  # noqa: BLE001 - 补图失败不该影响这条资讯入库
        return candidate
    found = parse_og_image(html)
    if found:
        candidate.image_url = found
    return candidate


# ---------- RSS 源正文提取（2026-10-21 引入，2026-10-22 重写） ----------
#
# 根因：RSS 候选 needs_detail=False，body 恒空，入库正文退化为 RSS 摘要
# （animeanime / LN News 的 feed 甚至没有 description，正文=标题）。
#
# 2026-10-22 线上实测各站正文容器（旧版只认 mce-content-body / article，两条都不中）：
# - LN News：div.mce-content-body（TinyMCE 编辑器容器）；
# - ACGCH：**div.article-body**——注意页面里先出现的是「相关文章」卡片
#   （article.tl-card），旧版按出现顺序取第一个 <article> 就拿到卡片，长度不够 → 返回空
#   → 正文退化成摘要（线上 28 条 ACGCH 全是这个症状）；
# - 4Gamer：**div.entry-content**（旧注释说「JS 渲染无正文」是误判，静态 HTML 里有）；
# - animeanime：没有专属 class，退到整篇 <article>。
#
# 两个实现要点：
# 1. **按标签深度切片**：非贪婪正则会在第一个嵌套 </div> 处截断（4Gamer 正文 818 字只能
#    取到 351 字），所以用 `_slice_element` 数同名标签的开合；
# 2. **取文本最长的候选**，而不是「第一个命中的容器」——同页混杂推荐卡片/侧栏时，
#    按顺序取必然拿到最短的那块。
_BODY_CONTAINER_KEYS: tuple[str, ...] = (
    "mce-content-body", "article-body", "entry-content", "maintxt",
    "post-content", "entry-body", "article-content",
)
_BODY_MARKER_RE = re.compile(
    r'<(div|article|section)\b[^>]*(?:class|id)="[^"]*(?:'
    + "|".join(_BODY_CONTAINER_KEYS)
    + r')[^"]*"[^>]*>',
    re.IGNORECASE,
)
_HTML_NOISE_RES: tuple[re.Pattern, ...] = (
    re.compile(r"(?is)<!--.*?-->"),          # 注释残留（游民星空正文里混过 -->）
    re.compile(r"(?is)<script\b.*?</script>"),
    re.compile(r"(?is)<style\b.*?</style>"),
    re.compile(r"(?is)<noscript\b.*?</noscript>"),
    re.compile(r"(?is)<iframe\b.*?</iframe>"),
)
_TAG_STEP_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)\b[^>]*?(/?)>")
_URL_ONLY_RE = re.compile(r"^https?://\S+$")
# 噪音行：短且没有句末标点的行多为导航 / 标签 / 关键词栏（4Gamer 页骨架曾混入
# 「週間記事ランキング」「キーワード」这类行）。
_BODY_LINE_NOISE_MAX = 12
# ACGCH 文章容器自带的站点噪音（阅读时长元信息 / 投稿 CTA），实测 2026-10-21。
_BODY_NOISE_WORDS: tuple[str, ...] = ("分钟读完", "来投个稿")
_BODY_MIN_CHARS = 60


def strip_html_noise(html: str) -> str:
    """去掉 script / style / 注释 / iframe——它们会混进正文（游民星空的统计像素链接）。"""
    text = html or ""
    for pattern in _HTML_NOISE_RES:
        text = pattern.sub(" ", text)
    return text


def _slice_element(html: str, start: int, tag: str) -> str:
    """从 start 起切出同名标签的完整内容（按嵌套深度配对）。

    非贪婪正则 `(.*?)</div>` 在第一个嵌套 div 结束处就停了，长正文会被腰斩。
    """
    depth = 1
    for match in _TAG_STEP_RE.finditer(html, start):
        if match.group(2).lower() != tag:
            continue
        if match.group(1):  # 结束标签
            depth -= 1
            if depth == 0:
                return html[start : match.start()]
        elif not match.group(3):  # 自闭合标签不增加深度
            depth += 1
    return html[start:]  # 没找到闭合：退化为到文末，再由长度校验兜底


def _clean_body_text(html_fragment: str) -> str:
    """正文片段 → 纯文本行（去导航噪音 / 纯 URL / 图片占位残留）。"""
    lines: list[str] = []
    for line in html_to_text(html_fragment).splitlines():
        line = line.strip()
        if not line or line == "&nbsp;" or line == "-->":
            continue
        if any(word in line for word in _BODY_NOISE_WORDS):
            continue
        if _URL_ONLY_RE.match(line) or line.startswith("[媒体]"):
            continue  # 统计像素 / 广告占位（游民星空正文开头混过一条）
        if line.count("#") >= 2:
            continue  # 尾部标签行（ACGCH："# TOHO animation# PV# 光荣特库摩# 日向夏"）
        if len(line) < _BODY_LINE_NOISE_MAX and not re.search(r"[。！？…」』〕]", line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_body(html: str) -> str:
    """详情页 HTML → 正文纯文本（复用 html_to_text：图片转 [图] 行、段落换行）。

    先清理 script/style/注释，再按 class/id 关键词找已知正文容器；每个候选按标签深度
    切出完整内容、转文本，**取文本最长的一个**（同页往往先出现「相关文章」卡片）。
    最后兜底整篇 <article>（animeanime 这类没有专属 class 的站）。

    找不到容器 / 提取结果过短时返回空串——由调用方保留 RSS 摘要，
    绝不把短摘要扩写成「正文」。
    """
    if not html:
        return ""
    cleaned = strip_html_noise(html)
    best = ""
    for match in _BODY_MARKER_RE.finditer(cleaned):
        content = _slice_element(cleaned, match.end(), match.group(1).lower())
        text = _clean_body_text(content)
        if len(text) > len(best):
            best = text
    for match in re.finditer(r"<article\b[^>]*>", cleaned, re.IGNORECASE):
        text = _clean_body_text(_slice_element(cleaned, match.end(), "article"))
        if len(text) > len(best):
            best = text
    return best[:BODY_MAX_CHARS] if len(best) >= _BODY_MIN_CHARS else ""


def fetch_candidates(
    source: SourceSpec,
    *,
    fetch,
    limit: int = 20,
) -> list[NewsCandidate]:
    """按源的 kind 抓取候选条目；失败抛 NewsSourceError（由调用方记为该源失败）。"""
    if source.kind == "rss":
        text = fetch(source.url)
        return parse_feed(text, source)[:limit]
    if source.kind == "html":
        page = fetch(source.url)
        return parse_gamersky_list(page, source, limit=limit)
    raise NewsSourceError(f"未知的源类型：{source.kind}")


def hydrate(candidate: NewsCandidate, *, fetch) -> NewsCandidate:
    """给 needs_detail 的候选补详情（目前只有游民星空用得到）。"""
    if not candidate.needs_detail:
        return candidate
    if candidate.source.kind != "html":
        candidate.needs_detail = False
        return candidate
    return parse_gamersky_detail(fetch(candidate.url), candidate)
