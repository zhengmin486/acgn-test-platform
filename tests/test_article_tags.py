"""资讯自动识别测试：识别算法、落库、导入集成、回填 CLI 与页面渲染。

覆盖：
- 纯函数：字段权重打分、大小写、最小匹配长度、上限截断、同分稳定序、
  空词表、ip_name 额外加分（互为子串）；
- crud：replace_article_tags / list_article_tags / article_tags_map 的
  卫生规则、上限、幂等替换与清空；replace_article_content_links 的
  得分序保持、同标题多作品全关联、清空；
- 导入集成：apply 后标签与关联落库、重复导入不堆叠、预览不写识别结果、
  「作品名／」前缀在正文未提及时也能关联作品；
- 回填 CLI：预览不写库、--apply 写入、--force 重识别、--article-id 过滤、
  退出码（0 正常 / 1 指定 ID 无匹配 / 2 用法错误）；
- 页面：资讯详情页标签 chips 与关联作品卡（含空态）、列表页 chips。

全部用例用独立临时 SQLite 与假站点，不访问网络、不触碰开发库。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import article_tags, crud
from app.database import Base, get_db
from app.main import app
from app.models import Article, ArticleContentLink, ArticleTag, Content, Tag
from article_helpers import detail_page, list_item, list_page, make_site
from scripts import import_articles as ia

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "link_articles.py"

# scripts/ 不是包，按文件路径加载被测 CLI 模块（与直接运行脚本等价）。
_spec = importlib.util.spec_from_file_location("link_articles_for_tests", SCRIPT_PATH)
link_articles = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = link_articles
_spec.loader.exec_module(link_articles)

SITE = ia.SITE_BASE
NEWS_URL = f"{SITE}/?category=news"


# ---------- 夹具与工具 ----------


@pytest.fixture
def factory(tmp_path):
    """独立临时库：两部作品 + 两个标签（只建表不播种，词表干净）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'article_tags.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        db.add(Content(title="机甲动画甲", type="anime", summary="机甲与热血的动画。", rating=8.0))
        db.add(Content(title="治愈漫画乙", type="comic", summary="治愈系漫画。", rating=7.5))
        db.commit()
        crud.replace_content_tags(db, 1, ["机甲", "热血"])
        crud.replace_content_tags(db, 2, ["治愈"])
    finally:
        db.close()
    try:
        yield session_factory
    finally:
        engine.dispose()


@pytest.fixture
def page_env(factory):
    """页面用例环境：TestClient + get_db 覆盖 + 作品 id 映射。"""
    db = factory()
    try:
        content_ids = {row.title: row.id for row in db.query(Content).order_by(Content.id)}
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
        yield SimpleNamespace(
            session_factory=factory,
            client=TestClient(app, follow_redirects=False),
            content_ids=content_ids,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _rows(factory, model) -> int:
    db = factory()
    try:
        return db.scalar(select(func.count()).select_from(model)) or 0
    finally:
        db.close()


def _write_tags(factory, article_id: int, names: list[str]) -> list[str]:
    """写识别标签并提交（crud 侧不自行 commit，由调用方统一事务边界）。"""
    db = factory()
    try:
        written = crud.replace_article_tags(db, article_id, names)
        db.commit()
        return written
    finally:
        db.close()


def _write_links(factory, article_id: int, titles: list[str]):
    """写关联作品并提交。"""
    db = factory()
    try:
        works = crud.replace_article_content_links(db, article_id, titles)
        db.commit()
        return works
    finally:
        db.close()


def _add_article(factory, *, title, body, summary="", published=True) -> int:
    """直接写一篇资讯，返回其 id。"""
    db = factory()
    try:
        article = Article(
            title=title,
            category="动漫资讯",
            summary=summary or f"{title} 的摘要",
            body=body,
            source=ia.SOURCE_NAME,
            original_url=f"{SITE}/detail.html?id={abs(hash(title)) % 10**6}",
            is_published=published,
        )
        db.add(article)
        db.commit()
        return article.id
    finally:
        db.close()


def _tagged_site(*, detail_ids=(201, 202)):
    """假站点：201 正文提及作品与标签；202 只有「作品名／」前缀、正文不提作品。"""
    items = [
        list_item(201, "Oct 16, 2026", "机甲动画甲新情报！", ip="机甲动画甲"),
        list_item(202, "Oct 16, 2026", "全新视觉图公开", ip="治愈漫画乙"),
    ]
    details = {
        201: detail_page(
            "机甲动画甲新情报！",
            "Oct 16, 2026",
            "<p>本期介绍机甲动画甲的机甲设定与热血剧情。</p>",
        ),
        202: detail_page(
            "全新视觉图公开",
            "Oct 16, 2026",
            "<p>全新视觉图与预告影片解禁。</p>",
        ),
    }
    return make_site({NEWS_URL: list_page(items)}, details)


# ---------- 纯函数：打分与匹配 ----------


class TestScoreTerm:
    def test_weights_accumulate_by_field(self):
        """标题 3 / 摘要 2 / 正文 1 加权累加。"""
        assert article_tags.score_term("机甲", title="机甲", summary="机甲", body="机甲") == 6
        assert article_tags.score_term("机甲", body="机甲 机甲") == 2

    def test_case_insensitive(self):
        assert article_tags.score_term("Fate", title="fate grand order") == 3

    def test_fullwidth_symbols_are_normalized(self):
        """全角/半角归一（NFKC）：『孤独摇滚！』命中『孤独摇滚!』。"""
        assert article_tags.score_term("孤独摇滚！", title="孤独摇滚! 开播") == 3
        assert article_tags.score_term("ＡＢＣ", body="abc abc") == 2

    def test_too_short_terms_never_match(self):
        """长度不足 MIN_MATCH_LENGTH 的词不得分（单字噪声防护）。"""
        assert article_tags.score_term("甲", title="机甲动画甲") == 0
        assert article_tags.score_term("", title="机甲") == 0


class TestMatchTags:
    def test_ranks_by_score_and_caps(self):
        """按得分排序并截断到上限；同分按名称字典序保证稳定。"""
        names = article_tags.match_tags(
            title="热血机甲",
            summary="",
            body="机甲 机甲 治愈",
            tag_names=["治愈", "机甲", "热血", "恋爱", "日常"],
        )
        # 机甲 3+2=5、热血 3、治愈 1；上限 5 个以内全收
        assert names == ["机甲", "热血", "治愈"]

    def test_respects_limit(self):
        tag_names = [f"标签{index}" for index in range(8)]
        body = " ".join(tag_names)
        matched = article_tags.match_tags(body=body, tag_names=tag_names, limit=3)
        assert len(matched) == 3

    def test_empty_vocabulary_and_no_hit(self):
        assert article_tags.match_tags(title="机甲", tag_names=[]) == []
        assert article_tags.match_tags(title="完全无关的文本", tag_names=["机甲"]) == []

    def test_vocabulary_dedup_and_blank(self):
        """词表重复项与空白项不重复计分。"""
        names = article_tags.match_tags(
            body="机甲", tag_names=["机甲", "机甲", "  ", ""]
        )
        assert names == ["机甲"]


class TestMatchContentTitles:
    def test_title_hit_outranks_body_hit(self):
        titles = article_tags.match_content_titles(
            title="机甲动画甲", body="治愈漫画乙 治愈漫画乙",
            content_titles=["治愈漫画乙", "机甲动画甲"],
        )
        assert titles == ["机甲动画甲", "治愈漫画乙"]

    def test_extra_name_matches_without_text_mention(self):
        """正文没提作品名，但「作品名／」前缀与词表词互为子串：加分命中。"""
        titles = article_tags.match_content_titles(
            title="全新视觉图公开",
            body="全新视觉图与预告影片解禁。",
            content_titles=["治愈漫画乙", "机甲动画甲"],
            extra_names=["治愈漫画乙"],
        )
        assert titles == ["治愈漫画乙"]

    def test_short_extra_name_is_ignored(self):
        """过短的额外信号不加分（与最小匹配长度同一防护）。"""
        titles = article_tags.match_content_titles(
            title="甲", body="正文",
            content_titles=["机甲动画甲"],
            extra_names=["甲"],
        )
        assert titles == []

    def test_extra_boosts_but_text_spam_can_outrank(self):
        """前缀加分是强信号，但正文反复提及仍可反超（权重设计意图）。"""
        titles = article_tags.match_content_titles(
            title="某作品",
            body="机甲动画甲 " * 10,
            content_titles=["机甲动画甲", "治愈漫画乙"],
            extra_names=["治愈漫画乙"],
        )
        assert titles[0] == "机甲动画甲"

    def test_limit_and_empty(self):
        assert article_tags.match_content_titles(title="x", content_titles=[]) == []
        titles = [f"作品{index}" for index in range(8)]
        matched = article_tags.match_content_titles(
            title=" ".join(titles), content_titles=titles, limit=2
        )
        assert len(matched) == 2


# ---------- crud：落库与查询 ----------


class TestArticleTagCrud:
    def test_replace_and_list(self, factory):
        article_id = _add_article(factory, title="资讯一", body="正文")
        assert _write_tags(factory, article_id, ["机甲", "热血"]) == ["机甲", "热血"]
        db = factory()
        try:
            assert crud.list_article_tags(db, article_id) == ["机甲", "热血"]
        finally:
            db.close()

    def test_hygiene_rules_and_cap(self, factory):
        """空白/超长名丢弃、去重保序、最多 MAX_ARTICLE_TAGS 个。"""
        article_id = _add_article(factory, title="资讯二", body="正文")
        names = [f"标签{index}" for index in range(7)]
        db = factory()
        try:
            written = crud.replace_article_tags(
                db, article_id, ["", "  ", "长" * 81, *names, "标签0"]
            )
            assert written == names[:crud.MAX_ARTICLE_TAGS]
            assert len(written) == 5
        finally:
            db.close()

    def test_replace_is_idempotent(self, factory):
        article_id = _add_article(factory, title="资讯三", body="正文")
        _write_tags(factory, article_id, ["机甲", "热血"])
        assert _write_tags(factory, article_id, ["治愈"]) == ["治愈"]
        db = factory()
        try:
            assert crud.list_article_tags(db, article_id) == ["治愈"]
        finally:
            db.close()
        assert _rows(factory, ArticleTag) == 1

    def test_empty_list_clears(self, factory):
        article_id = _add_article(factory, title="资讯四", body="正文")
        _write_tags(factory, article_id, ["机甲"])
        assert _write_tags(factory, article_id, []) == []
        db = factory()
        try:
            assert crud.list_article_tags(db, article_id) == []
        finally:
            db.close()

    def test_tags_map_batches_without_n_plus_one(self, factory):
        first = _add_article(factory, title="资讯五", body="正文")
        second = _add_article(factory, title="资讯六", body="正文")
        _write_tags(factory, first, ["机甲"])
        _write_tags(factory, second, ["治愈", "热血"])
        db = factory()
        try:
            mapping = crud.article_tags_map(db, [first, second])
            assert mapping == {first: ["机甲"], second: ["治愈", "热血"]}
            assert crud.article_tags_map(db, []) == {}
            assert first not in crud.article_tags_map(db, [second])
        finally:
            db.close()


class TestArticleContentLinkCrud:
    def test_link_and_list_preserves_score_order(self, factory):
        """传入标题按得分序，返回作品保持该顺序（同标题多作品全关联）。"""
        article_id = _add_article(factory, title="资讯七", body="正文")
        works = _write_links(factory, article_id, ["治愈漫画乙", "机甲动画甲"])
        assert [w.title for w in works] == ["治愈漫画乙", "机甲动画甲"]
        db = factory()
        try:
            works = crud.list_article_contents(db, article_id)
            assert [w.title for w in works] == ["治愈漫画乙", "机甲动画甲"]
            assert crud.count_article_content_links(db, article_id) == 2
        finally:
            db.close()

    def test_replace_clears_old_links(self, factory):
        article_id = _add_article(factory, title="资讯八", body="正文")
        _write_links(factory, article_id, ["机甲动画甲", "治愈漫画乙"])
        works = _write_links(factory, article_id, ["治愈漫画乙"])
        assert [w.title for w in works] == ["治愈漫画乙"]
        assert _rows(factory, ArticleContentLink) == 1
        assert _write_links(factory, article_id, []) == []
        db = factory()
        try:
            assert crud.list_article_contents(db, article_id) == []
        finally:
            db.close()

    def test_unknown_titles_link_nothing(self, factory):
        article_id = _add_article(factory, title="资讯九", body="正文")
        db = factory()
        try:
            assert crud.replace_article_content_links(db, article_id, ["不存在的作品"]) == []
            db.commit()
            assert crud.count_article_content_links(db, article_id) == 0
        finally:
            db.close()


# ---------- 编排：recognize / apply_recognition / link_articles ----------


class TestRecognitionOrchestration:
    def test_recognize_does_not_write(self, factory):
        article_id = _add_article(
            factory, title="机甲动画甲新情报", body="机甲设定与热血剧情。"
        )
        db = factory()
        try:
            article = db.get(Article, article_id)
            recognition = article_tags.recognize(
                article,
                tag_names=crud.list_all_tag_names(db),
                content_titles=[t for _, t in crud.list_content_titles(db)],
            )
            assert recognition.tags == ["机甲", "热血"]
            assert recognition.content_titles == ["机甲动画甲"]
        finally:
            db.close()
        assert _rows(factory, ArticleTag) == 0
        assert _rows(factory, ArticleContentLink) == 0

    def test_apply_recognition_writes_without_commit(self, factory):
        """apply_recognition 只写不提交；调用方可见未提交数据（同事务）。"""
        article_id = _add_article(
            factory, title="机甲动画甲新情报", body="机甲设定与热血剧情。"
        )
        db = factory()
        try:
            article = db.get(Article, article_id)
            recognition = article_tags.recognize(
                article,
                tag_names=crud.list_all_tag_names(db),
                content_titles=[t for _, t in crud.list_content_titles(db)],
            )
            article_tags.apply_recognition(db, recognition)
            # 同一会话内可读到自己写的关联（尚未 commit）
            assert crud.list_article_tags(db, article_id) == ["机甲", "热血"]
            db.rollback()
        finally:
            db.close()
        assert _rows(factory, ArticleTag) == 0

    def test_link_articles_batch(self, factory):
        first = _add_article(factory, title="机甲动画甲新情报", body="机甲与热血。")
        second = _add_article(factory, title="无关资讯", body="今天天气不错。")
        db = factory()
        try:
            results = article_tags.link_articles(db, [db.get(Article, first), db.get(Article, second)])
            assert results[first].tags == ["机甲", "热血"]
            assert results[second].tags == []
        finally:
            db.close()


# ---------- 导入集成 ----------


class TestImportIntegration:
    def test_apply_writes_tags_and_links(self, factory):
        summary = ia.run_import_articles(factory, apply=True, fetch=_tagged_site())
        assert summary.added == 2
        assert summary.recognized == 2

        db = factory()
        try:
            articles = {a.title: a for a in db.scalars(select(Article))}
            first = articles["机甲动画甲新情报！"]
            assert crud.list_article_tags(db, first.id) == ["机甲", "热血"]
            assert [w.title for w in crud.list_article_contents(db, first.id)] == ["机甲动画甲"]

            # 正文没提作品名，靠「作品名／」前缀关联
            second = articles["全新视觉图公开"]
            assert crud.list_article_tags(db, second.id) == []
            assert [w.title for w in crud.list_article_contents(db, second.id)] == ["治愈漫画乙"]
        finally:
            db.close()

    def test_reimport_does_not_stack_rows(self, factory):
        ia.run_import_articles(factory, apply=True, fetch=_tagged_site())
        again = ia.run_import_articles(factory, apply=True, fetch=_tagged_site())
        assert (again.added, again.existing) == (0, 2)
        assert _rows(factory, ArticleTag) == 2  # 机甲、热血（第二篇无标签）
        assert _rows(factory, ArticleContentLink) == 2

    def test_preview_writes_no_recognition(self, factory):
        preview = ia.run_import_articles(factory, fetch=_tagged_site())
        assert preview.mode == "preview"
        assert preview.recognized == 0
        assert _rows(factory, Article) == 0
        assert _rows(factory, ArticleTag) == 0
        assert _rows(factory, ArticleContentLink) == 0

    def test_empty_vocabulary_leaves_no_recognition(self, factory):
        """作品库为空（无标签无作品）时识别自然无结果，不报错。"""
        db = factory()
        try:
            db.execute(Tag.__table__.delete())
            db.execute(Content.__table__.delete())
            db.commit()
        finally:
            db.close()
        summary = ia.run_import_articles(factory, apply=True, fetch=_tagged_site())
        assert summary.added == 2
        assert _rows(factory, ArticleTag) == 0
        assert _rows(factory, ArticleContentLink) == 0


# ---------- 回填 CLI ----------


def _seed_articles(factory) -> list[int]:
    """三篇资讯：两篇有可识别内容、一篇完全无关。"""
    return [
        _add_article(factory, title="机甲动画甲新情报", body="机甲设定与热血剧情。"),
        _add_article(factory, title="治愈漫画乙联动", body="治愈漫画乙推出联动活动。"),
        _add_article(factory, title="无关资讯", body="今天天气不错。"),
    ]


class TestBackfillCli:
    def test_preview_recognizes_without_writing(self, factory):
        _seed_articles(factory)
        summary = link_articles.run_backfill(factory, apply=False)
        assert summary.mode == "preview"
        assert summary.requested == 3
        # 两篇有识别结果（待识别），一篇完全无关（无需写入）
        assert summary.pending == 2
        assert summary.skipped == 1
        assert summary.recognized == 0
        assert summary.tagged == 2
        assert summary.linked == 2
        assert _rows(factory, ArticleTag) == 0
        assert _rows(factory, ArticleContentLink) == 0

    def test_apply_writes(self, factory):
        ids = _seed_articles(factory)
        summary = link_articles.run_backfill(factory, apply=True)
        assert summary.recognized == 2
        assert _rows(factory, ArticleTag) == 3  # 机甲、热血、治愈
        assert _rows(factory, ArticleContentLink) == 2

        db = factory()
        try:
            assert crud.list_article_tags(db, ids[0]) == ["机甲", "热血"]
            assert [w.title for w in crud.list_article_contents(db, ids[0])] == ["机甲动画甲"]
            assert crud.list_article_tags(db, ids[2]) == []
        finally:
            db.close()

    def test_second_run_skips_recognized(self, factory):
        _seed_articles(factory)
        link_articles.run_backfill(factory, apply=True)
        again = link_articles.run_backfill(factory, apply=True)
        assert again.recognized == 0
        # 两篇已有识别结果 + 一篇识别结果为空，都跳过
        assert again.skipped == 3
        assert _rows(factory, ArticleTag) == 3

    def test_force_reruns_and_stays_idempotent(self, factory):
        _seed_articles(factory)
        link_articles.run_backfill(factory, apply=True)
        forced = link_articles.run_backfill(factory, apply=True, force=True)
        assert forced.recognized == 2
        assert forced.skipped == 1  # 空识别的那篇无需重写
        # 替换式写入：重跑不堆叠行
        assert _rows(factory, ArticleTag) == 3
        assert _rows(factory, ArticleContentLink) == 2

    def test_article_id_filter(self, factory):
        ids = _seed_articles(factory)
        summary = link_articles.run_backfill(factory, apply=True, article_ids=[ids[0]])
        assert summary.requested == 1
        assert summary.recognized == 1
        assert _rows(factory, ArticleTag) == 2

    def test_main_exit_codes(self, factory):
        ids = _seed_articles(factory)
        # 预览退出码 0
        assert link_articles.main([], session_factory=factory) == link_articles.EXIT_OK
        # 正式回填退出码 0
        assert (
            link_articles.main(["--apply"], session_factory=factory)
            == link_articles.EXIT_OK
        )
        # 指定存在的文章：退出码 0
        assert (
            link_articles.main(["--apply", "--article-id", str(ids[0])], session_factory=factory)
            == link_articles.EXIT_OK
        )
        # 指定不存在的文章：退出码 1
        assert (
            link_articles.main(["--article-id", "9999"], session_factory=factory)
            == link_articles.EXIT_NO_MATCH
        )
        # 非正整数 ID：用法错误退出码 2
        assert (
            link_articles.main(["--article-id", "0"], session_factory=factory)
            == link_articles.EXIT_USAGE
        )


# ---------- 页面渲染 ----------


class TestArticlePages:
    def test_detail_page_shows_tags_and_linked_works(self, page_env):
        factory = page_env.session_factory
        article_id = _add_article(
            factory, title="机甲动画甲新情报", body="机甲设定与热血剧情。"
        )
        db = factory()
        try:
            article_tags.apply_recognition(
                db,
                article_tags.recognize(
                    db.get(Article, article_id),
                    tag_names=crud.list_all_tag_names(db),
                    content_titles=[t for _, t in crud.list_content_titles(db)],
                ),
            )
            db.commit()
        finally:
            db.close()

        resp = page_env.client.get(f"/articles/{article_id}")
        assert resp.status_code == 200
        assert 'href="/contents?tag=' in resp.text
        assert "识别标签" in resp.text
        assert "机甲" in resp.text and "热血" in resp.text
        assert "关联作品" in resp.text
        work_id = page_env.content_ids["机甲动画甲"]
        assert f'href="/contents/{work_id}"' in resp.text
        assert "机甲动画甲" in resp.text
        assert "暂未关联到作品" not in resp.text

    def test_detail_page_empty_states(self, page_env):
        article_id = _add_article(
            page_env.session_factory, title="无关资讯", body="今天天气不错。"
        )
        resp = page_env.client.get(f"/articles/{article_id}")
        assert resp.status_code == 200
        assert "暂未关联到作品" in resp.text
        assert 'class="tag-chip"' not in resp.text
        assert "识别标签" not in resp.text

    def test_list_page_shows_tag_chips(self, page_env):
        factory = page_env.session_factory
        tagged = _add_article(factory, title="机甲动画甲新情报", body="机甲与热血。")
        _add_article(factory, title="无关资讯", body="今天天气不错。")
        db = factory()
        try:
            article_tags.apply_recognition(
                db,
                article_tags.recognize(
                    db.get(Article, tagged),
                    tag_names=crud.list_all_tag_names(db),
                    content_titles=[t for _, t in crud.list_content_titles(db)],
                ),
            )
            db.commit()
        finally:
            db.close()

        resp = page_env.client.get("/articles")
        assert resp.status_code == 200
        assert 'href="/contents?tag=' in resp.text
        assert "机甲" in resp.text
        # 无识别标签的资讯不渲染 chips（页面只有一处 chip 组：机甲 + 热血）
        assert resp.text.count('class="tag-chip"') == 2
