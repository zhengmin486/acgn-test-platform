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
    SourceSpec("gcores", "机核 GCORES", "https://www.gcores.com/rss", "rss", "综合动态", "zh"),
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


def parse_feed_date(raw: str) -> datetime | None:
    """解析订阅源时间：RFC822（RSS2.0）/ ISO-W3C（RDF 的 dc:date、Atom 的 updated）。

    旧版 Python 的 ``datetime.fromisoformat`` 不接受 ``+09:00`` 这类带冒号时区，
    这里兜底去掉时区后缀再解析（入库只用本地 naive 时间）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed.replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    candidate = text.replace("Z", "+00:00")
    for attempt in (candidate, re.sub(r"[+-]\d{2}:\d{2}$", "", candidate)):
        try:
            parsed = datetime.fromisoformat(attempt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=None)
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
GAMERSKY_SUMMARY_RE = re.compile(
    r'<meta[^>]+name="description"[^>]+content="([^"]*)"', re.IGNORECASE
)
GAMERSKY_BODY_START = 'class="Mid2L_con">'
GAMERSKY_BODY_STOPS = (
    'class="liPage"',
    'class="Mid2L_page"',
    "id=\"pe100_page_contentpage\"",
    'class="page_c"',
)


def parse_gamersky_list(html: str, source: SourceSpec, *, limit: int = 20) -> list[NewsCandidate]:
    """解析游民星空 ACG 首页的资讯链接，按文章 id 倒序（约等于最新在前）。"""
    seen: set[str] = set()
    rows: list[tuple[int, str, str]] = []
    for match in GAMERSKY_LIST_RE.finditer(html or ""):
        url, article_id, inner = match.group(1), match.group(2), match.group(3)
        title = clean_text(inner)
        if not title or len(title) < 6 or url in seen:
            continue
        seen.add(url)
        rows.append((int(article_id), title, url))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [
        NewsCandidate(title=title, url=url, source=source, needs_detail=True)
        for _id, title, url in rows[:limit]
    ]


def parse_gamersky_detail(html: str, candidate: NewsCandidate) -> NewsCandidate:
    """补齐游民详情页的发布时间、摘要与正文（正文截到分页/页脚标记处）。"""
    text = html or ""
    match = GAMERSKY_PUBLISHED_RE.search(text)
    if match:
        stamp = f"{match.group(1)} {match.group(2) or '00:00'}"
        try:
            candidate.published_at = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        except ValueError:
            pass
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
        candidate.body = html_to_text(body_html)[:BODY_MAX_CHARS]
    candidate.needs_detail = False
    return candidate


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
