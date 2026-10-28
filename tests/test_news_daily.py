"""多源每日资讯管线测试：RSS/RDF 解析、游民星空列表/详情、每日配额与翻译口径。

原则与既有测试一致：全部打桩（假 fetch / 假翻译），不触网；
数据库用独立临时 SQLite；资讯图片下载在 conftest 里全局关闭（保留外链）。
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import crud, news_llm
from app.config import settings
from app.database import build_engine
from app.models import Article
from scripts import news_daily
from scripts.news_daily import run_import_daily
from scripts.news_sources import (
    SOURCES,
    SourceSpec,
    extract_body,
    fetch_candidates,
    hydrate_head_image,
    parse_feed,
    parse_feed_date,
    parse_gamersky_detail,
    parse_gamersky_list,
    parse_og_image,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ZH_SOURCE = SourceSpec("zh-test", "测试中文源", "https://zh.example/rss", "rss", "动画", "zh")
JA_SOURCE = SourceSpec("ja-test", "测试日文源", "https://ja.example/rss", "rss", "动画", "ja")
# 题材优先级测试用：同语言、不同栏目（动画 vs 游戏）
ANIME_SRC = SourceSpec("zh-anime", "测试动画源", "https://zh-anime.example/rss", "rss", "动画", "zh")
GAME_SRC = SourceSpec("zh-game", "测试游戏源", "https://zh-game.example/rss", "rss", "游戏", "zh")

RSS_ANIME_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>动画源</title>
<item><title>动画新闻A</title><link>https://zh-anime.example/a1</link><description>动画相关。</description></item>
</channel></rss>"""

RSS_GAME_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>游戏源</title>
<item><title>游戏新闻B</title><link>https://zh-game.example/g1</link><description>游戏相关。</description></item>
</channel></rss>"""

RSS_ZH = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>测试</title>
<item><title>新动画宣布制作</title><link>https://zh.example/a1</link>
<pubDate>Mon, 19 Oct 2026 23:00:00 +0800</pubDate>
<description><![CDATA[<p>制作决定，明年开播。</p><img src="https://zh.example/img1.jpg"><p>续报随后。</p>]]></description>
</item>
<item><title>旧闻一条</title><link>https://zh.example/a2</link><description>纯文本摘要。</description></item>
</channel></rss>"""

RSS_JA = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>新作発表</title><link>https://ja.example/b1</link>
<dc:date>2026-10-19T12:00:00+09:00</dc:date><description>要約テキスト</description></item>
</rdf:RDF>"""

# 轻小说源（ラノベニュースオンライン）真实 Atom 结构的最小化复刻：
# <entry> + <link rel="alternate" href> 文章地址 + <link rel="enclosure" type="image/jpeg"> 封面 + <updated> 带时区。
ATOM_LN = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>『弱気MAX令嬢』のオンラインくじが登場</title>
  <link rel="enclosure" type="image/jpeg" href="https://assets.ln-news.com/m.jpg" />
  <link href="https://ln-news.com/articles/126906" rel="alternate" />
  <id>tag:ln-news.com,2026:126906</id>
  <updated>2026-10-20T16:00:00+09:00</updated>
</entry>
<entry>
  <title>ラノベニュースオンラインアワード投票受付中</title>
  <link href="https://ln-news.com/articles/126986" rel="alternate" />
  <updated>2026-10-20T14:30:00+09:00</updated>
</entry>
</feed>"""

GAMERSKY_LIST = """
<a href="https://www.gamersky.com/news/202610/2227038.shtml">《海贼王》1195话分析</a>
<a href="https://www.gamersky.com/news/202610/2221013.shtml">日本首次确认声音受法律保护</a>
<a href="https://other.example/not-news.html">不是资讯的链接</a>
"""

GAMERSKY_DETAIL = """<html><head>
<meta name="description" content="继上一话索隆成功动用霸王色霸气之后，剧情重心转向山治。" />
</head><body>
发布时间：2026-10-19 18:24
<div class="Mid2L_con">
<p>正文第一段。</p>
<img data-src="https://img1.gamersky.com/x.jpg">
<p>正文第二段。</p>
</div>
<div class="liPage">分页导航不该进正文</div>
</body></html>"""


@pytest.fixture
def factory(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'news.db'}")
    crud.init_db(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """测试默认没有翻译 Key（配置以空为准），需要翻译能力的用例显式传 translate。"""
    monkeypatch.setattr(settings, "news_llm_provider", "")
    monkeypatch.setattr(settings, "news_llm_base_url", "")
    monkeypatch.setattr(settings, "news_llm_api_key", "")
    monkeypatch.setattr(settings, "news_llm_model", "")
    monkeypatch.setattr(settings, "persona_llm_provider", "")
    monkeypatch.setattr(settings, "persona_llm_base_url", "")
    monkeypatch.setattr(settings, "persona_llm_api_key", "")
    monkeypatch.setattr(settings, "persona_llm_model", "")


def fake_fetch(pages: dict[str, str]):
    def fetch(url: str) -> str:
        if url in pages:
            return pages[url]
        raise RuntimeError(f"未打桩的地址：{url}")

    return fetch


# ---------- 订阅源解析 ----------


class TestFeedParsing:
    def test_rss20_items(self):
        spec = SourceSpec("t", "测试", "https://zh.example/rss", "rss", "动画", "zh")
        items = parse_feed(RSS_ZH, spec)
        assert [i.title for i in items] == ["新动画宣布制作", "旧闻一条"]
        assert items[0].url == "https://zh.example/a1"
        assert items[0].published_at == datetime(2026, 10, 19, 23, 0)
        assert items[0].image_url == "https://zh.example/img1.jpg"
        assert "制作决定" in items[0].body
        assert "[图] https://zh.example/img1.jpg" in items[0].body
        assert len(items[0].summary) <= 140

    def test_rdf_items_with_dc_date(self):
        spec = SourceSpec("j", "日文源", "https://ja.example/rss", "rss", "动画", "ja")
        items = parse_feed(RSS_JA, spec)
        assert len(items) == 1
        assert items[0].title == "新作発表"
        # dc:date 是 JST(+09:00)：要换算成本机时区，不是照抄 12:00
        assert items[0].published_at == datetime(
            2026, 10, 19, 12, 0, tzinfo=timezone(timedelta(hours=9))
        ).astimezone().replace(tzinfo=None)

    def test_atom_items(self):
        """Atom 源（轻小说 ln-news）按 <entry> 解析，链接取 rel=alternate，封面取 enclosure 图片。"""
        spec = SourceSpec("ln", "ラノベニュースオンライン", "https://ln-news.com/feed", "rss", "轻小说", "ja")
        items = parse_feed(ATOM_LN, spec)
        assert len(items) == 2
        assert items[0].title == "『弱気MAX令嬢』のオンラインくじが登場"
        assert items[0].url == "https://ln-news.com/articles/126906"
        assert items[0].image_url == "https://assets.ln-news.com/m.jpg"
        # 带时区（+09:00）的 updated 必须正确解析为 naive 本地时间
        assert items[0].published_at == datetime(
            2026, 10, 20, 16, 0, tzinfo=timezone(timedelta(hours=9))
        ).astimezone().replace(tzinfo=None)
        # 第二条没有 enclosure 封面，回落为空（不报错）
        assert items[1].url == "https://ln-news.com/articles/126986"
        assert items[1].image_url is None

    def test_feed_date_formats(self):
        # +0800 与本站时区一致，换算后不变
        assert parse_feed_date("Mon, 19 Oct 2026 23:00:00 +0800") == datetime(2026, 10, 19, 23, 0)
        # +09:00（JST）必须换算成本机时区：直接抹掉 tzinfo 会让日文资讯快 1 小时
        jst = datetime(2026, 10, 19, 12, 0, tzinfo=timezone(timedelta(hours=9)))
        assert parse_feed_date("2026-10-19T12:00:00+09:00") == jst.astimezone().replace(tzinfo=None)
        # UTC（GMT 的 RSS）同理，不能当成本地时间
        utc = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        assert parse_feed_date("Mon, 19 Oct 2026 12:00:00 GMT") == utc.astimezone().replace(tzinfo=None)
        assert parse_feed_date("") is None
        assert parse_feed_date("garbage") is None

    def test_fetch_candidates_rss(self):
        spec = SourceSpec("t", "测试", "https://zh.example/rss", "rss", "动画", "zh")
        items = fetch_candidates(spec, fetch=fake_fetch({"https://zh.example/rss": RSS_ZH}))
        assert len(items) == 2


# ---------- 游民星空 ACG ----------


class TestGamerskyParsing:
    def test_list_parse_sorted_and_filtered(self):
        spec = SourceSpec("g", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh")
        items = parse_gamersky_list(GAMERSKY_LIST, spec)
        assert [i.title for i in items] == ["《海贼王》1195话分析", "日本首次确认声音受法律保护"]
        assert all(i.needs_detail for i in items)

    def test_detail_parse_time_summary_body(self):
        spec = SourceSpec("g", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh")
        candidates = parse_gamersky_list(GAMERSKY_LIST, spec)
        item = parse_gamersky_detail(GAMERSKY_DETAIL, candidates[0])
        assert item.published_at == datetime(2026, 10, 19, 18, 24)
        assert "剧情重心转向山治" in item.summary
        assert "正文第一段" in item.body
        assert "分页导航" not in item.body  # 截断点之后不进正文
        assert item.needs_detail is False

    def test_detail_prefers_header_time_over_sidebar_date(self):
        """文章头部带秒的真实时间优先；侧栏「发布时间：日期」只有日期，必须忽略。

        真实页面里侧栏推荐列表里有 `发布时间：2026-10-20`（无时刻），旧解析器认了它，
        把 2024 年的旧文写成了抓取当天——时效全错。这里用真实结构的最小复刻守护。
        """
        html = """<html><body>
        <div class="side"><div class="txt">发布时间：2026-10-20</div></div>
        <div class="Mid2L_tit">
            <h1>武侠动画《杯雪》发布首支PV 经典江湖客 乱世见英雄</h1>
            <div class="detail">
                2024-04-24 16:21:26 来源：官方 作者：未知 编辑：宁静海
            </div>
        </div>
        <div class="Mid2L_con"><p>正文。</p></div>
        </body></html>"""
        spec = SourceSpec("g", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh")
        item = parse_gamersky_detail(html, parse_gamersky_list(GAMERSKY_LIST, spec)[0])
        assert item.published_at == datetime(2024, 4, 24, 16, 21, 26)

    def test_parse_og_image(self):
        """详情页头图：og:image / twitter:image 两种属性顺序都要认。"""
        a = '<meta property="og:image" content="https://img.example/a.jpg">'
        b = '<meta content="https://img.example/b.jpg" name="twitter:image">'
        assert parse_og_image(a) == "https://img.example/a.jpg"
        assert parse_og_image(b) == "https://img.example/b.jpg"
        assert parse_og_image("<html></html>") is None

    def test_hydrate_head_image_fills_missing(self):
        """RSS 不带图时，补图函数抓详情页填 image_url；抓取失败静默保留空。"""
        spec = SourceSpec("t", "测试", "https://zh.example/rss", "rss", "动画", "zh")
        item = parse_feed(RSS_ZH, spec)[0]
        assert item.image_url == "https://zh.example/img1.jpg"  # feed 自带图，不该再抓
        bare = parse_feed(RSS_JA, spec)[0]
        assert bare.image_url is None
        page = '<meta property="og:image" content="https://img.example/x.jpg">'
        filled = hydrate_head_image(bare, fetch=fake_fetch({bare.url: page}))
        assert filled.image_url == "https://img.example/x.jpg"

        def boom(url):
            raise OSError("network down")

        still_bare = hydrate_head_image(parse_feed(RSS_JA, spec)[0], fetch=boom)
        assert still_bare.image_url is None  # 补图失败不抛、不阻断

    def test_hydrate_fetches_detail(self):
        spec = SourceSpec("g", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh")
        item = parse_gamersky_list(GAMERSKY_LIST, spec)[0]
        page = "https://www.gamersky.com/news/202610/2227038.shtml"
        from scripts.news_sources import hydrate

        result = hydrate(item, fetch=fake_fetch({page: GAMERSKY_DETAIL}))
        assert "正文第一段" in result.body
        assert result.needs_detail is False


# ---------- 每日配额与翻译口径 ----------


class TestDailyImport:
    def test_ja_source_imported_as_original_without_llm(self, factory):
        """没有翻译 Key 时日文源**不再整源跳过**，按原文入库且不标注「译文」。

        2026-10-21 口径：宁可上日文原文也不空着；LLM 接口保留，配 Key 后自动改回翻译。
        """
        fetch = fake_fetch({
            ZH_SOURCE.url: RSS_ZH,
            JA_SOURCE.url: RSS_JA,
        })
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(ZH_SOURCE, JA_SOURCE), fetch=fetch
        )
        assert summary.added == 3  # 中文 2 条 + 日文 1 条
        assert summary.skipped_sources == 0  # 不再整源跳过
        assert summary.untranslated_sources == 1  # 一个日文源按原文入库
        assert summary.per_source == {"zh-test": 2, "ja-test": 1}

        db = factory()
        try:
            rows = {row.title: row for row in db.scalars(select(Article))}
            assert set(rows) == {"新动画宣布制作", "旧闻一条", "新作発表"}  # 日文标题原样
            body = rows["新动画宣布制作"].body
            # 正文不再塞裸链接：详情页模板有独立的「原文链接」入口，正文里塞长 URL 会撑破排版
            assert "原文链接" not in body
            assert "zh.example/a1" not in body
            assert "译文由自动翻译生成" not in rows["新作発表"].body  # 没翻译就不能标译文
        finally:
            db.close()

    def test_daily_quota_caps_total(self, factory):
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})
        first = run_import_daily(
            factory, apply=True, quota=1, sources=(ZH_SOURCE,), fetch=fetch
        )
        # 「新动画宣布制作」命中官宣豁免（形态 动画 × 决定 制作），走豁免额度不占配额；
        # 「旧闻一条」是普通稿，用掉这唯一的 1 条配额 → 本轮共 2 条。
        assert first.added == 2
        assert first.exempted == 1

        # 当天配额用完：第二轮普通稿一条不收，官宣额度虽有余但无新官宣 → 新增为 0
        second = run_import_daily(
            factory, apply=True, quota=1, sources=(ZH_SOURCE,), fetch=fetch
        )
        assert second.added == 0
        assert "达到每日上限" in second.message

        db = factory()
        try:
            assert db.scalar(select(Article.original_url).where(
                Article.original_url == "https://zh.example/a1"
            ))
        finally:
            db.close()

    def test_quota_counts_articles_created_today(self, factory):
        db = factory()
        try:
            db.add(Article(title="今天已有的资讯", category="动画", summary="s", body="b",
                           created_at=datetime.now()))
            db.commit()
        finally:
            db.close()
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})
        summary = run_import_daily(
            factory, apply=True, quota=3, sources=(ZH_SOURCE,), fetch=fetch
        )
        # 已有 1 条占配额，本轮只补 2 条
        assert summary.added == 2
        assert summary.added_before == 1

    def test_dedupe_by_url_and_title(self, factory):
        db = factory()
        try:
            db.add(Article(title="同标题旧闻", category="动画", summary="s", body="b",
                           original_url="https://zh.example/old"))
            db.commit()
        finally:
            db.close()
        pages = {
            ZH_SOURCE.url: RSS_ZH.replace("https://zh.example/a2", "https://zh.example/old"),
        }
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(ZH_SOURCE,), fetch=fake_fetch(pages)
        )
        assert summary.added == 1  # a1 新增；a2 与旧记录 URL 重了
        assert summary.existing == 1

    def test_rerun_is_idempotent(self, factory):
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})
        run_import_daily(factory, apply=True, quota=20, sources=(ZH_SOURCE,), fetch=fetch)
        second = run_import_daily(
            factory, apply=True, quota=20, sources=(ZH_SOURCE,), fetch=fetch
        )
        assert second.added == 0
        assert second.existing == 2

    def test_ja_translated_when_key_available(self, factory, monkeypatch):
        monkeypatch.setattr(news_llm, "translate_available", lambda cfg=None: True)

        def translate(texts: list[str]) -> list[str] | None:
            out = []
            for text in texts:
                out.append("【译】" + text)
            return out

        fetch = fake_fetch({JA_SOURCE.url: RSS_JA})
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(JA_SOURCE,), fetch=fetch,
            translate=translate,
        )
        assert summary.added == 1
        assert summary.skipped_sources == 0
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
            assert row.title.startswith("【译】新作発表")
            assert row.summary.startswith("【译】")
        finally:
            db.close()

    def test_ja_translation_failure_skips_entry(self, factory, monkeypatch):
        """翻译挂了不写日文原文：跳过并计数（口径：日文必须翻译后才入库）。"""
        monkeypatch.setattr(news_llm, "translate_available", lambda cfg=None: True)

        def translate(texts: list[str]) -> list[str] | None:
            return None

        fetch = fake_fetch({JA_SOURCE.url: RSS_JA})
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(JA_SOURCE,), fetch=fetch,
            translate=translate,
        )
        assert summary.added == 0
        assert summary.translate_failed == 1
        db = factory()
        try:
            assert db.scalars(select(Article)).all() == []
        finally:
            db.close()

    def test_preview_writes_nothing(self, factory):
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})
        summary = run_import_daily(
            factory, apply=False, quota=20, sources=(ZH_SOURCE,), fetch=fetch
        )
        assert summary.added == 2
        db = factory()
        try:
            assert db.scalars(select(Article)).all() == []
        finally:
            db.close()

    def test_source_fetch_failure_does_not_stop_rest(self, factory):
        broken = SourceSpec("broken", "坏源", "https://bad.example/rss", "rss", "动画", "zh")
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})  # bad.example 未打桩 -> 失败
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(broken, ZH_SOURCE), fetch=fetch
        )
        assert summary.added == 2
        assert summary.failed == 1
        assert summary.details[0]["status"] == "failed"

    def test_real_source_registry_languages(self):
        """源注册表：日文源必须标记 lang=ja（否则无 Key 时会灌日文进站）。"""
        assert {spec.lang for spec in SOURCES} == {"zh", "ja"}
        ja = [spec for spec in SOURCES if spec.lang == "ja"]
        assert ja, "日文源清单不能为空（有 Key 时按「日为主」跑）"

    def test_source_registry_covers_manga_and_light_novel(self):
        """题材优先级要成立，漫画 / 轻小说 档位必须有源（至少登记）。"""
        categories = {spec.category for spec in SOURCES}
        assert "轻小说" in categories, "缺轻小说源（ln-news）"
        assert "漫画" in categories, "缺漫画源（natalie_comic 占位）"
        ln = [s for s in SOURCES if s.category == "轻小说"]
        assert any(s.key == "lnnews" and s.url == "https://ln-news.com/feed" for s in ln)
        mg = [s for s in SOURCES if s.category == "漫画"]
        assert any(s.key == "natalie_comic" for s in mg)

    def test_ja_priority_over_zh(self, factory, monkeypatch):
        """日语源优先：配额只够 1 条时，应取日语条目而非中文。"""
        monkeypatch.setattr(news_llm, "translate_available", lambda cfg=None: True)

        def translate(texts: list[str]) -> list[str]:
            return ["【译】" + t for t in texts]

        fetch = fake_fetch({JA_SOURCE.url: RSS_JA, ZH_SOURCE.url: RSS_ZH})
        # exempt_quota=0：本条只验「普通条目按 priority 竞争」，豁免语义见
        # TestMajorAnnouncementExemption（官宣不占配额，会绕过这里的优先级比较）。
        summary = run_import_daily(
            factory, apply=True, quota=1, exempt_quota=0,
            sources=(JA_SOURCE, ZH_SOURCE), fetch=fetch, translate=translate,
        )
        assert summary.added == 1
        assert summary.per_source == {"ja-test": 1}  # 中文源根本没被抓取
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
            assert row.title.startswith("【译】")
        finally:
            db.close()

    def test_topic_priority_anime_before_game(self, factory):
        """题材优先级：同语言下动画（优先）应先于游戏入库。"""
        fetch = fake_fetch({ANIME_SRC.url: RSS_ANIME_ONE, GAME_SRC.url: RSS_GAME_ONE})
        summary = run_import_daily(
            factory, apply=True, quota=1, sources=(ANIME_SRC, GAME_SRC), fetch=fetch
        )
        assert summary.added == 1
        assert summary.per_source == {"zh-anime": 1}  # 游戏源未入选

    def test_low_priority_source_not_fetched_when_quota_met(self, factory):
        """高优先级源凑满配额后，低优先级源不应再被抓取（避免无谓请求）。"""
        # 故意不给 GAME_SRC 打桩：若它被抓取会因未打桩抛错并计入 failed。
        fetch = fake_fetch({ANIME_SRC.url: RSS_ANIME_ONE})
        summary = run_import_daily(
            factory, apply=True, quota=1, sources=(ANIME_SRC, GAME_SRC), fetch=fetch
        )
        assert summary.added == 1
        assert summary.failed == 0
        assert summary.per_source == {"zh-anime": 1}


# ---------- 翻译批处理 ----------


class TestTranslateTexts:
    def test_batch_translation(self, monkeypatch):
        monkeypatch.setattr(news_llm, "translate_available", lambda cfg=None: True)
        cfg = {"ready": True, "base_url": "https://llm.example", "api_key": "k", "model": "m"}

        def fake_call(prompt: str) -> str:
            assert "1." in prompt
            return "1. 译A\n2. 译B"

        result = news_llm.translate_texts(["标题A", "标题B"], cfg=cfg, call=fake_call)
        assert result == ["译A", "译B"]

    def test_line_count_mismatch_returns_none(self, monkeypatch):
        cfg = {"ready": True, "base_url": "https://llm.example", "api_key": "k", "model": "m"}
        result = news_llm.translate_texts(["标题A"], cfg=cfg, call=lambda p: "1. 译A\n2. 多余")
        assert result is None

    def test_not_configured_reports_unavailable(self):
        assert news_llm.translate_available() is False


# ---------- 软配额 / 分类落库 / 反垃圾 ----------


def _cand(spec: SourceSpec, title: str, url: str = "", summary: str = ""):
    from scripts.news_sources import NewsCandidate

    return NewsCandidate(
        title=title,
        url=url or f"https://ex.example/{abs(hash(title)) % 99999}",
        source=spec,
        summary=summary,
        published_at=datetime(2026, 10, 20, 12, 0),
    )


class TestSoftQuotaAndClassification:
    def test_general_subject_competes_with_others(self):
        """综合动态与其它主体同权（2026-10-21 用户定稿）：不再单独封顶，配额够就全收。

        旧口径给综合动态每天封顶 6 条，用户明确「综合可以和游戏同权」后取消；
        类别覆盖改由「主体基础分同权」自然调节（动画/漫画/轻小说仍高一档）。
        """
        from scripts.news_daily import _PER_SUBJECT_CAP, select_quota

        general = SourceSpec(
            "g", "综合源", "https://g.example/rss", "rss", "综合动态", "zh"
        )
        pool = [_cand(general, f"综合资讯第 {i} 条") for i in range(20)]
        selected, _ = select_quota(pool, 10, 0)
        assert len(selected) == 10  # 没有综合动态专属封顶
        assert _PER_SUBJECT_CAP == {}

    def test_same_event_reports_are_merged(self):
        """同一事件的不同报道只占一个位置。"""
        from scripts.news_daily import select_quota

        src = SourceSpec("a", "动画源", "https://a.example/rss", "rss", "动画", "zh")
        pool = [
            _cand(src, "新作アニメ「ホシ」制作決定、2027年放送"),
            _cand(src, "新作アニメ「ホシ」制作決定、2027年放送へ"),
        ]
        # 两条都是「制作決定」类官宣 → 走豁免额度，所以显式给豁免额度（默认 0 会一条不收）
        selected, deduped = select_quota(pool, 10, 10)
        assert len(selected) == 1
        assert deduped == 1

    def test_higher_priority_wins_when_duplicated(self):
        """同事件去重保留优先级更高的那条（日本报道优先于欧美报道）。"""
        from scripts.news_daily import select_quota

        jp = SourceSpec("jp", "日本源", "https://jp.example/rss", "rss", "游戏", "zh", region="日本")
        west = SourceSpec("w", "欧美源", "https://w.example/rss", "rss", "游戏", "zh", region="欧美")
        title = "新作ゲーム発売決定、2027年春に配信"
        pool = [_cand(west, title), _cand(jp, title)]
        selected, _ = select_quota(pool, 10, 10)
        assert len(selected) == 1
        assert selected[0].source.key == "jp"

    def test_hard_ad_is_filtered(self, factory):
        """最露骨广告被过滤，不计入新增。"""
        src = SourceSpec("ad", "广告源", "https://ad.example/rss", "rss", "动画", "zh")
        pool_src = SourceSpec(
            "ok", "正常源", "https://ok.example/rss", "rss", "动画", "zh"
        )
        fetch = fake_fetch({})
        from scripts.news_daily import run_import_daily

        # 直接走 select/persist 之外的路径不方便，这里用 hydrate 打桩：
        # 通过 run_import_daily 的 fetch 打桩拿到 RSS，再校验过滤计数
        rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>【PR】新作グッズ広告</title><link>https://ad.example/1</link>
<description>赞助内容</description><pubDate>Mon, 20 Oct 2026 12:00:00 +0900</pubDate></item>
<item><title>新作アニメ制作決定</title><link>https://ad.example/2</link>
<description>制作決定</description><pubDate>Mon, 20 Oct 2026 12:00:00 +0900</pubDate></item>
</channel></rss>"""
        fetch = fake_fetch({src.url: rss})
        summary = run_import_daily(
            factory, apply=True, quota=10, sources=(src,), fetch=fetch
        )
        assert summary.filtered == 1
        assert summary.added == 1
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
            assert row.title == "新作アニメ制作決定"
        finally:
            db.close()

    def test_adult_galge_is_kept_and_flagged(self, factory):
        """galge 不因成人向被删除，只打 is_adult 标记（展示层隐藏）。"""
        gal = SourceSpec("gal", "Galgame源", "https://gal.example/rss", "rss", "游戏", "zh")
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>18禁ギャルゲー新作発売決定</title><link>https://gal.example/1</link>
<description>18禁美少女ゲーム</description><pubDate>Mon, 20 Oct 2026 12:00:00 +0900</pubDate></item>
</channel></rss>"""
        fetch = fake_fetch({gal.url: rss})
        summary = run_import_daily(
            factory, apply=True, quota=10, sources=(gal,), fetch=fetch
        )
        assert (summary.added, summary.filtered) == (1, 0)
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
            assert row.is_adult is True
            assert row.subcategory == "Galgame"
            assert row.category == "游戏"
        finally:
            db.close()

    def test_classification_and_priority_are_persisted(self, factory):
        """主体 / 事件 / 地区 / 来源类型 / priority_score 全部落库且互不相同。"""
        src = SourceSpec("z", "动画源", "https://z.example/rss", "rss", "动画", "zh")
        rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>新作アニメ制作決定、キャスト発表</title><link>https://z.example/1</link>
<description>制作決定とキャスト発表</description><pubDate>Mon, 20 Oct 2026 12:00:00 +0900</pubDate></item>
</channel></rss>"""
        fetch = fake_fetch({src.url: rss})
        run_import_daily(factory, apply=True, quota=10, sources=(src,), fetch=fetch)
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
            assert row.category == "动画"
            assert row.event_type in ("新作发布", "制作消息")
            assert row.region == "日本"
            assert row.source_type == "媒体编译"
            assert row.priority_score > 0
            assert row.subcategory == ""  # 非游戏不打子类
        finally:
            db.close()


# ---------- 2026-10-22：重大官宣配额豁免 ----------

RSS_MAJOR = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>官宣源</title>
<item><title>『ホシ』アニメ化決定、2027年放送</title><link>https://zh.example/m1</link>
<description>アニメ化が決定しました。制作会社は後日発表されます。</description>
<pubDate>Mon, 20 Oct 2026 12:00:00 +0900</pubDate></item>
<item><title>旧闻一条</title><link>https://zh.example/m2</link>
<description>纯文本摘要，跟官宣没有任何关系的一条普通资讯。</description>
<pubDate>Mon, 20 Oct 2026 11:00:00 +0900</pubDate></item>
</channel></rss>"""

RSS_MAJOR_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>官宣源2</title>
<item><title>『ツキ』第2期制作決定</title><link>https://zh.example/m3</link>
<description>第2期の制作が決定しました。続報をお待ちください。</description>
<pubDate>Mon, 20 Oct 2026 13:00:00 +0900</pubDate></item>
</channel></rss>"""


class TestMajorAnnouncementExemption:
    def test_word_table_hits_and_misses(self):
        """词表三档：化类 > 续作/剧场版 > 中文官宣；普通稿不命中。"""
        from scripts.news_classify import (
            MAJOR_NONE,
            MAJOR_OFFICIAL,
            MAJOR_SEQUEL,
            MAJOR_STRONG,
            detect_major_announcement,
        )

        assert detect_major_announcement("『ホシ』アニメ化決定、2027年放送")[0] == MAJOR_STRONG
        assert detect_major_announcement("『ホシ』映画化が決定")[0] == MAJOR_STRONG
        assert detect_major_announcement("『ホシ』コミカライズ決定")[0] == MAJOR_STRONG
        assert detect_major_announcement("『ホシ』第2期制作決定")[0] == MAJOR_SEQUEL
        assert detect_major_announcement("『ホシ』続編制作決定")[0] == MAJOR_SEQUEL
        assert detect_major_announcement("新动画宣布制作")[0] == MAJOR_OFFICIAL
        # 普通稿：没有决定词，或没有形态词，都不算官宣
        assert detect_major_announcement("旧闻一条")[0] == MAJOR_NONE
        assert detect_major_announcement("ゲームアップデート実施")[0] == MAJOR_NONE
        assert detect_major_announcement("〇〇のグッズ発売")[0] == MAJOR_NONE

    def test_operator_news_are_not_announcements(self):
        """线上 286 条真实标题回归：商品 / 活动 / 放映 / 报道稿一律不算官宣。

        这批全是 2026-10-22 线上实测被误判的原文标题（Aniplex 官方 PR 的稿子几乎都带
        「TVアニメ」「劇場アニメ」前缀，用「裸形态词 × 决定词」判定会全线命中）。
        """
        from scripts.news_classify import MAJOR_NONE, detect_major_announcement

        for title in (
            "TVアニメ「マジカル★エクスプローラー」 Blu-ray全4巻が発売決定！",
            "TVアニメ「バーテックスフォース」Blu-ray&DVDシリーズ発売決定！",
            "アニメ「青春ブタ野郎」シリーズTVer、ABEMAでの配信決定！",
            "TVアニメ「マッシュル」、10月4日よりBS11にて再放送決定！",
            "TVアニメ「妖狐×僕SS」15周年企画始動！",
            "アニメまんが聖地EXPO2026にてステージ参加決定！",
            "TVアニメ『BLEACH』完結記念！歴代OP＆EDリスニングパーティー 開催決定！",
            "アニメ『SAO』POP UP STOREが新宿マルイにて開催決定",
            "劇場アニメーション『魔法使いの夜』最終予告公開！",
            "マチアソビVol.31にて劇場アニメーション「魔法使いの夜」の企画が展開！",
            "ゲーム「放置少女」×アニメ「生徒会にも穴はある！」コラボ決定！",
            "「ガンダム」旧作アニメのリメイクの定義【藤津亮太のアニメの門V】",
            "第25回角川ビーンズ小説大賞の受賞結果が発表",
            "サンリオ パーティランド」新TVCM発表会",
            "「カルドセプト ビギンズ」，PC版の発売日が10月28日に決定",
        ):
            assert detect_major_announcement(title)[0] == MAJOR_NONE, title

    def test_plot_teasers_are_not_announcements(self):
        """「官宣 + 剧情情报」不算官宣（2026-10-22 用户明确剔除博人传这类）。

        「官宣」在中文媒体里也用来发设定 / 剧情爆料，单靠这个词会把
        「《博人传》官宣火影全新劲敌」这种当成新作官宣——必须配形态结果词。
        """
        from scripts.news_classify import is_major_announcement

        for title in (
            "《博人传》官宣火影全新劲敌 威胁程度远超壳组织",
            "时隔13年 《博人传》官宣：木叶诞生全新顶级族群",
        ):
            assert not is_major_announcement(title), title
        for title in (
            "《罗小黑战记 学前篇》官宣定档10月30日 B站独播",
            "《海贼王》官宣双剧场版：神之谷2027年 BAAD2029年",
            "恋爱漫画《8月31日的长夏》官宣双重改编 SHAFT制作",
        ):
            assert is_major_announcement(title), title

    def test_real_announcements_still_hit(self):
        """同一批线上数据里真正的官宣不能被收紧掉（过拟合检查）。"""
        from scripts.news_classify import detect_major_announcement, is_major_announcement

        for title in (
            "《胡桃同学是人造人》宣布2027年TV动画化",
            "《加奈子的幸福杀手生活》2027年4月TV动画化",
            "续篇来袭！《魔都精兵的奴隶》动画第三季制作确定",
            "《晨曦公主》续作动画由PIERROT FILMS制作",
            "机动战士高达最新动画官宣定档 2027年4月播出",
            "劇場版『ホシ』2027年公開、特報映像公開",
            "『ホシ』第3期の制作が決定、2027年放送",  # 含「放送」但是正经续作官宣
        ):
            assert is_major_announcement(title), title

    def test_major_is_admitted_when_quota_exhausted(self, factory):
        """配额跑满（quota=0）时仍收官宣，普通稿一条不进。"""
        fetch = fake_fetch({ZH_SOURCE.url: RSS_MAJOR})
        summary = run_import_daily(
            factory, apply=True, quota=0, exempt_quota=2,
            sources=(ZH_SOURCE,), fetch=fetch,
        )
        assert summary.added == 1
        assert summary.exempted == 1
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
            assert row.title == "『ホシ』アニメ化決定、2027年放送"
            assert row.is_major is True
        finally:
            db.close()

    def test_major_does_not_consume_daily_quota(self, factory):
        """官宣走豁免额度，不占普通配额：quota=1 时应进 1 条官宣 + 1 条普通稿。"""
        fetch = fake_fetch({ZH_SOURCE.url: RSS_MAJOR})
        summary = run_import_daily(
            factory, apply=True, quota=1, exempt_quota=5,
            sources=(ZH_SOURCE,), fetch=fetch,
        )
        assert summary.added == 2
        assert summary.exempted == 1
        db = factory()
        try:
            rows = {row.title: row for row in db.scalars(select(Article))}
            assert rows["『ホシ』アニメ化決定、2027年放送"].is_major is True
            assert rows["旧闻一条"].is_major is False
        finally:
            db.close()

    def test_exempt_budget_is_counted_across_runs(self, factory):
        """豁免额度按天累计（跨轮查 is_major），用完当天不再收。"""
        first = run_import_daily(
            factory, apply=True, quota=0, exempt_quota=1,
            sources=(ZH_SOURCE,), fetch=fake_fetch({ZH_SOURCE.url: RSS_MAJOR}),
        )
        assert first.exempted == 1

        # 第二轮：额度已用完（不是每轮重置），换一批官宣标题也不收
        second = run_import_daily(
            factory, apply=True, quota=0, exempt_quota=1,
            sources=(ZH_SOURCE,), fetch=fake_fetch({ZH_SOURCE.url: RSS_MAJOR_2}),
        )
        assert second.exempted == 0
        assert second.added == 0
        assert "达到每日上限" in second.message

    def test_major_sorts_ahead_of_plain_in_same_round(self):
        """豁免池先入场：普通配额为 0 时，只有官宣能被选中。"""
        from scripts.news_daily import select_quota

        pr = SourceSpec(
            "pr", "官方PR源", "https://pr.example/rss", "rss", "动画", "zh",
            source_type="官方PR",
        )
        media = SourceSpec("m", "媒体源", "https://m.example/rss", "rss", "动画", "zh")
        major = _cand(pr, "『ホシ』アニメ化決定")
        plain = _cand(media, "アニメ新作の放送開始")
        selected, _ = select_quota([plain, major], 0, 1)
        # 官方 PR 源有 -25 降权，但豁免只看官宣强度，所以它排在前面
        assert [candidate.title for candidate in selected] == ["『ホシ』アニメ化決定"]

    def test_ensure_article_major_column_is_idempotent(self, tmp_path):
        """老库补 is_major 列：补完历史行置 0，且可重复执行（每轮 cron 都会跑 init_db）。"""
        from sqlalchemy import create_engine, inspect, text

        from app import crud

        engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE articles (id INTEGER PRIMARY KEY, "
                    "title VARCHAR(200) NOT NULL, summary TEXT NOT NULL, body TEXT NOT NULL)"
                )
            )
            connection.execute(
                text("INSERT INTO articles (title, summary, body) VALUES ('旧闻', 's', 'b')")
            )

        assert crud.ensure_article_major_column(engine) == ["is_major"]
        assert "is_major" in {c["name"] for c in inspect(engine).get_columns("articles")}
        with engine.begin() as connection:
            assert connection.execute(text("SELECT is_major FROM articles")).scalar() == 0
        assert crud.ensure_article_major_column(engine) == []  # 幂等
        engine.dispose()

    def test_major_priority_bonus_beats_official_pr_penalty(self):
        """官宣加成（+60）要盖得住官方 PR 的 -25 与商品宣传的 -30。"""
        from scripts.news_classify import MAJOR_ANNOUNCEMENT_BONUS, compute_priority_score

        base = dict(category="动画", region="日本", event_type="新作发布",
                    source_type="官方PR")
        plain = compute_priority_score(**base, text="『ホシ』の新情報")
        major = compute_priority_score(**base, text="『ホシ』アニメ化決定")
        assert major == plain + MAJOR_ANNOUNCEMENT_BONUS


# ---------- 2026-10-21：游民星空标题污染修复 + 新源注册 + 停用开关 ----------


class TestGamerskyTitleCleanup:
    def test_list_prefers_tit_div(self):
        """真标题在 <div class="tit"> 里；vote/评论数（tem div）不得混进标题。"""
        html = (
            '<a href="https://www.gamersky.com/news/202404/1723000.shtml">'
            '<img src="https://imgs.gamersky.com/x.jpg" alt="武侠动画《杯雪》发布首支PV">'
            '<div class="tit">武侠动画《杯雪》发布首支PV 乱世见英雄</div>'
            '<div class="tem"><div class="vote gshit">4354</div><div>0</div></div></a>'
        )
        spec = SourceSpec("g", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh")
        items = parse_gamersky_list(html, spec)
        assert items[0].title == "武侠动画《杯雪》发布首支PV 乱世见英雄"

    def test_list_fallback_strips_count_tail(self):
        """无 tit div 时退回整体折叠，只剥「数字 数字」计数尾巴，正常数字标题不动。"""
        html = (
            '<a href="https://www.gamersky.com/news/202404/1723001.shtml">新番导视十月篇 4354 0</a>'
            '<a href="https://www.gamersky.com/news/202404/1723002.shtml">《足球小将》第4话</a>'
        )
        spec = SourceSpec("g", "游民星空 ACG", "https://acg.gamersky.com/", "html", "动画", "zh")
        items = parse_gamersky_list(html, spec)
        assert items[0].title == "《足球小将》第4话"  # id 更大，倒序在前；正常数字标题不受影响
        assert items[1].title == "新番导视十月篇"


class TestSourceRegistry20261021:
    def test_acgch_and_chuapp_registered(self):
        keys = {spec.key for spec in SOURCES}
        assert {"acgch", "chuapp"} <= keys
        acgch = next(spec for spec in SOURCES if spec.key == "acgch")
        assert acgch.url == "https://acgch.com/zh-hans/rss.xml"
        assert acgch.lang == "zh" and acgch.kind == "rss"
        chuapp = next(spec for spec in SOURCES if spec.key == "chuapp")
        assert chuapp.url == "http://www.chuapp.com/feed"

    def test_disabled_sources_skipped_without_fetch(self, factory, monkeypatch):
        """NEWS_DISABLED_SOURCES：列出的源整源跳过，连网络请求都不发。"""
        monkeypatch.setattr(settings, "news_disabled_sources", "disabled-src")
        disabled = SourceSpec(
            "disabled-src", "停用源", "https://off.example/rss", "rss", "动画", "zh"
        )
        # off.example 未打桩：若停用源仍被抓取会直接 failed，测试立刻暴露
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(disabled, ZH_SOURCE), fetch=fetch
        )
        assert summary.added == 2
        assert summary.per_source == {"zh-test": 2}
        assert summary.failed == 0


# ---------- 2026-10-21：RSS 源正文提取（详情页正文，不再拿摘要冒充正文） ----------


class TestExtractBody:
    def test_acgch_container_with_noise_lines(self):
        html = (
            "<article><div>游戏2026年10月21日 GMT+9 18:44约 1 分钟读完</div>"
            "<p>《药屋少女的呢喃》首款家用游戏2027年3月18日发售，日向夏原案担任原案的完全新作。</p>"
            "<p>本作讲述猫猫刚开始当侍女不久的后宫故事，被称为五个谜团的推理谭。</p>"
            "<div>有瓜？来投个稿→</div></article>"
        )
        body = extract_body(html)
        assert "首款家用游戏" in body
        assert "五个谜团" in body
        assert "分钟读完" not in body
        assert "投个稿" not in body

    def test_no_known_container_returns_empty(self):
        assert extract_body("<html><body><p>正文段落。</p></body></html>") == ""

    def test_too_short_body_returns_empty(self):
        """快讯本来就短：提取结果低于阈值返回空串，由调用方保留摘要形态。"""
        assert extract_body("<article><p>短讯一条。</p></article>") == ""

    def test_related_card_before_body_is_ignored(self):
        """同页先出现「相关文章」卡片时，取文本最长的容器（2026-10-22 ACGCH 实测病症）。

        旧版按出现顺序取第一个 <article>，拿到的是推荐卡片，长度不够 → 返回空
        → 正文退化成摘要。现在按容器取最长，并且认 div.article-body。
        """
        html = (
            "<html><body>"
            '<article class="tl-card"><h3>推荐阅读</h3><p>另一篇文章的标题在这。</p></article>'
            '<div class="article-body">'
            "<p>这是文章真正的第一段正文，讲的是首款家用游戏的发售时间与平台信息。</p>"
            "<p>第二段继续交代原案作者、故事背景与玩法类型，长度足够通过最短校验。</p>"
            "</div>"
            "</body></html>"
        )
        body = extract_body(html)
        assert "首款家用游戏的发售时间" in body
        assert "推荐阅读" not in body

    def test_nested_divs_are_not_truncated(self):
        """正文容器里嵌套 div 时不能腰斩（旧正则非贪婪在第一个 </div> 就停）。"""
        html = (
            '<div class="entry-content">'
            "<div><p>第一段：这是嵌套在外层 div 里的正文内容，足够长以通过最短长度校验。</p></div>"
            "<p>第二段：这部分在外层 div 之后，非贪婪正则取不到它。</p>"
            "</div>"
        )
        body = extract_body(html)
        assert "第一段" in body
        assert "第二段" in body

    def test_stylesheet_and_media_pixel_lines_are_dropped(self):
        """script/style/注释与统计像素行不得混进正文（游民星空正文开头实测）。"""
        html = (
            '<div class="article-body">'
            "<style>.x{color:red}</style>"
            "<p>这是正文第一段，内容需要足够长才能通过最短长度校验，所以这里多写一点：讲的是动画改编情况。</p>"
            "<p>[媒体] https://www.gamersky.com/img/baidu_300news.htm</p>"
            "<p>--> 注释残留</p>"
            "<p>这是正文第二段，补充了制作公司与播出档期，同样写长一点以凑够最短长度要求。</p>"
            "</div>"
        )
        body = extract_body(html)
        assert "这是正文第一段" in body
        assert "baidu_300news" not in body
        assert "color:red" not in body
        assert "-->" not in body

    def test_rss_candidate_gets_real_body(self, factory):
        """RSS 候选：详情页抓得到正文 → body=真正文，summary 回填正文前 200 字。"""
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<item><title>ACGCH快讯标题</title><link>https://acgch.example/news/a</link>"
            "</item></channel></rss>"
        )
        article_page = (
            "<article>"
            "<p>这是详情页真正的第一段正文，内容足够长，能够通过最短长度校验，不会被当作提取失败。</p>"
            "<p>第二段：游戏将于2027年3月18日发售，登陆 Nintendo Switch 与 Steam 平台。</p>"
            "</article>"
        )
        fetch = fake_fetch(
            {ZH_SOURCE.url: rss, "https://acgch.example/news/a": article_page}
        )
        summary = run_import_daily(
            factory, apply=True, quota=10, sources=(ZH_SOURCE,), fetch=fetch
        )
        assert summary.added == 1
        db = factory()
        try:
            article = db.scalars(select(Article)).one()
            assert "真正的第一段正文" in article.body
            assert article.summary.startswith("这是详情页真正的第一段正文")
        finally:
            db.close()




# ---------- 源顺序与池子均衡（2026-10-23：游戏源先到先得抢光配额） ----------


BIG_GAME_SRC = SourceSpec("big-game", "测试大游戏源", "https://big-game.example/rss", "rss", "游戏", "zh")
MANY_SRC = SourceSpec("many", "测试综合源", "https://many.example/rss", "rss", "综合动态", "zh")

# 刻意用互不相似的标题：同事件的字符二元组相似度会触发去重合并。
DISTINCT_TITLES = [
    "声优访谈：夏日回忆",
    "动画电影票房破十亿",
    "厚木市联动海报公开",
    "轻小说新刊书单推荐",
    "店内主题橱窗布置",
    "电台节目第四回",
]


def rss_items(prefix: str, titles: list[str]) -> str:
    """拼一个含多条 item 的最小 RSS。"""
    items = "".join(
        f"<item><title>{title}</title><link>{prefix}/{index}</link>"
        f"<description>摘要。</description></item>"
        for index, title in enumerate(titles)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss version=\"2.0\"><channel><title>t</title>{items}</channel></rss>"
    )


# 互不相似的动画标题：同事件去重按标题二元组算相似度（阈值 0.62），
# 用「动画新闻1/2/3」这种只差一个字的标题会被判成同一事件合并掉。
ANIME_TITLES = [
    "剧场版动画票房突破百亿日元",
    "声优阵容追加三位新人",
    "电视动画第二季定档四月",
    "监督换人引发热议",
    "片头曲由知名乐队创作",
    "原作累计发行突破千万册",
    "海外配信平台同步上线",
    "角色设计图首次曝光",
    "特别篇年末播出决定",
    "舞台化改编计划启动",
    "蓝光套装附赠广播剧",
    "制作公司启用新工作室",
    "主题曲试听片段放出",
    "动画电影入围国际影展",
    "人气投票结果揭晓",
    "联动咖啡厅限定菜单",
    "官方画集将于下月发售",
    "导演谈创作幕后故事",
    "官方网站改版上线",
    "首映礼现场报道",
    # 下面这组用来把动画候选池撑到 50 条以上（验证「保底之后剩余额度仍按权重回到动画」）。
    "东京漫展现场速报",
    "京都工作室参观记",
    "北海道取景地巡礼",
    "福冈粉丝见面会",
    "名古屋上映会记录",
    "札幌特别放映",
    "仙台签名会实况",
    "横滨主题展参观",
    "神户联动活动报道",
    "广岛巡回展启动",
    "新潟放映厅加场",
    "金泽原画展纪实",
    "长野露营企划",
    "静冈舞台见面会",
    "冈山粉丝感谢祭",
    "熊本复刻上映",
    "鹿儿岛先行放映",
    "冲绳特别活动",
    "大阪座谈会实录",
    "奈良合作企划公布",
    "蓝光修复版重映",
    "原声带追加曲目",
    "设定资料集第二卷",
    "角色歌专辑发售",
    "广播剧CD化决定",
    "手办化企划公开",
    "改编新作正式发表",
    "舞台剧演员定妆",
    "朗读剧门票开售",
    "美术展巡回日程",
    "分镜集限定发售",
    "制作记录片上线",
]


def _subject_cand(spec: SourceSpec, title: str, subject: str, score: int):
    """构造一条「主体类型 / 优先级」已固定的候选。

    绕过文本分类：类别保底按 `_classify` 的 category 计数，这里直接写死分类结果，
    测试才可以只关心「各类留几条」而不被关键词判定牵着走（`_classify` 会复用缓存）。
    """
    candidate = _cand(spec, title)
    candidate.classification = {
        "category": subject,
        "subcategory": "",
        "event_type": "新作情报",
        "region": "日本",
        "source_type": spec.source_type,
        "priority_score": score,
        "is_adult": False,
        "major": 0,
        "major_hit": "",
    }
    return candidate


class TestSourceOrderAndPoolBalance:
    def test_sources_follow_category_priority(self):
        """抓源顺序固定为 动画 → 漫画 → 轻小说 → 游戏 → 综合动态（2026-10-23 定稿）。"""
        from scripts.news_daily import _ordered_sources

        specs = (
            SourceSpec("g1", "游戏1", "https://g1.example/rss", "rss", "游戏", "zh"),
            SourceSpec("n1", "轻小说1", "https://n1.example/rss", "rss", "轻小说", "zh"),
            SourceSpec("a1", "动画1", "https://a1.example/rss", "rss", "动画", "zh"),
            SourceSpec("gen1", "综合1", "https://gen.example/rss", "rss", "综合动态", "zh"),
            SourceSpec("m1", "漫画1", "https://m1.example/rss", "rss", "漫画", "zh"),
            SourceSpec("a2", "动画2", "https://a2.example/rss", "rss", "动画", "zh"),
        )
        assert [spec.key for spec in _ordered_sources(specs)] == [
            "a1", "a2", "m1", "n1", "g1", "gen1",
        ]

    def test_official_pr_still_last(self):
        """交错不影响原有口径：官方 PR 源仍然垫底。"""
        from scripts.news_daily import _ordered_sources

        specs = (
            SourceSpec("pr", "PR源", "https://pr.example/rss", "rss", "动画", "zh",
                       source_type="官方PR"),
            SourceSpec("a1", "动画1", "https://a1.example/rss", "rss", "动画", "zh"),
        )
        assert [spec.key for spec in _ordered_sources(specs)] == ["a1", "pr"]

    def test_single_source_cannot_swallow_the_run(self, factory):
        """一个 20 条的游戏源不能独吞配额，排在后面的源照样会被抓。"""
        fetch = fake_fetch({
            BIG_GAME_SRC.url: rss_items("https://big-game.example", [f"游戏新闻{i}" for i in range(20)]),
            ANIME_SRC.url: RSS_ANIME_ONE,
            MANY_SRC.url: rss_items("https://many.example", DISTINCT_TITLES[:2]),
        })
        summary = run_import_daily(
            factory, apply=True, quota=10, run_quota=0, exempt_quota=0,
            sources=(BIG_GAME_SRC, ANIME_SRC, MANY_SRC), fetch=fetch,
        )
        # ceil(10 * 0.34) = 4：大游戏源最多贡献 4 条，剩下的位置留给后面的源
        assert summary.per_source.get("big-game", 0) <= 4
        assert summary.per_source.get("zh-anime") == 1
        assert summary.per_source.get("many", 0) >= 1

    def test_run_quota_limits_each_run(self, factory):
        """日配额 100、单轮 2：一轮只入库 2 条，下一轮继续。"""
        fetch = fake_fetch({MANY_SRC.url: rss_items("https://many.example", DISTINCT_TITLES)})
        kwargs = dict(
            apply=True, quota=100, run_quota=2, exempt_quota=0,
            sources=(MANY_SRC,), fetch=fetch,
        )
        assert run_import_daily(factory, **kwargs).added == 2
        assert run_import_daily(factory, **kwargs).added == 2
        assert run_import_daily(factory, **kwargs).added == 2
        # 6 条全部按轮放出，日配额（100）远未用完
        assert run_import_daily(factory, **kwargs).added == 0

    def test_run_quota_zero_means_unlimited(self, factory):
        """run_quota=0 退回「只看日配额」的旧行为。"""
        fetch = fake_fetch({MANY_SRC.url: rss_items("https://many.example", DISTINCT_TITLES)})
        summary = run_import_daily(
            factory, apply=True, quota=100, run_quota=0, exempt_quota=0,
            sources=(MANY_SRC,), fetch=fetch,
        )
        assert summary.added == len(DISTINCT_TITLES)

    def test_category_floor_reserves_quota_per_category(self):
        """动画能喂满配额时，保底仍给漫画 / 轻小说 / 游戏各留位置。"""
        from scripts.news_daily import select_quota
        from scripts.news_classify import SUBJECT_ANIME, SUBJECT_GAME, SUBJECT_LN, SUBJECT_MANGA

        pool = [_subject_cand(ANIME_SRC, t, SUBJECT_ANIME, 200) for t in ANIME_TITLES]
        pool += [
            _subject_cand(ANIME_SRC, t, SUBJECT_MANGA, 120)
            for t in ("漫画单行本发售", "漫画连载进入最终章", "漫画赏获奖公布")
        ]
        pool += [
            _subject_cand(ANIME_SRC, t, SUBJECT_LN, 110)
            for t in ("轻小说新刊情报", "轻小说动画化企划")
        ]
        pool += [
            _subject_cand(ANIME_SRC, t, SUBJECT_GAME, 100)
            for t in ("新作游戏发售日", "游戏改编电影", "游戏联动活动", "游戏更新补丁", "电竞比赛结果")
        ]
        selected, _ = select_quota(
            pool, 50, 0,
            {SUBJECT_ANIME: 10, SUBJECT_MANGA: 10, SUBJECT_LN: 10, SUBJECT_GAME: 10},
        )
        counts: dict[str, int] = {}
        for candidate in selected:
            subject = candidate.classification["category"]
            counts[subject] = counts.get(subject, 0) + 1
        # 三类不足 10 条的，池里有多少给多少（不强行凑数）
        assert counts[SUBJECT_MANGA] == 3
        assert counts[SUBJECT_LN] == 2
        assert counts[SUBJECT_GAME] == 5
        # 剩下的额度按权重回到动画，总数填满 50（不足不勉强，但这里候选够）
        assert counts[SUBJECT_ANIME] == 40
        assert len(selected) == 50

    def test_without_floor_animation_takes_everything(self):
        """关掉保底（category_floor=0）退回纯权重：动画把额度吃光。"""
        from scripts.news_daily import select_quota
        from scripts.news_classify import SUBJECT_ANIME, SUBJECT_MANGA

        pool = [_subject_cand(ANIME_SRC, t, SUBJECT_ANIME, 200) for t in ANIME_TITLES]
        pool += [
            _subject_cand(ANIME_SRC, t, SUBJECT_MANGA, 120)
            for t in ("漫画单行本发售", "漫画连载进入最终章")
        ]
        selected, _ = select_quota(pool, 20, 0, {})
        assert len(selected) == 20
        assert all(c.classification["category"] == SUBJECT_ANIME for c in selected)

    def test_final_run_stops_reserving(self, factory):
        """当天最后一轮（NEWS_FINAL_RUN_HOUR 起）不再预留，剩余额度按权重补满。"""
        fetch = fake_fetch({ANIME_SRC.url: RSS_ANIME_ONE})
        summary = run_import_daily(
            factory, apply=True, quota=10, run_quota=0, exempt_quota=0,
            category_floor=10, sources=(ANIME_SRC,), fetch=fetch,
            now=datetime(2026, 10, 23, 16, 30),
        )
        assert summary.final_run is True
        assert summary.category_deficit == {}

    def test_floor_keeps_fetching_later_categories(self, factory):
        """动画源一次就够配额时，仍会为没达标的漫画去抓源（否则保底永远落空）。"""
        manga = SourceSpec("zh-manga", "测试漫画源", "https://zh-manga.example/rss", "rss", "漫画", "zh")
        fetch = fake_fetch({
            ANIME_SRC.url: rss_items("https://anime.example", ANIME_TITLES[:6]),
            manga.url: rss_items("https://manga.example", ["漫画单行本发售", "漫画连载进入最终章"]),
        })
        summary = run_import_daily(
            factory, apply=True, quota=5, run_quota=0, exempt_quota=0,
            category_floor=2, sources=(ANIME_SRC, manga), fetch=fetch,
            now=datetime(2026, 10, 23, 10, 0),
        )
        # 动画 6 条已 ≥ 本轮额度 5，但因为漫画欠额未满足，漫画源照抓
        assert summary.per_source.get("zh-manga") == 2
        assert summary.added == 5

    def test_floor_off_restores_early_break(self, factory):
        """保底关闭时维持老优化：配额一够就停，不再为后面的类别多抓源。"""
        manga = SourceSpec("zh-manga", "测试漫画源", "https://zh-manga.example/rss", "rss", "漫画", "zh")
        fetch = fake_fetch({ANIME_SRC.url: rss_items("https://anime.example", ANIME_TITLES[:6])})
        summary = run_import_daily(
            factory, apply=True, quota=5, run_quota=0, exempt_quota=0,
            category_floor=0, sources=(ANIME_SRC, manga), fetch=fetch,
            now=datetime(2026, 10, 23, 10, 0),
        )
        assert summary.per_source.get("zh-manga") is None
        assert summary.added == 5


# ---------- 新源正文容器 + RSS 导语补全（2026-10-23） ----------
# 用户报「加的新源又没有文章详情」：详情页只有 feed 的一两句导语。两个成因——
# ① リスアニ！ / アニメ！アニメ！ 的正文容器不在旧词表里；
# ② parse_feed 把 description 塞进了 body，于是 `not candidate.body` 恒为假，详情页从不请求。

DETAIL_URL = "https://zh-anime.example/a1"

DETAIL_HTML = (
    '<html><body><div class="detail__wysiwyg parts__wysiwyg">'
    "<p>这是详情页里的正文第一段，长度足够长，用来证明导语会被替换成真正的正文内容。</p>"
    "<p>第二段继续说明活动的时间、地点与参与方式，全文明显超过正文阈值，不会被判成短讯。</p>"
    "</div></body></html>"
)


class TestNewSourceBodyContainers:
    def test_lisani_wysiwyg_container(self):
        """リスアニ！的 `div.detail__wysiwyg`（2026-10-23 实测容器）。"""
        html = (
            '<div class="detail__wrap"><div class="widget__ranking">排行</div>'
            '<div class="detail__wysiwyg parts__wysiwyg">'
            "<p>アニメレーベル“スターチャイルドレコード”発足から45年、歴代作品を振り返る特集。</p>"
            "<p>関係者へのインタビューも掲載している。</p></div></div>"
        )
        body = extract_body(html)
        assert "スターチャイルドレコード" in body
        assert "排行" not in body

    def test_animeanime_arti_body_container(self):
        """アニメ！アニメ！的 `article.arti-body`（旧代码靠 <article> 兜底才勉强命中）。"""
        html = (
            '<article class="arti-body cf cXenseParse editor-revolution">'
            "<p>『しろたん』の期間限定ポップアップショップが池袋で開催される。</p>"
            "<p>開催期間は2026年10月27日から11月9日まで。限定グッズの販売も予定している。</p>"
            "<p>入場は無料で、混雑時は整理券が配布される見込みだ。</p></article>"
        )
        body = extract_body(html)
        assert "ポップアップショップ" in body


class TestRssBodyHydration:
    def _feed(self, description: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0"><channel><title>动画源</title>'
            f"<item><title>动画新闻甲</title><link>{DETAIL_URL}</link>"
            '<enclosure url="https://zh-anime.example/i.jpg" type="image/jpeg"/>'
            f"<description>{description}</description></item>"
            "</channel></rss>"
        )

    def test_short_feed_description_is_replaced_by_detail(self, factory):
        """feed 只给一句导语时，抓详情页并用正文替换（原来直接拿导语当正文）。"""
        calls: list[str] = []

        def fetch(url: str) -> str:
            calls.append(url)
            if url == ANIME_SRC.url:
                return self._feed("动画相关的一句导语。")
            if url == DETAIL_URL:
                return DETAIL_HTML
            raise RuntimeError(f"未打桩：{url}")

        summary = run_import_daily(
            factory, apply=True, quota=5, run_quota=0, exempt_quota=0,
            category_floor=0, sources=(ANIME_SRC,), fetch=fetch,
        )
        assert summary.added == 1
        assert DETAIL_URL in calls
        db = factory()
        try:
            row = db.scalars(select(Article)).one()
        finally:
            db.close()
        assert "详情页里的正文第一段" in row.body

    def test_long_feed_description_skips_detail_fetch(self, factory):
        """feed 已经给全篇正文（超过阈值）时不多发一次详情请求。"""
        long_text = "本文正文。" * 60  # 300 字，超过 _RSS_FEED_BODY_MIN
        calls: list[str] = []

        def fetch(url: str) -> str:
            calls.append(url)
            if url == ANIME_SRC.url:
                return self._feed(long_text)
            raise RuntimeError(f"不该请求：{url}")

        summary = run_import_daily(
            factory, apply=True, quota=5, run_quota=0, exempt_quota=0,
            category_floor=0, sources=(ANIME_SRC,), fetch=fetch,
        )
        assert summary.added == 1
        assert DETAIL_URL not in calls


# ---------- 图片补全 / 装饰图 / 详情页留痕与限速（2026-10-25 三大症状修复） ----------


class TestImageBaseUrlAndDecoration:
    """2026-10-25：相对图片路径按页面 origin 补全 + 装饰图扩容 + 贴纸按位置保留。"""

    def test_relative_image_uses_page_origin(self):
        """详情页里的相对图片路径按页面 origin 补全。

        旧版兜底写死 Aniplex 域名：acgch 的 /mascot/sticker-pointing.webp、
        lnnews 的 /images/logo_x_white.png 全被补成 news.aniplex.co.jp/...（404），
        详情页正文里留一片裂图——用户报「抓不到图片」的主形态。
        """
        html = (
            '<div class="article-body">'
            "<p>正文第一段，长度足够通过最短校验，讲的是新角色公布与配音阵容情况。</p>"
            '<img src="/imgs/visual.jpg">'
            "<p>正文第二段，继续讲活动详情与发售信息，同样足够长通过校验。</p>"
            "</div>"
        )
        body = extract_body(html, "https://acgch.example/zh-hans/news/abc")
        assert "[图] https://acgch.example/imgs/visual.jpg" in body
        assert "aniplex" not in body

    def test_feed_relative_image_uses_feed_origin(self):
        """订阅源 description 里的相对图按 feed 地址的 origin 补全。"""
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<item><title>快讯</title><link>https://ln.example/articles/1</link>"
            "<description><![CDATA[<p>摘要文字。</p><img src=\"/images/cover.jpg\">]]></description>"
            "</item></channel></rss>"
        )
        spec = SourceSpec("ln", "LN", "https://ln.example/feed", "rss", "轻小说", "ja")
        items = parse_feed(rss, spec)
        assert items[0].image_url == "https://ln.example/images/cover.jpg"
        assert "[图] https://ln.example/images/cover.jpg" in items[0].body

    def test_hard_decorative_and_tracking_images_dropped(self):
        """logo / 图标 / Facebook 追踪像素 / data 占位一律丢（硬装饰）。"""
        html = (
            '<div class="article-body">'
            '<img src="/base/images/header-logo.png">'
            '<img src="https://www.facebook.com/tr?id=1&ev=PageView&noscript=1">'
            '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">'
            '<img src="/brand/acgch-logo-v5.png">'
            "<p>正文第一段，长度足够通过最短校验，介绍公布的新视觉图与角色阵容。</p>"
            '<img src="/medias/real-visual.jpg">'
            "<p>正文第二段，继续讲发售时间与平台信息，同样足够长通过校验。</p>"
            "</div>"
        )
        body = extract_body(html, "https://animeanime.example/article/1")
        assert "[图] https://animeanime.example/medias/real-visual.jpg" in body
        assert "header-logo" not in body
        assert "facebook.com/tr" not in body
        assert "data:image" not in body
        assert "logo-v5" not in body

    def test_soft_decoration_kept_only_inside_text(self):
        """表情贴纸（软装饰）按位置：孤立贴在首尾的丢，夹在文字中间的保留。"""
        html = (
            '<div class="article-body">'
            '<img src="/mascot/sticker-pointing.webp">'
            "<p>正文第一段，长度足够通过最短校验，介绍公布的新角色与视觉图。</p>"
            '<img src="/mascot/sticker-cheer.webp">'
            "<p>正文第二段，继续讲活动详情与发售信息，同样足够长通过校验。</p>"
            '<img src="/mascot/sticker-end.webp">'
            "</div>"
        )
        body = extract_body(html, "https://acgch.example/news/1")
        assert "sticker-cheer" in body
        assert "sticker-pointing" not in body
        assert "sticker-end" not in body


class TestDetailFailureAndPacing:
    """2026-10-25：详情页失败留痕 + 摘要剥离占位行 + 详情页限速。"""

    def test_detail_failure_is_counted_not_silent(self, factory):
        """详情页抓不到时计入 detail_failed 并留原因；源级 failed 语义不变。"""
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<item><title>快讯甲</title><link>https://paced.example/a1</link></item>"
            "<item><title>快讯乙</title><link>https://paced.example/a2</link></item>"
            "</channel></rss>"
        )
        fetch = fake_fetch({ZH_SOURCE.url: rss})  # 详情页一律未打桩 → 失败
        summary = run_import_daily(
            factory, apply=True, quota=10, run_quota=0, exempt_quota=0,
            category_floor=0, sources=(ZH_SOURCE,), fetch=fetch, sleep=lambda s: None,
        )
        assert summary.detail_failed == 2
        assert summary.failed == 0  # 源级抓取没失败，不混入 failed
        assert any("正文详情页失败" in d.get("reason", "") for d in summary.details)
        assert "正文详情页失败 2" in summary.per_source_stats[ZH_SOURCE.key]

    def test_summary_strips_placeholder_lines(self, factory):
        """desc 为空的源：summary 回填时剥掉 [图]/[媒体] 行，不以 URL 开头。"""
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<item><title>快讯丙</title><link>https://paced.example/c1</link></item>"
            "</channel></rss>"
        )
        page = (
            "<article>"
            '<img src="https://paced.example/cover.jpg">'
            "<p>这是详情页真正的第一段正文，内容足够长，能够通过最短长度校验，不会被当作提取失败。</p>"
            "<p>第二段：游戏将于2027年3月18日发售，登陆 Nintendo Switch 与 Steam 平台。</p>"
            "</article>"
        )
        fetch = fake_fetch({ZH_SOURCE.url: rss, "https://paced.example/c1": page})
        summary = run_import_daily(
            factory, apply=True, quota=10, run_quota=0, exempt_quota=0,
            category_floor=0, sources=(ZH_SOURCE,), fetch=fetch, sleep=lambda s: None,
        )
        db = factory()
        try:
            article = db.scalars(select(Article)).one()
        finally:
            db.close()
        assert article.summary.startswith("这是详情页真正的第一段正文")
        assert "[图]" not in article.summary

    def test_detail_fetch_is_paced(self, factory):
        """详情页之间按间隔限速（默认 1s）；sleep 可注入，测试不真等。"""
        rss = (
            '<?xml version="1.0"?><rss version="2.0"><channel>'
            "<item><title>快讯丁</title><link>https://paced.example/d1</link></item>"
            "<item><title>快讯戊</title><link>https://paced.example/d2</link></item>"
            "</channel></rss>"
        )
        page = (
            "<article><p>详情页正文第一段，长度足够通过最短长度校验，讲的是公布信息。</p>"
            "<p>第二段继续补充发售时间与平台，长度也足够通过校验。</p></article>"
        )
        fetch = fake_fetch(
            {
                ZH_SOURCE.url: rss,
                "https://paced.example/d1": page,
                "https://paced.example/d2": page,
            }
        )
        waited: list[float] = []
        summary = run_import_daily(
            factory, apply=True, quota=10, run_quota=0, exempt_quota=0,
            category_floor=0, sources=(ZH_SOURCE,), fetch=fetch,
            sleep=waited.append,
        )
        assert summary.added == 2
        # 两篇都抓了详情页 → 两次限速等待；feed 列表请求不计间隔
        assert waited == [news_daily.DETAIL_SLEEP_SECONDS] * 2


# ---------- 无限制源（2026-10-25 用户定稿：acgch 没有任何限制包括配额，全抓） ----------


class TestUnrestrictedSource:
    """SourceSpec.unrestricted：过滤链一层不走、配额不占、单源入池上限不适用。"""

    FREE_SOURCE = SourceSpec(
        "free-test", "无限制测试源", "https://free.example/rss", "rss",
        "综合动态", "zh", unrestricted=True,
    )
    # 三条都会被普通源的过滤层拒掉的稿子（硬广标记 / 抽奖导流 / VOD 导流，
    # 均为与类别无关的判据）。description 刻意超过 _RSS_FEED_BODY_MIN(240) 并带图，
    # 免得触发详情页抓取——本组测试只关心过滤与配额，不关心正文补全。
    _PAD = "活动细则以官方公告为准，最终解释权归主办方所有，详情留意后续更新说明。"
    JUNK_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>无限制源</title>
<item><title>【推广】普通游戏联名手柄发售决定</title><link>https://free.example/j1</link>
<description><![CDATA[<p>与普通游戏联名的纪念手柄开启预约。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://free.example/i1.jpg">]]></description></item>
<item><title>关注转发抽奖送福利</title><link>https://free.example/j2</link>
<description><![CDATA[<p>关注账号并转发即可参与抽奖，奖品是联名周边一套。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://free.example/i2.jpg">]]></description></item>
<item><title>新番見放題配信先比較指南</title><link>https://free.example/j3</link>
<description><![CDATA[<p>哪家平台能看新番，各大配信先横向比较。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://free.example/i3.jpg">]]></description></item>
</channel></rss>""".format(pad=_PAD)

    NORMAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>普通源</title>
<item><title>《星海机工》公布新角色与声优阵容</title><link>https://zh-anime.example/n1</link>
<description><![CDATA[<p>机甲动画星海机工公布新角色与声优，制作阵容同步公开。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://zh-anime.example/n1.jpg">]]></description></item>
<item><title>《治愈食堂》第二季制作决定</title><link>https://zh-anime.example/n2</link>
<description><![CDATA[<p>治愈系漫画改编动画第二季制作决定，预告片同步解禁。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://zh-anime.example/n2.jpg">]]></description></item>
<item><title>《荒野列车》剧场版上映日期确定</title><link>https://zh-anime.example/n3</link>
<description><![CDATA[<p>荒野列车剧场版上映日期确定，入场者特典信息公布。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://zh-anime.example/n3.jpg">]]></description></item>
</channel></rss>""".format(pad=_PAD)

    def _run(self, factory, sources, pages, **kwargs):
        params = dict(
            apply=True, quota=10, run_quota=0, exempt_quota=0,
            category_floor=0, sources=sources, fetch=fake_fetch(pages),
            sleep=lambda s: None,
        )
        params.update(kwargs)
        return run_import_daily(factory, **params)

    def test_acgch_is_the_only_unrestricted_source(self):
        """线上只有 acgch 一个无限制源（用户 2026-10-25 定稿），其它源行为不变。"""
        assert [spec.key for spec in SOURCES if spec.unrestricted] == ["acgch"]

    def test_junk_articles_pass_for_unrestricted_source(self, factory):
        """硬广 / 抽奖 / VOD 导流在无限制源里一条都不拦。"""
        summary = self._run(factory, (self.FREE_SOURCE,), {self.FREE_SOURCE.url: self.JUNK_RSS})
        assert summary.filtered == 0
        assert summary.added == 3

    def test_same_junk_is_filtered_for_normal_source(self, factory):
        """对照组：同样的稿子在普通源里全被拒——证明绕过是真的绕过了。"""
        summary = self._run(factory, (ZH_SOURCE,), {ZH_SOURCE.url: self.JUNK_RSS})
        assert summary.filtered == 3
        assert summary.added == 0

    def test_added_even_when_daily_quota_exhausted(self, factory):
        """日配额 0 也全量抓：早退与「配额满就丢非官宣」两个闸门都绕过。"""
        summary = self._run(
            factory, (self.FREE_SOURCE,), {self.FREE_SOURCE.url: self.JUNK_RSS}, quota=0,
        )
        assert summary.added == 3
        assert "无限制全量" in summary.per_source_stats[self.FREE_SOURCE.key]

    def test_does_not_consume_daily_quota(self, factory):
        """无限制源不扣 remaining：普通源照样拿满自己的配额，互不挤占。"""
        summary = self._run(
            factory, (ANIME_SRC, self.FREE_SOURCE),
            {ANIME_SRC.url: self.NORMAL_RSS, self.FREE_SOURCE.url: self.JUNK_RSS},
            quota=2,
        )
        assert summary.per_source.get("zh-anime") == 2  # 普通源：配额 2 条拿满即止
        assert summary.per_source.get("free-test") == 3  # 无限制源：3 条全进
        assert summary.added == 5

    def test_not_capped_by_per_source_pool_cap(self, factory):
        """单源入池上限（needed×0.34）不适用于无限制源。"""
        # quota=3、3 个源 → source_cap = ceil(3×0.34) = 2；无限制源 3 条候选应全入池。
        one = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>源</title>
<item><title>动画条目</title><link>https://zh-anime.example/c1</link>
<description><![CDATA[<p>某动画公布新视觉图与放送日程。</p><p>{pad}{pad}{pad}{pad}{pad}{pad}{pad}{pad}</p>
<img src="https://zh-anime.example/c1.jpg">]]></description></item>
</channel></rss>""".format(pad=self._PAD)
        game = (
            one.replace("动画条目", "游戏条目")
            .replace("zh-anime.example", "zh-game.example")
            .replace("某动画公布新视觉图与放送日程", "某游戏公布新版本内容与上线时间")
        )
        summary = self._run(
            factory, (ANIME_SRC, GAME_SRC, self.FREE_SOURCE),
            {
                ANIME_SRC.url: one,
                GAME_SRC.url: game,
                self.FREE_SOURCE.url: self.JUNK_RSS,
            },
            quota=3,
        )
        assert summary.per_source.get("free-test") == 3
        assert summary.added == 5  # 2 条普通（配额内）+ 3 条无限制
