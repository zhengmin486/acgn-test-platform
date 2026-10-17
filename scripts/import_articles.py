"""ANIPLEX NEWS BOX 资讯抓取与导入。默认预览，--apply 才写入。

数据源：https://news.aniplex.co.jp/ （Aniplex 官方新闻站）

站点结构（2026-10 核对，抓取逻辑依赖以下事实）：
- 栏目页 `/?category=<key>`；本站只收录能拿到「标题 + 日期 + 原文链接」的栏目
  （news / ranking / event，见 CATEGORIES 的说明），站点上的 YouTube、播客栏目不属于资讯文章。
- 页内列表项是 `<article class="*_archive__item ...">`，含 `detail.html?id=<id>`（有详情页的栏目）、
  `<date>Oct 16, 2026</date>` 与 `<h3 ...__ttl>标题</h3>`；活动栏目的条目改用官网原文链接。
- 列表翻页由页内「view more」按钮驱动，按钮带 `data-query="/rest/xxx_list.html?subcategory=&p=N"`；
  该 REST 端点返回同结构的列表片段。event 栏目为单页，无翻页端点。
- 详情页 `detail.html?id=<id>` 提供完整标题（`h2.single__ttl`）、日期、正文
  （`div.single__content`）与摘要（`meta[property=og:description]`）。

设计要点（给后续维护者）：
- 默认 dry-run：只按「站点列表 -> 逐条详情」抓取并汇总，不写数据库；显式 --apply 才入库。
- 预览与正式写入走**同一条抓取路径**，所以预览看到的内容就是确认后写入的内容；
  预览会写一条 sync_task_runs 记录（由 app.sync_tasks 负责），但绝不写业务数据。
- 幂等与「不覆盖本地数据」：original_url 命中库中已有记录即跳过（省掉该条的详情请求）；
  标题命中同样跳过。任何情况下都不 UPDATE 已存在的 article 行。
- 只取正文抓取成功的条目入库：详情页抓不到就不写半条数据，该条计为 failed 并在明细里说明。
- 正式导入后自动「识别标签 + 关联作品」（app/article_tags）：拿库中作品标签与
  作品标题当词表，在新文章的标题/摘要/正文里做子串匹配，替换式写入
  article_tags / article_content_links；预览模式不做识别（不写库）。
  存量资讯用 scripts/link_articles.py 回填。
- 只用 Python 标准库（urllib + concurrent.futures），不新增第三方依赖。
- 抓取失败（网络/站点结构变化）抛 ArticleFetchError，由调用方回显原因，不影响其它功能。

用法示例：
    # 预览最新资讯（默认，不写库）
    python scripts/import_articles.py
    # 正式入库
    python scripts/import_articles.py --apply
    # 只处理「活动资讯」栏目，最多 5 条
    python scripts/import_articles.py --category 活动资讯 --limit 5

核心函数（fetch_page / parse_list_page / parse_detail_page / run_import_articles）
同时服务 CLI 与管理端「数据管理 → 数据同步」页面，见 tests/test_article_import.py。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path

from sqlalchemy import select

# Allow execution as `python scripts\import_articles.py` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import article_tags
from app.config import settings
from app.database import SessionLocal
from app.models import Article

# ---------- 站点常量 ----------

SOURCE_NAME = "ANIPLEX NEWS BOX"
SITE_BASE = "https://news.aniplex.co.jp"
SITE_LABEL = "news.aniplex.co.jp"

DEFAULT_USER_AGENT = (
    "acgn-test-platform/0.1 (local import script; news.aniplex.co.jp reader)"
)
DEFAULT_TIMEOUT = 15.0  # 单次请求超时（秒）
DEFAULT_RETRIES = 2  # 失败后的重试次数（总请求次数 = 1 + retries）
RETRY_BACKOFF = 0.8  # 重试间隔基数（秒），第 n 次重试等待 n * 基数

DEFAULT_LIMIT = 12  # 未指定条数时的默认抓取量（约等于一个列表页）
MAX_LIST_PAGES = 5  # 列表最多翻页数，避免「不限条数」时无边界抓取
MAX_SUMMARY_CHARS = 140  # summary 从正文截断时的长度
DETAIL_WORKERS = 4  # 详情页并发抓取线程数（纯网络 I/O，与 SQLite 写入无关）


@dataclass(frozen=True)
class NewsCategory:
    """站点栏目：key 用于请求，label 是本站使用的分类名。"""

    key: str
    label: str
    list_path: str
    next_page_path: str  # 该栏目的翻页 REST 端点；空表示单页栏目
    archive_item: str  # 列表项 class 中的标识（仅用于提示，解析不依赖它）


# 只收录「站内能拿到标题 + 日期 + 原文链接」的栏目：
# - news / ranking 的条目有站内详情页（detail.html?id=...），能抓到完整正文；
# - event 的条目没有站内详情页，但每条都有官网原文链接，正文以「标题 + 原文链接」入库；
# - movie（YouTube 视频）与 radio（播客）在站内只有播放器弹窗、没有文章页，
#   不属于「资讯文章」，因此不作为可选分类，避免写入只有标题没有内容的空壳记录。
CATEGORIES: tuple[NewsCategory, ...] = (
    NewsCategory("news", "动漫资讯", "/?category=news", "/rest/news_list.html", "news_archive__item"),
    NewsCategory("ranking", "榜单资讯", "/?category=ranking", "/rest/news_list_ranking.html", "news_archive__item"),
    NewsCategory("event", "活动资讯", "/?category=event", "", "event_archive__item"),
)

DEFAULT_CATEGORY = CATEGORIES[0]


class ArticleFetchError(RuntimeError):
    """资讯站点不可用或返回内容无法解析。"""


class UnknownArticleCategoryError(ValueError):
    """分类名不在站点栏目表内。"""


def list_categories() -> list[str]:
    """本站可选的资讯分类（= 站点栏目，按站点顺序）。"""
    return [category.label for category in CATEGORIES]


def category_by_label(label: str | None) -> NewsCategory:
    """按分类名取栏目；label 为空取默认栏目，未知分类抛错。"""
    text = (label or "").strip()
    if not text:
        return DEFAULT_CATEGORY
    for category in CATEGORIES:
        if category.label == text or category.key == text:
            return category
    raise UnknownArticleCategoryError(f"未知的资讯分类：{text}")


# ---------- HTML 解析（纯函数，便于离线测试） ----------

_ITEM_RE = re.compile(
    r'<article[^>]*class="(?P<cls>[^"]*_archive__item[^"]*)"[^>]*>', re.IGNORECASE
)
_ITEM_ID_RE = re.compile(r'detail\.html\?id=(\d+)')
_ITEM_DATE_RE = re.compile(r"<date[^>]*>([^<]+)</date>", re.IGNORECASE)
_ITEM_TITLE_RE = re.compile(
    r'<h3[^>]*class="[^"]*__ttl[^"]*"[^>]*>([\s\S]*?)</h3>', re.IGNORECASE
)
_NEXT_QUERY_RE = re.compile(r'data-query="([^"]+)"')
_NEXT_PAGE_RE = re.compile(r'data-nextpage="(\d+)"')
_EXTERNAL_LINK_RE = re.compile(r'href="(https?://[^"]+)"', re.IGNORECASE)
_ORIGIN_TITLE_RE = re.compile(
    r'<h2[^>]*class="[^"]*__origin[^"]*"[^>]*>([\s\S]*?)</h2>', re.IGNORECASE
)

_DETAIL_TITLE_RE = re.compile(
    r'<h2[^>]*class="[^"]*single__ttl[^"]*"[^>]*>([\s\S]*?)</h2>', re.IGNORECASE
)
_DETAIL_CONTENT_RE = re.compile(
    r'<div[^>]*class="[^"]*single__content[^"]*"[^>]*>([\s\S]*?)</div>\s*(?:<button|<section|</article)',
    re.IGNORECASE,
)
_DETAIL_DATE_RE = re.compile(
    r'<time[^>]*class="[^"]*single__date[^"]*"[^>]*>([^<]+)</time>', re.IGNORECASE
)
_META_DESCRIPTION_RE = re.compile(
    r'<meta[^>]*property="og:description"[^>]*content="([^"]*)"', re.IGNORECASE
)
_META_DESCRIPTION_NAME_RE = re.compile(
    r'<meta[^>]*name="description"[^>]*content="([^"]*)"', re.IGNORECASE
)
_IFRAME_SRC_RE = re.compile(r'<iframe\b[^>]*?src="([^"]+)"[^>]*>', re.IGNORECASE)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def clean_text(raw: str) -> str:
    """把 HTML 片段折叠成单行纯文本（去标签之外再压缩空白）。"""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def parse_site_date(raw: str) -> datetime | None:
    """解析站点日期（列表页为 `Oct 16, 2026`），失败返回 None。"""
    match = re.match(r"\s*([A-Za-z]{3})\w*\s+(\d{1,2}),\s*(\d{4})", raw or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).title())
    if month is None:
        return None
    return datetime(int(match.group(3)), month, int(match.group(2)))


@dataclass
class NewsEntry:
    """列表页解析出的一条资讯（详情抓取前的元信息）。

    detail_id：站内详情页 id；条目没有站内详情页时等于 url（仅作去重键使用）。
    has_detail：是否存在站内详情页（False 表示只能拿到标题、日期与原文链接）。
    """

    detail_id: str
    url: str
    title: str
    published_at: datetime
    ip_name: str = ""
    summary: str = ""
    body: str = ""
    has_detail: bool = True


def split_ip_title(raw: str) -> tuple[str, str]:
    """拆分列表标题的「作品名／ 标题」前缀，返回 (作品名, 标题)。"""
    head, sep, tail = raw.partition("／")
    if sep and tail.strip():
        return head.strip(), tail.strip()
    return "", raw.strip()


def parse_list_page(html: str) -> list[NewsEntry]:
    """解析一个栏目页或列表片段，按出现顺序返回条目（同一条只保留首次）。"""
    matches = [m for m in _ITEM_RE.finditer(html) if "relation_archive__item" not in m.group("cls")]
    entries: list[NewsEntry] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        block = html[match.end():end]
        title_match = _ITEM_TITLE_RE.search(block)
        if not title_match:
            continue  # 结构不符合预期（如广告位）时跳过该块，不影响其它条目
        ip_name, title = split_ip_title(clean_text(title_match.group(1)))
        if not title:
            continue

        id_match = _ITEM_ID_RE.search(block)
        if id_match:
            key = id_match.group(1)
            url = f"{SITE_BASE}/detail.html?id={key}"
            has_detail = True
        else:
            # 没有站内详情页的条目（如活动栏目）：用官网原文链接作为来源地址。
            link_match = next(
                (
                    m for m in _EXTERNAL_LINK_RE.finditer(block)
                    if SITE_LABEL not in m.group(1)
                ),
                None,
            )
            if not link_match:
                continue
            key = url = unescape(link_match.group(1))
            has_detail = False

        if key in seen:
            continue
        seen.add(key)
        date_match = _ITEM_DATE_RE.search(block)
        published_at = parse_site_date(date_match.group(1) if date_match else "")
        origin_match = _ORIGIN_TITLE_RE.search(block)
        entries.append(
            NewsEntry(
                detail_id=key,
                url=url,
                title=title,
                published_at=published_at or datetime.now(),
                ip_name=ip_name or (clean_text(origin_match.group(1)) if origin_match else ""),
                has_detail=has_detail,
            )
        )
    return entries


def next_page_url(html: str, category: NewsCategory, current_page: int) -> str | None:
    """从列表页取下一页地址；没有「view more」按钮时返回 None。"""
    if not _NEXT_PAGE_RE.search(html):
        return None
    query_match = _NEXT_QUERY_RE.search(html)
    if query_match:
        query = unescape(query_match.group(1)).strip()
        if query.startswith("http"):
            return query
        if query.startswith("/"):
            return f"{SITE_BASE}{query}"
    if not category.next_page_path:
        return None
    return f"{SITE_BASE}{category.next_page_path}?subcategory=&p={current_page + 1}"


def parse_detail_page(html: str, entry: NewsEntry) -> NewsEntry:
    """用详情页补全条目：完整标题、摘要与正文（就地返回新对象）。"""
    title_match = _DETAIL_TITLE_RE.search(html)
    title = clean_text(title_match.group(1)) if title_match else ""
    if not title:
        title = entry.title

    date_match = _DETAIL_DATE_RE.search(html)
    published_at = parse_site_date(date_match.group(1) if date_match else "") or entry.published_at

    body = ""
    content_match = _DETAIL_CONTENT_RE.search(html)
    if content_match:
        body = html_to_text(content_match.group(1))

    if not body:
        body = f"{title}\n\n原文：{entry.url}"

    summary = ""
    for pattern in (_META_DESCRIPTION_RE, _META_DESCRIPTION_NAME_RE):
        meta_match = pattern.search(html)
        if meta_match:
            summary = clean_text(meta_match.group(1))
            if summary:
                break
    if not summary or summary == title:
        summary = _first_meaningful_line(body, title)[:MAX_SUMMARY_CHARS]

    return NewsEntry(
        detail_id=entry.detail_id,
        url=entry.url,
        title=title,
        published_at=published_at,
        ip_name=entry.ip_name,
        summary=summary,
        body=body,
    )


def _first_meaningful_line(body: str, title: str) -> str:
    """取正文中第一行与标题不同的内容；正文只有标题/媒体时回落标题。"""
    for line in (body or "").split("\n"):
        text = re.sub(r"\s+", " ", line).strip()
        if not text or text == title or text.startswith("[媒体]"):
            continue
        return text
    return title


def html_to_text(raw: str) -> str:
    """详情正文 HTML -> 纯文本：保留段落换行，内嵌视频/音频转为链接行。"""
    text = raw or ""
    text = _IFRAME_SRC_RE.sub(lambda m: f"\n[媒体] {unescape(m.group(1))}\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


# ---------- 网络层 ----------


def fetch_page(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> str:
    """获取页面 HTML（标准库 urllib）；网络错误重试，仍失败抛 ArticleFetchError。"""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, "replace")
        except urllib.error.HTTPError as exc:
            # 4xx 是确定性错误，重试没有意义（429 除外）。
            if 400 <= exc.code < 500 and exc.code != 429:
                raise ArticleFetchError(
                    f"请求 {url} 返回 HTTP {exc.code} {exc.reason}"
                ) from exc
            last_error = exc
        except Exception as exc:  # 网络抖动、超时、TLS 握手失败等
            last_error = exc
        if attempt < retries:
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise ArticleFetchError(f"请求 {url} 失败：{last_error}")


def collect_news(
    category: NewsCategory,
    *,
    limit: int | None = None,
    fetch=None,
) -> list[NewsEntry]:
    """抓取栏目列表（必要时翻页）直到够 limit 条或没有下一页。"""
    fetch = fetch or fetch_page
    target = limit or DEFAULT_LIMIT
    entries: list[NewsEntry] = []
    seen: set[str] = set()
    url = f"{SITE_BASE}{category.list_path}"
    for page in range(1, MAX_LIST_PAGES + 1):
        html = fetch(url)
        for entry in parse_list_page(html):
            if entry.detail_id in seen:
                continue
            seen.add(entry.detail_id)
            entries.append(entry)
            if len(entries) >= target:
                return entries
        following = next_page_url(html, category, page)
        if not following:
            break
        url = following
    return entries


def fetch_details(entries: list[NewsEntry], *, fetch=None) -> dict[str, NewsEntry | None]:
    """并发抓取详情页；返回 detail_id -> 补全后的条目（失败为 None）。"""
    fetch = fetch or fetch_page
    results: dict[str, NewsEntry | None] = {}
    if not entries:
        return results
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(fetch, entry.url): entry for entry in entries}
        for future, entry in futures.items():
            try:
                results[entry.detail_id] = parse_detail_page(future.result(), entry)
            except Exception:
                results[entry.detail_id] = None
    return results


# ---------- 可复用的筛选与导入流程（CLI 与管理端数据同步共用） ----------


@dataclass
class ArticleOutcome:
    """单条资讯的处理结果，用于输出跳过或失败原因。"""

    title: str
    status: str  # added / pending / existing / failed
    reason: str = ""
    url: str = ""


@dataclass
class ArticleImportSummary:
    """一次资讯导入的汇总统计与明细。"""

    mode: str  # preview / apply
    requested: int
    category: str = ""
    existing: int = 0
    pending: int = 0
    added: int = 0
    failed: int = 0
    recognized: int = 0  # apply 模式下实际执行「识别标签 + 关联作品」的文章数
    outcomes: list[ArticleOutcome] = field(default_factory=list)

    def describe(self) -> str:
        """汇总信息的多行文本。"""
        mode_label = "正式导入（--apply）" if self.mode == "apply" else "预览（dry-run）"
        lines = [
            f"数据源：{SOURCE_NAME}（{SITE_LABEL}）",
            f"栏目：{self.category or '全部'}",
            f"模式：{mode_label}",
            "导入汇总：",
            f"  抓取条目：{self.requested}",
            f"  已存在跳过：{self.existing}",
            f"  待新增：{self.pending}",
            f"  实际新增：{self.added}",
            f"  抓取失败：{self.failed}",
        ]
        if self.mode == "apply":
            lines.append(f"  识别标签/关联作品：{self.recognized} 篇")
        return "\n".join(lines)


def run_import_articles(
    session_factory,
    *,
    apply: bool = False,
    category: str | None = None,
    limit: int | None = None,
    fetch=None,
) -> ArticleImportSummary:
    """抓取并导入资讯：采集列表 -> 去重 -> 抓详情 -> （apply 时）写入。

    session_factory：产生 SQLAlchemy Session 的工厂（如 SessionLocal），本函数负责开闭会话。
    fetch：可注入的页面抓取函数 `fetch(url) -> html`，默认走真实网络；测试用它打桩。
    去重规则：original_url 或 title 命中库中已有记录即跳过，绝不覆盖、不更新已存在的数据。
    只写入正文抓取成功的条目；详情页抓取失败的条目不写库，计为 failed 并在明细里说明。
    """
    channel = category_by_label(category)
    summary = ArticleImportSummary(
        mode="apply" if apply else "preview",
        category=channel.label,
        requested=0,
    )

    entries = collect_news(channel, limit=limit, fetch=fetch)
    summary.requested = len(entries)

    db = session_factory()
    try:
        existing_urls = _existing_values(db, Article.original_url, [e.url for e in entries])
        fresh = [entry for entry in entries if entry.url not in existing_urls]
        for entry in entries:
            if entry.url in existing_urls:
                summary.existing += 1
                summary.outcomes.append(
                    ArticleOutcome(entry.title, "existing", "库中已有同 URL 记录，跳过不覆盖", entry.url)
                )

        details = fetch_details([entry for entry in fresh if entry.has_detail], fetch=fetch)

        prepared: list[NewsEntry] = []
        for entry in fresh:
            item = _materialize(entry, details)
            if item is None:
                summary.failed += 1
                summary.outcomes.append(
                    ArticleOutcome(entry.title, "failed", "详情页抓取失败，未写入", entry.url)
                )
                continue
            prepared.append(item)

        existing_titles = _existing_values(db, Article.title, [item.title for item in prepared])
        added: list[tuple[Article, str]] = []  # (新文章, 列表页的作品名前缀)
        for item in prepared:
            if item.title in existing_titles:
                summary.existing += 1
                summary.outcomes.append(
                    ArticleOutcome(item.title, "existing", "库中已有同标题记录，跳过不覆盖", item.url)
                )
                continue
            summary.pending += 1
            if not apply:
                summary.outcomes.append(
                    ArticleOutcome(item.title, "pending", "预览模式：未写入数据库", item.url)
                )
                continue
            article = Article(
                title=item.title,
                category=channel.label,
                summary=item.summary,
                body=item.body,
                source=SOURCE_NAME,
                original_url=item.url,
                is_published=True,
                published_at=item.published_at,
            )
            db.add(article)
            added.append((article, item.ip_name))
            summary.added += 1
            summary.outcomes.append(ArticleOutcome(item.title, "added", "已写入", item.url))

        if apply and added:
            db.flush()  # 先分配文章 id，供识别关联行引用
            article_tags.link_articles(
                db,
                [article for article, _ in added],
                extra_names_map={
                    article.id: [ip_name] if ip_name else []
                    for article, ip_name in added
                },
            )
            summary.recognized = len(added)
            db.commit()
    finally:
        db.close()

    return summary


def _materialize(entry: NewsEntry, details: dict) -> NewsEntry | None:
    """把列表条目补全为可写入的记录；站内详情页抓取失败时返回 None。

    - 有站内详情页：用详情页的完整标题、摘要与正文；
    - 没有站内详情页（如活动栏目）：用标题 + 原文链接组成可读的正文。
    """
    if entry.has_detail:
        return details.get(entry.detail_id)
    return NewsEntry(
        detail_id=entry.detail_id,
        url=entry.url,
        title=entry.title,
        published_at=entry.published_at,
        ip_name=entry.ip_name,
        summary=entry.title[:MAX_SUMMARY_CHARS],
        body=f"{entry.title}\n\n原文：{entry.url}",
        has_detail=False,
    )


def _existing_values(db, column, values: list[str]) -> set[str]:
    """查询库中已存在的取值（values 为空时不查库）。"""
    if not values:
        return set()
    return set(db.scalars(select(column).where(column.in_(values))).all())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=f"从 {SOURCE_NAME} 抓取并导入 ACGN 资讯；默认预览，--apply 才写入。"
    )
    parser.add_argument("--apply", action="store_true", help="正式写入数据库")
    parser.add_argument(
        "--category",
        default=None,
        choices=list_categories(),
        help="只抓取指定栏目（默认抓「动漫资讯」栏目的最新资讯）",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=f"最多抓取条数，缺省 {DEFAULT_LIMIT} 条（每条都会请求一次详情页）",
    )
    args = parser.parse_args(argv)

    print(f"数据库：{settings.database_url}")
    print(f"数据源：{SOURCE_NAME}（{SITE_BASE}）")
    try:
        summary = run_import_articles(
            SessionLocal, apply=args.apply, category=args.category, limit=args.limit
        )
    except (ArticleFetchError, UnknownArticleCategoryError) as exc:
        print(f"抓取失败：{exc}")
        return 1

    print(summary.describe())
    for i, outcome in enumerate(summary.outcomes, 1):
        if outcome.status == "existing":
            continue
        print(f"{i}. [{outcome.status}] {outcome.title}")
        print(f"   {outcome.url}")
        if outcome.reason:
            print(f"   {outcome.reason}")
    if not args.apply:
        print("模式：dry-run，未写入数据库。确认后追加 --apply 正式导入。")
        return 0
    print(f"正式导入完成：新增 {summary.added} 条，跳过已存在 {summary.existing} 条，失败 {summary.failed} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
