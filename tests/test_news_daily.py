"""多源每日资讯管线测试：RSS/RDF 解析、游民星空列表/详情、每日配额与翻译口径。

原则与既有测试一致：全部打桩（假 fetch / 假翻译），不触网；
数据库用独立临时 SQLite；资讯图片下载在 conftest 里全局关闭（保留外链）。
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import crud, news_llm
from app.config import settings
from app.database import build_engine
from app.models import Article
from scripts.news_daily import run_import_daily
from scripts.news_sources import (
    SOURCES,
    SourceSpec,
    fetch_candidates,
    parse_feed,
    parse_feed_date,
    parse_gamersky_detail,
    parse_gamersky_list,
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
        assert items[0].published_at == datetime(2026, 10, 19, 12, 0)

    def test_atom_items(self):
        """Atom 源（轻小说 ln-news）按 <entry> 解析，链接取 rel=alternate，封面取 enclosure 图片。"""
        spec = SourceSpec("ln", "ラノベニュースオンライン", "https://ln-news.com/feed", "rss", "轻小说", "ja")
        items = parse_feed(ATOM_LN, spec)
        assert len(items) == 2
        assert items[0].title == "『弱気MAX令嬢』のオンラインくじが登場"
        assert items[0].url == "https://ln-news.com/articles/126906"
        assert items[0].image_url == "https://assets.ln-news.com/m.jpg"
        # 带时区（+09:00）的 updated 必须正确解析为 naive 本地时间
        assert items[0].published_at == datetime(2026, 10, 20, 16, 0)
        # 第二条没有 enclosure 封面，回落为空（不报错）
        assert items[1].url == "https://ln-news.com/articles/126986"
        assert items[1].image_url is None

    def test_feed_date_formats(self):
        assert parse_feed_date("Mon, 19 Oct 2026 23:00:00 +0800") == datetime(2026, 10, 19, 23, 0)
        assert parse_feed_date("2026-10-19T12:00:00+09:00") == datetime(2026, 10, 19, 12, 0)
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
    def test_zh_sources_imported_and_ja_skipped_without_llm(self, factory):
        fetch = fake_fetch({
            ZH_SOURCE.url: RSS_ZH,
            JA_SOURCE.url: RSS_JA,
        })
        summary = run_import_daily(
            factory, apply=True, quota=20, sources=(ZH_SOURCE, JA_SOURCE), fetch=fetch
        )
        assert summary.added == 2
        assert summary.skipped_sources == 1  # 没有翻译 Key：日文源整源跳过
        assert summary.per_source == {"zh-test": 2}

        db = factory()
        try:
            rows = {row.title: row for row in db.scalars(select(Article))}
            assert set(rows) == {"新动画宣布制作", "旧闻一条"}
            body = rows["新动画宣布制作"].body
            assert "原文链接：https://zh.example/a1" in body
        finally:
            db.close()

    def test_daily_quota_caps_total(self, factory):
        fetch = fake_fetch({ZH_SOURCE.url: RSS_ZH})
        first = run_import_daily(
            factory, apply=True, quota=1, sources=(ZH_SOURCE,), fetch=fetch
        )
        assert first.added == 1

        # 当天配额用完：第二轮直接停（新增为 0），不重复入库
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
        summary = run_import_daily(
            factory, apply=True, quota=1, sources=(JA_SOURCE, ZH_SOURCE),
            fetch=fetch, translate=translate,
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
    def test_subject_cap_prevents_one_category_taking_all(self):
        """综合动态有封顶，不会挤占全部配额（兼顾类别覆盖）。"""
        from scripts.news_daily import _PER_SUBJECT_CAP, select_quota

        general = SourceSpec(
            "g", "综合源", "https://g.example/rss", "rss", "综合动态", "zh"
        )
        pool = [_cand(general, f"综合资讯第 {i} 条") for i in range(20)]
        selected, _ = select_quota(pool, 10)
        assert len(selected) == _PER_SUBJECT_CAP["综合动态"]

    def test_same_event_reports_are_merged(self):
        """同一事件的不同报道只占一个位置。"""
        from scripts.news_daily import select_quota

        src = SourceSpec("a", "动画源", "https://a.example/rss", "rss", "动画", "zh")
        pool = [
            _cand(src, "新作アニメ「ホシ」制作決定、2027年放送"),
            _cand(src, "新作アニメ「ホシ」制作決定、2027年放送へ"),
        ]
        selected, deduped = select_quota(pool, 10)
        assert len(selected) == 1
        assert deduped == 1

    def test_higher_priority_wins_when_duplicated(self):
        """同事件去重保留优先级更高的那条（日本报道优先于欧美报道）。"""
        from scripts.news_daily import select_quota

        jp = SourceSpec("jp", "日本源", "https://jp.example/rss", "rss", "游戏", "zh", region="日本")
        west = SourceSpec("w", "欧美源", "https://w.example/rss", "rss", "游戏", "zh", region="欧美")
        title = "新作ゲーム発売決定、2027年春に配信"
        pool = [_cand(west, title), _cand(jp, title)]
        selected, _ = select_quota(pool, 10)
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
