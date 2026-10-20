"""ANIPLEX NEWS BOX 资讯抓取与导入的离线测试。

覆盖：
- 解析器：列表页 / 翻页按钮 / 详情页 / 正文转文本 / 日期与标题拆分（纯函数，喂静态片段）；
- 采集：条数上限、按页翻页、终页停止；
- 导入：预览不写库、确认写入、URL 与标题双重去重且不覆盖本地数据、
  详情页抓取失败只影响该条、无详情页栏目用原文链接兜底、未知分类直接报错；
- 网络层：4xx 不重试、可重试错误重试后成功（打桩 urlopen，不访问真实网络）。

全部用例都注入假 fetch 或打桩 urlopen，绝不访问真实网络，也不触碰开发库。
"""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Article
from article_helpers import detail_page, external_item, list_item, list_page, make_site
from scripts import import_articles as ia

SITE = ia.SITE_BASE
NEWS_URL = f"{SITE}/?category=news"
EVENT_URL = f"{SITE}/?category=event"


@pytest.fixture
def session_factory(tmp_path):
    """独立临时库（不碰开发库 local_dev.db）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'articles.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _count(factory, model=Article) -> int:
    db = factory()
    try:
        return db.scalar(select(func.count()).select_from(model))
    finally:
        db.close()


def _articles(factory) -> list[Article]:
    db = factory()
    try:
        return list(db.scalars(select(Article).order_by(Article.id)))
    finally:
        db.close()


def _news_site(*, detail_ids=(101, 102), detail_fails=()):
    """一个 news 栏目假站点：列表页含指定 id，详情页可指定失败。"""
    items = [list_item(i, "Oct 16, 2026", f"资讯标题 {i}") for i in detail_ids]
    details = {
        i: detail_page(f"资讯标题 {i}", "Oct 16, 2026", f"<p>第 {i} 条正文</p><br/><div><br/></div>")
        for i in detail_ids
        if i not in detail_fails
    }
    return make_site({NEWS_URL: list_page(items)}, details)


# ---------- 解析器 ----------


def test_parse_list_page_reads_items_and_splits_ip():
    """列表页解析：取出 id / 日期 / 标题，并拆出「作品名／标题」前缀。"""
    html = list_page([
        list_item(71493, "Oct 16, 2026", "THE LAST PV解禁！", ip="BLEACH 千年血戦篇"),
        list_item(71491, "Oct 15, 2026", "第3話 WEB予告公開！"),
    ])
    entries = ia.parse_list_page(html)
    assert [e.detail_id for e in entries] == ["71493", "71491"]
    assert entries[0].title == "THE LAST PV解禁！"
    assert entries[0].ip_name == "BLEACH 千年血戦篇"
    assert entries[0].published_at.strftime("%Y-%m-%d") == "2026-10-16"
    assert entries[0].url == f"{SITE}/detail.html?id=71493"
    assert entries[0].has_detail is True
    assert entries[1].ip_name == ""


def test_parse_list_page_dedupes_and_skips_related_block():
    """同一条重复出现只保留首次；「関連記事」区块不会被当成列表项。"""
    html = list_page([list_item(71493, "Oct 16, 2026", "标题 A")]) + (
        '<section class="relation_archive">'
        '<article class="relation_archive__item c-thumb_hover">'
        '<a href="/detail.html?id=70000"><date>Oct 1, 2026</date>'
        '<h3 class="relation_archive__item__ttl">関連記事标题</h3></a>'
        "</article></section>"
    )
    entries = ia.parse_list_page(html)
    assert [e.detail_id for e in entries] == ["71493"]


def test_parse_list_page_reads_external_item():
    """没有站内详情页的条目：用官网原文链接作为来源地址。"""
    html = list_page([external_item("https://ip.example/news/?id=1", "Oct 25, 2026",
                                    "Blu-ray 发售纪念活动", origin="ガンバレ！中村くん！！")],
                     next_query="")
    entries = ia.parse_list_page(html)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.has_detail is False
    assert entry.url == "https://ip.example/news/?id=1"
    assert entry.title == "Blu-ray 发售纪念活动"
    assert entry.ip_name == "ガンバレ！中村くん！！"


def test_next_page_url_prefers_site_query_and_stops_without_button():
    """翻页地址优先用页内 data-query；没有「view more」按钮时返回 None。"""
    html = list_page([list_item(1, "Oct 16, 2026", "标题")],
                     next_query="/rest/news_list.html?subcategory=&p=2")
    assert ia.next_page_url(html, ia.DEFAULT_CATEGORY, 1) == (
        f"{SITE}/rest/news_list.html?subcategory=&p=2"
    )
    assert ia.next_page_url(list_page([list_item(1, "Oct 16, 2026", "标题")]),
                            ia.DEFAULT_CATEGORY, 1) is None


def test_parse_detail_page_fills_title_summary_body():
    """详情页：完整标题、日期、摘要与正文（内嵌视频转为媒体链接行）。"""
    entry = ia.parse_list_page(list_page([list_item(71493, "Oct 16, 2026", "列表标题被截断…")]))[0]
    html = detail_page(
        "完整标题（列表页看不到）",
        "Oct 16, 2026",
        '开场说明。<br/><iframe src="https://www.youtube.com/embed/abc" width="560" height="315"></iframe>',
        description="官方给出的摘要",
    )
    item = ia.parse_detail_page(html, entry)
    assert item.title == "完整标题（列表页看不到）"
    assert item.summary == "官方给出的摘要"
    assert item.body == "开场说明。\n[媒体] https://www.youtube.com/embed/abc"
    assert item.published_at.strftime("%Y-%m-%d") == "2026-10-16"


def test_parse_detail_page_falls_back_to_title():
    """摘要与标题相同时（站点常态）回落为标题，不产生空摘要。"""
    entry = ia.parse_list_page(list_page([list_item(71493, "Oct 16, 2026", "标题")]))[0]
    html = detail_page("标题", "Oct 16, 2026", "<p>标题</p>")
    item = ia.parse_detail_page(html, entry)
    assert item.summary == "标题"
    assert item.body == "标题"


def test_parse_site_date_and_split_ip_title():
    """日期与标题拆分的边界：无斜杠、斜杠后为空、无法识别的日期。"""
    assert ia.parse_site_date("Sep 28, 2026").strftime("%Y-%m-%d") == "2026-09-28"
    assert ia.parse_site_date("2026-09-28") is None
    assert ia.split_ip_title("作品名／ 标题") == ("作品名", "标题")
    assert ia.split_ip_title("只有标题") == ("", "只有标题")
    assert ia.split_ip_title("作品名／") == ("", "作品名／")


def test_html_to_text_keeps_paragraphs():
    """正文 HTML 转文本：段落换行保留，空行折叠。"""
    text = ia.html_to_text("<p>第一段</p><p>第二段</p><div><br/></div>")
    assert text == "第一段\n第二段"


# ---------- 采集 ----------


def test_collect_news_respects_limit_and_pages():
    """采集：按上限截断；不够时顺着翻页按钮继续抓。"""
    page1 = list_page(
        [list_item(i, "Oct 16, 2026", f"标题 {i}") for i in (1, 2)],
        next_query="/rest/news_list.html?subcategory=&p=2",
    )
    page2 = list_page([list_item(i, "Oct 15, 2026", f"标题 {i}") for i in (3, 4)])
    fetch = make_site(
        {NEWS_URL: page1, f"{SITE}/rest/news_list.html?subcategory=&p=2": page2},
        {i: detail_page(f"标题 {i}", "Oct 16, 2026", "<p>正文</p>") for i in (1, 2, 3, 4)},
    )
    assert [e.detail_id for e in ia.collect_news(ia.DEFAULT_CATEGORY, limit=1, fetch=fetch)] == ["1"]
    assert [e.detail_id for e in ia.collect_news(ia.DEFAULT_CATEGORY, limit=3, fetch=fetch)] == [
        "1", "2", "3",
    ]
    # 不限条数时用脚本默认值，并跟着翻页一直抓到没有下一页为止。
    assert len(ia.collect_news(ia.DEFAULT_CATEGORY, fetch=fetch)) == 4


# ---------- 导入流程 ----------


def test_preview_then_apply_writes_and_skips_duplicates(session_factory):
    """预览不写库；确认写入新增；重复确认不再新增，本地已有条目不覆盖。"""
    fetch = _news_site()
    preview = ia.run_import_articles(session_factory, fetch=fetch)
    assert (preview.mode, preview.added, preview.pending, preview.existing) == (
        "preview", 0, 2, 0,
    )
    assert _count(session_factory) == 0

    summary = ia.run_import_articles(session_factory, apply=True, fetch=fetch)
    assert (summary.added, summary.existing, summary.failed) == (2, 0, 0)
    rows = _articles(session_factory)
    assert [a.title for a in rows] == ["资讯标题 101", "资讯标题 102"]
    assert rows[0].source == ia.SOURCE_NAME
    # 栏目名「动漫资讯」按新分类口径归一化到主体类型「动画」
    assert rows[0].category == "动画"
    assert rows[0].source_type == "官方PR"
    assert rows[0].original_url == f"{SITE}/detail.html?id=101"
    assert rows[0].is_published is True
    assert rows[0].body == "第 101 条正文"

    again = ia.run_import_articles(session_factory, apply=True, fetch=fetch)
    assert (again.added, again.existing) == (0, 2)
    assert _count(session_factory) == 2


def test_existing_article_is_not_overwritten(session_factory):
    """本地同 URL / 同标题的记录被跳过，内容不被源站覆盖。"""
    db = session_factory()
    db.add(Article(
        title="资讯标题 101",
        category="动漫资讯",
        summary="本地摘要（不可被覆盖）",
        body="本地正文（不可被覆盖）",
        original_url=f"{SITE}/detail.html?id=101",
        is_published=True,
    ))
    db.commit()
    db.close()

    summary = ia.run_import_articles(session_factory, apply=True, fetch=_news_site())
    assert (summary.added, summary.existing) == (1, 1)
    rows = _articles(session_factory)
    kept = next(a for a in rows if a.title == "资讯标题 101")
    assert kept.summary == "本地摘要（不可被覆盖）"
    assert kept.body == "本地正文（不可被覆盖）"


def test_title_duplicate_is_skipped_even_with_other_url(session_factory):
    """同标题但 URL 不同的记录同样跳过（标题去重）。"""
    db = session_factory()
    db.add(Article(
        title="资讯标题 102",
        category="动漫资讯",
        summary="本地摘要",
        body="本地正文",
        original_url=f"{SITE}/detail.html?id=19999",
        is_published=True,
    ))
    db.commit()
    db.close()

    summary = ia.run_import_articles(session_factory, apply=True, fetch=_news_site())
    assert (summary.added, summary.existing) == (1, 1)
    assert [a.title for a in _articles(session_factory)] == ["资讯标题 102", "资讯标题 101"]


def test_import_writes_ip_name_column(session_factory):
    """导入时把列表页「作品名／」前缀落库（回填识别的 extra_names 来源）。"""
    fetch = make_site(
        {NEWS_URL: list_page([
            list_item(101, "Oct 16, 2026", "THE LAST PV解禁！", ip="BLEACH 千年血戦篇"),
        ])},
        {101: detail_page("THE LAST PV解禁！", "Oct 16, 2026", "<p>正文</p>")},
    )
    summary = ia.run_import_articles(session_factory, apply=True, fetch=fetch)
    assert summary.added == 1
    row = _articles(session_factory)[0]
    assert row.ip_name == "BLEACH 千年血戦篇"


def test_import_without_ip_prefix_leaves_null(session_factory):
    """列表标题没有「作品名／」前缀时 ip_name 为空（不写空字符串）。"""
    summary = ia.run_import_articles(session_factory, apply=True, fetch=_news_site())
    assert summary.added == 2
    assert all(article.ip_name is None for article in _articles(session_factory))


def test_reimport_backfills_ip_name_on_existing(session_factory):
    """重复导入不覆盖本地内容，但会给空 ip_name 的存量文章补上前缀。"""
    db = session_factory()
    db.add(Article(
        title="THE LAST PV解禁！",
        category="动漫资讯",
        summary="本地摘要（不可被覆盖）",
        body="本地正文（不可被覆盖）",
        original_url=f"{SITE}/detail.html?id=101",
        is_published=True,
    ))
    db.commit()
    db.close()

    fetch = make_site(
        {NEWS_URL: list_page([
            list_item(101, "Oct 16, 2026", "THE LAST PV解禁！", ip="BLEACH 千年血戦篇"),
        ])},
        {101: detail_page("THE LAST PV解禁！", "Oct 16, 2026", "<p>正文</p>")},
    )
    summary = ia.run_import_articles(session_factory, apply=True, fetch=fetch)
    assert (summary.added, summary.existing) == (0, 1)
    row = _articles(session_factory)[0]
    assert row.ip_name == "BLEACH 千年血戦篇"
    assert row.summary == "本地摘要（不可被覆盖）"
    assert row.body == "本地正文（不可被覆盖）"


def test_reimport_does_not_overwrite_existing_ip_name(session_factory):
    """已有 ip_name 的存量文章不被源站覆盖（只补空列）。"""
    db = session_factory()
    db.add(Article(
        title="THE LAST PV解禁！",
        category="动漫资讯",
        summary="本地摘要",
        body="本地正文",
        ip_name="本地认定的作品",
        original_url=f"{SITE}/detail.html?id=101",
        is_published=True,
    ))
    db.commit()
    db.close()

    fetch = make_site(
        {NEWS_URL: list_page([
            list_item(101, "Oct 16, 2026", "THE LAST PV解禁！", ip="BLEACH 千年血戦篇"),
        ])},
        {101: detail_page("THE LAST PV解禁！", "Oct 16, 2026", "<p>正文</p>")},
    )
    ia.run_import_articles(session_factory, apply=True, fetch=fetch)
    assert _articles(session_factory)[0].ip_name == "本地认定的作品"


def test_detail_failure_marks_failed_and_keeps_others(session_factory):
    """详情页抓取失败的条目计为 failed 且不写库，其余条目照常写入。"""
    summary = ia.run_import_articles(
        session_factory, apply=True, fetch=_news_site(detail_fails=(102,))
    )
    assert (summary.added, summary.failed) == (1, 1)
    assert [a.title for a in _articles(session_factory)] == ["资讯标题 101"]
    failed = [o for o in summary.outcomes if o.status == "failed"]
    assert failed and "详情页抓取失败" in failed[0].reason


def test_category_filter_and_limit(session_factory):
    """分类决定抓哪个栏目；条数上限在抓取时生效。"""
    fetch = _news_site(detail_ids=(101, 102, 103))
    summary = ia.run_import_articles(
        session_factory, apply=True, category="动漫资讯", limit=2, fetch=fetch
    )
    assert summary.requested == 2
    assert summary.added == 2
    assert [a.title for a in _articles(session_factory)] == ["资讯标题 101", "资讯标题 102"]


def test_external_category_uses_original_link(session_factory):
    """无站内详情页的栏目：正文用「标题 + 原文链接」兜底，不请求详情页。"""
    fetch = make_site({EVENT_URL: list_page([
        external_item("https://ip.example/event/1", "Oct 25, 2026", "发售纪念活动")
    ])})
    summary = ia.run_import_articles(
        session_factory, apply=True, category="活动资讯", fetch=fetch
    )
    assert (summary.added, summary.failed) == (1, 0)
    row = _articles(session_factory)[0]
    # 栏目名「活动资讯」归一化到兜底主体类型「综合动态」
    assert row.category == "综合动态"
    assert row.original_url == "https://ip.example/event/1"
    assert row.body == "发售纪念活动\n\n原文：https://ip.example/event/1"


def test_unknown_category_is_rejected(session_factory):
    """未知分类直接报错，不发起任何抓取。"""
    with pytest.raises(ia.UnknownArticleCategoryError):
        ia.run_import_articles(session_factory, category="音乐资讯", fetch=_news_site())


def test_fetch_error_propagates(session_factory):
    """站点不可用时抛出 ArticleFetchError，由调用方回显原因。"""
    def failing_fetch(url):
        raise ia.ArticleFetchError(f"请求 {url} 失败：连接超时")

    with pytest.raises(ia.ArticleFetchError):
        ia.run_import_articles(session_factory, fetch=failing_fetch)
    assert _count(session_factory) == 0


# ---------- 跨栏目放量导入 ----------


def _multi_site():
    """含「动漫资讯 / 榜单资讯」两个栏目的假站点，每栏目 1 条可入库资讯。"""
    ranking_url = f"{SITE}/?category=ranking"
    return make_site(
        {
            NEWS_URL: list_page([list_item(1, "Oct 16, 2026", "资讯 1")]),
            ranking_url: list_page([list_item(2, "Oct 15, 2026", "榜单 1")]),
        },
        {
            1: detail_page("资讯 1", "Oct 16, 2026", "<p>正文一</p>"),
            2: detail_page("榜单 1", "Oct 15, 2026", "<p>正文二</p>"),
        },
    )


def test_run_import_all_categories_stops_at_total(session_factory):
    """跨栏目放量：累计够 total 条就停，不跑多余的栏目。"""
    summaries = ia.run_import_all_categories(
        session_factory, apply=True, total=1, fetch=_multi_site()
    )
    assert [s.category for s in summaries] == ["动漫资讯"]
    assert summaries[0].added == 1
    assert _count(session_factory) == 1


def test_run_import_all_categories_walks_next_category(session_factory):
    """额度没用完时继续跑下一个栏目，直到凑够 total。"""
    summaries = ia.run_import_all_categories(
        session_factory, apply=True, total=2, fetch=_multi_site()
    )
    assert [s.category for s in summaries] == ["动漫资讯", "榜单资讯"]
    assert _count(session_factory) == 2


def test_run_import_all_categories_per_category_cap(session_factory):
    """per_category 优先于 total 余额：每个栏目各自只抓上限条。"""
    summaries = ia.run_import_all_categories(
        session_factory,
        apply=True,
        categories=["动漫资讯", "榜单资讯"],
        per_category=1,
        fetch=_multi_site(),
    )
    assert [s.requested for s in summaries] == [1, 1]
    assert _count(session_factory) == 2


# ---------- 网络层 ----------


class _FakeResponse:
    """足够以假乱真的 urlopen 返回值：只用到 read 与 headers。"""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")
        self.headers = SimpleNamespace(get_content_charset=lambda: "utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_fetch_page_does_not_retry_client_errors(monkeypatch):
    """HTTP 4xx 是确定性错误：立即报错，不浪费时间重试。"""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(ia.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ia.ArticleFetchError) as excinfo:
        ia.fetch_page(NEWS_URL)
    assert "404" in str(excinfo.value)
    assert len(calls) == 1


def test_fetch_page_retries_transient_errors(monkeypatch):
    """网络抖动：重试后成功；重试间隔被替换为空操作，不拖慢测试。"""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.URLError("timed out")
        return _FakeResponse("<html>ok</html>")

    monkeypatch.setattr(ia.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ia.time, "sleep", lambda _seconds: None)
    assert ia.fetch_page(NEWS_URL, retries=2) == "<html>ok</html>"
    assert len(calls) == 3


def test_fetch_page_raises_after_retries(monkeypatch):
    """一直失败：重试耗尽后抛 ArticleFetchError，错误信息里带原因。"""
    monkeypatch.setattr(
        ia.urllib.request, "urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("握手失败")),
    )
    monkeypatch.setattr(ia.time, "sleep", lambda _seconds: None)
    with pytest.raises(ia.ArticleFetchError) as excinfo:
        ia.fetch_page(NEWS_URL, retries=1)
    assert "握手失败" in str(excinfo.value)
