"""资讯图片：解析占位、本地化、页面渲染与回填 CLI 的离线测试。

覆盖：
- 解析：图片转 ``[图]`` 占位行、相对地址补全、数量上限、装饰图跳过；
  列表缩略图成头图、列表没有时回退正文首图；
- 正文行结构：``[图]``/``[媒体]`` 提取与改写、块解析；
- 本地化：魔术字节校验、大小上限、落盘路径、单张失败保留外链；
- 导入集成：apply 时改写正文与头图，预览不下载；
- 页面：详情页渲染 <img>、列表页缩略图；
- 回填 CLI：预览不写库、apply 更新、已有图跳过、非详情页来源跳过。

全部用例注入假 fetch / 假 downloader，绝不访问真实网络，也不触碰开发库。
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import article_body, article_media, crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Article
from article_helpers import detail_page, list_item, list_page, make_site
from fastapi.testclient import TestClient
from scripts import backfill_article_images as bai
from scripts import import_articles as ia

SITE = ia.SITE_BASE
NEWS_URL = f"{SITE}/?category=news"

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _site_factory(tmp_path, name="images.db"):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False), engine


# ---------- 解析：占位与头图 ----------


class TestParsing:
    def test_absolute_image_url_variants(self):
        assert ia.absolute_image_url("SYS/CONTENTS/x") == f"{SITE}/SYS/CONTENTS/x"
        assert ia.absolute_image_url("/SYS/CONTENTS/x") == f"{SITE}/SYS/CONTENTS/x"
        assert ia.absolute_image_url("//cdn.example.com/x.jpg") == "https://cdn.example.com/x.jpg"
        assert ia.absolute_image_url("https://a.example/x") == "https://a.example/x"
        assert ia.absolute_image_url("") == ""

    def test_img_becomes_placeholder_line(self):
        text = ia.html_to_text(
            '<p>开场说明。</p><img src="SYS/CONTENTS/uuid-1" alt=""><p>后续说明。</p>'
        )
        assert text == f"开场说明。\n[图] {SITE}/SYS/CONTENTS/uuid-1\n后续说明。"

    def test_image_count_limit(self):
        body = "".join(f'<img src="SYS/CONTENTS/u{i}">' for i in range(ia.MAX_BODY_IMAGES + 3))
        urls = article_body.extract_image_urls(ia.html_to_text(body))
        assert len(urls) == ia.MAX_BODY_IMAGES

    def test_decorative_and_broken_images_skipped_without_quota(self):
        body = (
            '<img src="assets/siteinfo/logo.svg">'
            '<img alt="没有 src">'
            '<img src="SYS/CONTENTS/real">'
        )
        urls = article_body.extract_image_urls(ia.html_to_text(body))
        assert urls == [f"{SITE}/SYS/CONTENTS/real"]

    def test_list_page_thumb_url(self):
        html = list_page([list_item(71493, "Oct 16, 2026", "标题")])
        entry = ia.parse_list_page(html)[0]
        assert entry.thumb_url == f"{SITE}/SYS/CONTENTS/keyvisual_71493"

    def test_detail_head_image_prefers_list_thumb(self):
        entry = ia.parse_list_page(list_page([list_item(7, "Oct 16, 2026", "标题")]))[0]
        html = detail_page(
            "标题", "Oct 16, 2026",
            '<img src="SYS/CONTENTS/body-1"><p>正文</p>',
        )
        item = ia.parse_detail_page(html, entry)
        assert item.thumb_url == f"{SITE}/SYS/CONTENTS/keyvisual_7"
        assert f"{SITE}/SYS/CONTENTS/body-1" in article_body.extract_image_urls(item.body)

    def test_detail_head_image_falls_back_to_first_body_image(self):
        entry = ia.NewsEntry(
            detail_id="7", url=f"{SITE}/detail.html?id=7", title="标题",
            published_at=__import__("datetime").datetime.now(),
        )
        html = detail_page("标题", "Oct 16, 2026", '<img src="SYS/CONTENTS/body-1"><p>正文</p>')
        item = ia.parse_detail_page(html, entry)
        assert item.thumb_url == f"{SITE}/SYS/CONTENTS/body-1"


# ---------- 正文行结构 ----------


class TestBodyBlocks:
    def test_extract_replaces_roundtrip(self):
        body = "[图] https://x/1.jpg\n说明\n[图] https://x/2.jpg\n[图] https://x/1.jpg"
        urls = article_body.extract_image_urls(body)
        assert urls == ["https://x/1.jpg", "https://x/2.jpg"]
        replaced = article_body.replace_image_urls(body, {"https://x/1.jpg": "/article-images/a.jpg"})
        assert "[图] /article-images/a.jpg" in replaced
        assert "[图] https://x/2.jpg" in replaced  # 未命中的保留外链

    def test_parse_blocks(self):
        blocks = article_body.parse_blocks("第一段\n\n[图] /a.jpg\n[媒体] https://v/1\n第二段")
        assert blocks == [
            {"kind": "text", "text": "第一段"},
            {"kind": "image", "url": "/a.jpg"},
            {"kind": "media", "url": "https://v/1"},
            {"kind": "text", "text": "第二段"},
        ]


# ---------- 本地化 ----------


class TestLocalize:
    @pytest.fixture
    def image_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "article_image_storage_dir", str(tmp_path))
        return tmp_path

    def test_save_rejects_non_image(self, image_dir):
        with pytest.raises(article_media.ArticleImageError):
            article_media.save_article_image(b"<html>not an image</html>", key="7", index=1)

    def test_save_rejects_too_large(self, image_dir, monkeypatch):
        monkeypatch.setattr(settings, "article_image_max_bytes", 16)
        with pytest.raises(article_media.ArticleImageError):
            article_media.save_article_image(JPEG_BYTES, key="7", index=1)

    def test_save_writes_file_and_returns_site_path(self, image_dir):
        url = article_media.save_article_image(JPEG_BYTES, key="7", index=2)
        assert url == "/article-images/article_7_2.jpg"
        assert (image_dir / "article_7_2.jpg").read_bytes() == JPEG_BYTES

    def test_name_sanitized(self, image_dir):
        url = article_media.save_article_image(PNG_BYTES, key="../../etc/pass", index=1)
        name = url.removeprefix("/article-images/")
        # 路径分隔符与点全部被替换，不可能穿越目录
        assert "/" not in name and ".." not in name and "\\" not in name
        assert name.startswith("article_") and name.endswith("_1.png")
        assert (image_dir / name).exists()

    def test_localize_rewrites_success_and_keeps_failures(self):
        body = "[图] https://x/ok.jpg\n说明\n[图] https://x/bad.jpg"
        seen = []

        def downloader(url, key, index):
            seen.append((url, key, index))
            if "bad" in url:
                raise article_media.ArticleImageError("403")
            return f"/article-images/article_{key}_{index}.jpg"

        result = article_media.localize_media(
            body, "https://x/head.jpg", key="9", downloader=downloader
        )
        assert result.saved == 2 and result.failed == 1
        # 头图占第 1 个序号，正文图从第 2 个起（同一份编号，避免同名覆盖）
        assert "[图] /article-images/article_9_2.jpg" in result.body
        assert "[图] https://x/bad.jpg" in result.body
        assert result.head_image == "/article-images/article_9_1.jpg"
        assert seen[0][0] == "https://x/head.jpg"

    def test_localize_respects_disabled_flag(self):
        """全局关闭下载时原样返回（测试环境的默认状态）。"""
        body = "[图] https://x/a.jpg"
        result = article_media.localize_media(body, "https://x/h.jpg", key="1")
        assert result.body == body and result.head_image == "https://x/h.jpg"
        assert result.saved == 0 and result.failed == 0


# ---------- 导入集成 ----------


class TestImportIntegration:
    def test_apply_localizes_images(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        monkeypatch.setattr(settings, "article_image_download_enabled", True)
        monkeypatch.setattr(settings, "article_image_storage_dir", str(tmp_path))
        calls = []

        def fake_download(url, *, key, index, timeout=0, opener=None):
            calls.append((url, key))
            return article_media.save_article_image(JPEG_BYTES, key=key, index=index)

        monkeypatch.setattr(article_media, "download_image", fake_download)

        site = make_site(
            {NEWS_URL: list_page([list_item(55, "Oct 16, 2026", "带图资讯")])},
            {55: detail_page("带图资讯", "Oct 16, 2026", '<p>正文</p><img src="SYS/CONTENTS/u-1">')},
        )
        summary = ia.run_import_articles(
            factory, apply=True, category="动漫资讯", limit=1, fetch=site
        )
        assert summary.added == 1
        db = factory()
        try:
            article = db.scalars(select(Article)).one()
            assert article.image_url == "/article-images/article_55_1.jpg"  # 列表缩略图
            assert f"[图] {SITE}/SYS/CONTENTS/u-1" not in article.body
            assert "[图] /article-images/article_55_2.jpg" in article.body
            assert len(calls) == 2
        finally:
            db.close()
            engine.dispose()

    def test_preview_does_not_download(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        monkeypatch.setattr(settings, "article_image_download_enabled", True)

        def explode(*args, **kwargs):
            raise AssertionError("预览不应下载图片")

        monkeypatch.setattr(article_media, "download_image", explode)
        site = make_site(
            {NEWS_URL: list_page([list_item(56, "Oct 16, 2026", "预览资讯")])},
            {56: detail_page("预览资讯", "Oct 16, 2026", '<img src="SYS/CONTENTS/u-2">')},
        )
        summary = ia.run_import_articles(
            factory, apply=False, category="动漫资讯", limit=1, fetch=site
        )
        assert summary.pending == 1
        assert _count_articles(factory) == 0
        engine.dispose()


def _count_articles(factory) -> int:
    db = factory()
    try:
        return len(list(db.scalars(select(Article))))
    finally:
        db.close()


# ---------- 页面渲染 ----------


@pytest.fixture
def page_env(tmp_path):
    factory, engine = _site_factory(tmp_path, "pages.db")
    db = factory()
    try:
        db.add(
            Article(
                title="带图资讯标题",
                category="动漫资讯",
                summary="摘要",
                body="开场说明\n[图] /article-images/article_1_1.jpg\n[媒体] https://v.example/1",
                image_url="/article-images/article_1_1.jpg",
                source="ANIPLEX NEWS BOX",
                original_url=f"{SITE}/detail.html?id=1",
            )
        )
        db.commit()
    finally:
        db.close()

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


class TestRendering:
    def test_detail_renders_image_and_media_blocks(self, page_env):
        body = page_env.get("/articles/1").text
        assert 'src="/article-images/article_1_1.jpg"' in body
        assert 'class="article-hero"' in body
        assert 'class="article-figure"' in body
        assert "[媒体]" in body and 'href="https://v.example/1"' in body
        assert "开场说明" in body

    def test_list_shows_thumbnail(self, page_env):
        body = page_env.get("/articles").text
        assert 'class="article-card-thumb"' in body
        assert "/article-images/article_1_1.jpg" in body


# ---------- 回填 CLI ----------


class TestBackfill:
    def _seed(self, factory, *, body="旧正文（没有图）", image_url=None, url_id="77"):
        db = factory()
        try:
            article = Article(
                title="旧资讯",
                category="动漫资讯",
                summary="旧摘要",
                body=body,
                image_url=image_url,
                source="ANIPLEX NEWS BOX",
                original_url=f"{SITE}/detail.html?id={url_id}",
            )
            db.add(article)
            db.commit()
            return article.id
        finally:
            db.close()

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        article_id = self._seed(factory)
        fetch = make_site(
            {},
            {77: detail_page("旧资讯", "Oct 16, 2026", '<img src="SYS/CONTENTS/b-1"><p>新正文</p>')},
        )
        summary = bai.run_backfill(
            factory, apply=False, fetch=fetch, sleep=lambda s: None
        )
        assert summary.pending == 1 and summary.images == 0
        db = factory()
        try:
            article = db.get(Article, article_id)
            assert article.body == "旧正文（没有图）"
            assert article.image_url is None
        finally:
            db.close()
            engine.dispose()

    def test_apply_updates_body_and_image(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        article_id = self._seed(factory)
        monkeypatch.setattr(settings, "article_image_storage_dir", str(tmp_path))
        fetch = make_site(
            {},
            {77: detail_page("旧资讯", "Oct 16, 2026", '<img src="SYS/CONTENTS/b-1"><p>新正文</p>')},
        )
        summary = bai.run_backfill(
            factory, apply=True, fetch=fetch, sleep=lambda s: None,
            downloader=lambda url, key, index: f"/article-images/article_{key}_{index}.jpg",
        )
        assert summary.updated == 1 and summary.images == 1
        db = factory()
        try:
            article = db.get(Article, article_id)
            assert "[图] /article-images/article_77_1.jpg" in article.body
            assert article.image_url == "/article-images/article_77_1.jpg"
        finally:
            db.close()
            engine.dispose()

    def test_skips_articles_with_existing_images(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        self._seed(factory, body="[图] /article-images/x.jpg", image_url="/article-images/x.jpg")

        def explode(*args, **kwargs):
            raise AssertionError("已有图片不应抓取")

        summary = bai.run_backfill(
            factory, apply=True, fetch=explode, sleep=lambda s: None,
        )
        assert summary.skipped == 1
        engine.dispose()

    def test_skips_non_detail_source(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        db = factory()
        try:
            db.add(
                Article(
                    title="活动资讯",
                    category="活动资讯",
                    summary="摘要",
                    body="标题\n\n原文：https://example.com/event",
                    source="ANIPLEX NEWS BOX",
                    original_url="https://example.com/event",
                )
            )
            db.commit()
        finally:
            db.close()

        def explode(*args, **kwargs):
            raise AssertionError("非站内详情页不应抓取")

        summary = bai.run_backfill(factory, apply=True, fetch=explode, sleep=lambda s: None)
        assert summary.skipped == 1
        engine.dispose()

    def test_no_image_keeps_original_body(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        article_id = self._seed(factory)
        fetch = make_site({}, {77: detail_page("旧资讯", "Oct 16, 2026", "<p>纯文字</p>")})
        summary = bai.run_backfill(factory, apply=True, fetch=fetch, sleep=lambda s: None)
        assert summary.no_image == 1 and summary.updated == 0
        db = factory()
        try:
            assert db.get(Article, article_id).body == "旧正文（没有图）"
        finally:
            db.close()
            engine.dispose()

    def test_cli_reports_missing_article(self, tmp_path, monkeypatch):
        factory, engine = _site_factory(tmp_path)
        monkeypatch.setattr(
            bai, "build_engine", lambda url: engine
        )
        exit_code = bai.main(["--article-id", "999", "--database-url", "sqlite://"])
        assert exit_code == bai.EXIT_NO_MATCH
        engine.dispose()
