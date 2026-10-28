"""backfill_article_bodies 守护测试（2026-10-25：错域名图重提取 + 本地化对齐导入链路）。

背景：多源导入初期 ``absolute_image_url`` 没有页面 base_url 概念，相对路径图片
全被补成 Aniplex 域名 → 线上 135 条非 Aniplex 源文章正文里混着 404 裂图，
且正文都不短，与「短正文」集合完全不重叠——回填脚本因此多了一条专用口径。
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Article
from scripts import backfill_article_bodies as bab

ACGCH_PAGE = (
    '<div class="article-body">'
    "<p>第一段正文，长度足够通过最短校验，讲的是新角色公布与配音阵容的情况。</p>"
    '<img src="/api/media/file/key-1024-1200x675.webp">'
    "<p>第二段正文，继续讲活动详情与发售信息，同样足够长通过校验。</p>"
    "</div>"
)

# 错域名口径要求新正文 ≥ MIN_BODY(200)：长页面（四段 + 一张相对路径图）
LONG_PAGE = (
    '<div class="article-body">'
    "<p>第一段正文，讲的是新角色公布与配音阵容，信息量足够通过最短长度校验，讲发售信息。</p>"
    "<p>第二段正文，继续讲活动详情与票务安排，舞台活动将于冬季举办，同样足够长通过校验。</p>"
    '<img src="/api/media/file/key-1024-1200x675.webp">'
    "<p>第三段正文，补充预约特典与价格信息，限定版本包含设定集与特典光盘，长度也足够。</p>"
    "<p>第四段正文，最后说明平台与语言支持情况，确保整体长度超过两百字的阈值线。</p>"
    "</div>"
)

_LONG_BODY = (
    "第一段足够长的正文内容，讲的是游戏发售信息与平台详情，长度超过两百字。"
    "第二段继续补充预约特典与价格信息，同样足够长，确保不会被短正文规则覆盖。"
    "第三段再补充一些发行商与开发商信息，让整体长度稳稳超过两百字的阈值线。"
    "第四段继续说明语言与版本信息，确保这篇旧正文足够长，不会被判定为退化摘要。"
    "第五段补充上市时间与预载安排，第六段补充媒体评分与玩家试玩反馈情况。"
)


@pytest.fixture
def factory(tmp_path):
    """独立临时库：只建表不播种。yield (session_factory, database_url)。"""
    url = f"sqlite:///{tmp_path / 'backfill_bodies.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session_factory, url
    finally:
        engine.dispose()


def _add_article(db, **kwargs) -> Article:
    fields = {
        "title": "回填测试资讯",
        "category": "动漫资讯",
        "summary": "摘要",
        "body": "短摘要。",
        "source": "ACGCH",
        "original_url": "https://acgch.example/zh-hans/news/abc",
    }
    fields.update(kwargs)
    article = Article(**fields)
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def _fake_fetch(pages: dict[str, str]):
    def fetch(url: str) -> str:
        if url not in pages:
            raise OSError(f"未打桩的地址：{url}")
        return pages[url]

    return fetch


class TestWrongDomainImages:
    """2026-10-25：非 Aniplex 源正文混进 Aniplex 域名图 → 单独一条回填口径。"""

    def test_long_body_with_wrong_domain_is_recrawled(self, factory):
        """正文不短但混着错域名图：也要重抓（与短正文集合不重叠）。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(
                db,
                source="4Gamer",
                title="长正文错域名图",
                body=(
                    _LONG_BODY
                    + "\n[图] https://news.aniplex.co.jp/article/images/zzz.jpg"
                ),
                original_url="https://www.4gamer.net/g/063/063852/20251024001/",
            )
        finally:
            db.close()
        pages = {article.original_url: LONG_PAGE}
        stats = bab.run(
            url,
            apply=True, sleep=0, fetch=_fake_fetch(pages),
        )
        assert stats["candidates"] == 1
        assert stats["changed"] == 1
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert "news.aniplex.co.jp" not in row.body
            # 页面挂在 4gamer 的详情页 URL 下 → 相对图按 4gamer origin 补全
            assert "[图] https://www.4gamer.net/api/media/file/key-1024-1200x675.webp" in row.body
        finally:
            db.close()

    def test_aniplex_source_with_own_domain_not_touched(self, factory):
        """Aniplex 自己的文章用这个域名合法：长正文 + 自有域名不动。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(
                db,
                source="ANIPLEX NEWS BOX",
                title="Aniplex 正统文章",
                body=(
                    _LONG_BODY
                    + "\n[图] https://news.aniplex.co.jp/article/images/own.jpg"
                ),
                original_url="https://news.aniplex.co.jp/detail.html?id=1",
            )
        finally:
            db.close()
        stats = bab.run(
            url,
            apply=True, sleep=0, fetch=_fake_fetch({}),
        )
        assert stats["candidates"] == 0
        assert stats["tried"] == 0

    def test_short_body_still_uses_gain_rule(self, factory):
        """短正文口径不变：提取结果要明显更长才替换。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(db, body="太短。")
        finally:
            db.close()
        stats = bab.run(
            url,
            apply=True, sleep=0, fetch=_fake_fetch({article.original_url: ACGCH_PAGE}),
        )
        assert stats["changed"] == 1
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert "news.aniplex.co.jp" not in row.body
            assert "[图] https://acgch.example/api/media/file/key-1024-1200x675.webp" in row.body
        finally:
            db.close()

    def test_clean_but_too_short_replacement_rejected(self, factory):
        """错域名文章重抓后提取结果不完整（<200 字）：保留旧正文，不越换越差。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(
                db,
                source="ACGCH",
                title="换不出更好正文的",
                body=(
                    "旧正文足够长，讲的是发售信息与平台详情，长度超过两百字达标线。"
                    "第二段继续补充预约特典与价格信息，同样足够长，确保不会被短正文规则覆盖。"
                    "\n[图] https://news.aniplex.co.jp/article/images/zzz.jpg"
                ),
            )
        finally:
            db.close()
        thin_page = '<div class="article-body"><p>只有一句话的残页。</p></div>'
        stats = bab.run(
            url,
            apply=True, sleep=0, fetch=_fake_fetch({article.original_url: thin_page}),
        )
        assert stats["changed"] == 0
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert "news.aniplex.co.jp" in row.body  # 原样保留
        finally:
            db.close()


class TestLocalizeAlignment:
    """回填本地化与导入链路对齐：key 同名、头图只在还是外链时才下载。"""

    def test_localize_key_matches_import(self, factory):
        """key = md5(original_url)[:16]，与 news_daily._media_key 一致（文件名对齐）。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(db, body="太短。")
        finally:
            db.close()
        seen: list[tuple[str, str]] = []

        def downloader(url, key, index):
            seen.append((url, key))
            return f"/article-images/article_{key}_{index}.webp"

        bab.run(
            url,
            apply=True, sleep=0, downloader=downloader,
            fetch=_fake_fetch({article.original_url: ACGCH_PAGE}),
        )
        expected_key = hashlib.md5(article.original_url.encode("utf-8")).hexdigest()[:16]
        assert seen
        assert all(key == expected_key for _, key in seen)
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert f"article_{expected_key}_1.webp" in row.body
        finally:
            db.close()

    def test_local_head_image_not_redownloaded(self, factory):
        """头图已是站内路径：不下载、不改写（避免把本地路径当外链抓）。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(
                db, body="太短。", image_url="/article-images/article_old_1.jpg",
            )
        finally:
            db.close()
        seen: list[str] = []

        def downloader(url, key, index):
            seen.append(url)
            return f"/article-images/article_{key}_{index}.webp"

        bab.run(
            url,
            apply=True, sleep=0, downloader=downloader,
            fetch=_fake_fetch({article.original_url: ACGCH_PAGE}),
        )
        assert "/article-images/article_old_1.jpg" not in seen
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert row.image_url == "/article-images/article_old_1.jpg"
        finally:
            db.close()

    def test_external_head_image_localized(self, factory):
        """头图还是外链：一起下载并改写为站内路径。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(
                db, body="太短。",
                image_url="https://acgch.example/api/media/file/og-1200x630.jpg",
            )
        finally:
            db.close()
        seen: list[str] = []

        def downloader(url, key, index):
            seen.append(url)
            return f"/article-images/article_{key}_{index}.jpg"

        bab.run(
            url,
            apply=True, sleep=0, downloader=downloader,
            fetch=_fake_fetch({article.original_url: ACGCH_PAGE}),
        )
        assert "https://acgch.example/api/media/file/og-1200x630.jpg" in seen
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert row.image_url.startswith("/article-images/article_")
        finally:
            db.close()

    def test_download_failure_keeps_external_url(self, factory):
        """下载失败不外链也不能裂：占位行保留源站地址，正文照样写入。"""
        session_factory, url = factory
        db = session_factory()
        try:
            article = _add_article(db, body="太短。")
        finally:
            db.close()

        def downloader(url, key, index):
            raise OSError("源站拒绝")

        stats = bab.run(
            url,
            apply=True, sleep=0, downloader=downloader,
            fetch=_fake_fetch({article.original_url: ACGCH_PAGE}),
        )
        assert stats["changed"] == 1
        assert stats["localize_failed"] >= 1
        db = factory[0]()
        try:
            row = db.get(Article, article.id)
            assert "[图] https://acgch.example/api/media/file/key-1024-1200x675.webp" in row.body
        finally:
            db.close()
