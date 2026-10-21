"""资讯自动识别测试：识别算法、落库、导入集成、回填 CLI 与页面渲染。

覆盖：
- 纯函数：字段权重打分、大小写、最小匹配长度、上限截断、同分稳定序、
  空词表、ip_name 额外加分（互为子串）；
- 作品信息（2026-10-20 新增）：实体 / 系列 / 译名词表分组、二级补充
  关联（低于标题命中、不挤占直接命中名额）、译名按标题权重、展示标签
  截断；
- crud：replace_article_tags / list_article_tags / article_tags_map 的
  卫生规则、上限、幂等替换与清空；replace_article_content_links 的
  得分序保持、清空；replace_article_entities / list_article_entities /
  count_article_entities 与 list_work_info_terms 词表；
- 导入集成：apply 后标签与关联落库、重复导入不堆叠、预览不写识别结果、
  「作品名／」前缀在正文未提及时也能关联作品；
- 回填 CLI：预览不写库、--apply 写入、--force 重识别、--article-id 过滤、
  退出码（0 正常 / 1 指定 ID 无匹配 / 2 用法错误）；
- 页面：资讯详情页标签 chips、作品信息 chips 与关联作品卡（含空态）、
  列表页 chips。

全部用例用独立临时 SQLite 与假站点，不访问网络、不触碰开发库。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app import article_tags, crud
from app.database import Base, get_db
from app.main import app
from app.models import (
    Article,
    ArticleContentLink,
    ArticleEntity,
    ArticleTag,
    Content,
    ContentEntityLink,
    Entity,
    Tag,
)
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
def wi_factory(factory):
    """作品信息词表环境：在基础词表上追加公司/作者实体、系列与译名。

    命名刻意避开既有测试文本（机甲/热血/治愈/视觉图/天气），保证旧用例
    不受新词表影响：
    - 河村动画（公司）→ 机甲动画甲（动画制作）+ 治愈漫画乙（发行）；
    - 林澈（作者）→ 机甲动画甲（原作）；
    - 机甲宇宙（系列）→ 机甲动画甲；
    - 机甲动画甲的译名：機甲動畫甲 / Mecha-Kou。
    """
    db = factory()
    try:
        mecha = db.scalar(select(Content).where(Content.title == "机甲动画甲"))
        heal = db.scalar(select(Content).where(Content.title == "治愈漫画乙"))
        studio = Entity(name="河村动画", entity_type="company")
        author = Entity(name="林澈", entity_type="person")
        db.add_all([studio, author])
        db.flush()
        db.add_all(
            [
                ContentEntityLink(content_id=mecha.id, entity_id=studio.id, role="动画制作"),
                ContentEntityLink(content_id=heal.id, entity_id=studio.id, role="发行"),
                ContentEntityLink(content_id=mecha.id, entity_id=author.id, role="原作"),
            ]
        )
        mecha.title_original = "機甲動畫甲"
        mecha.aliases = ["Mecha-Kou"]
        db.commit()
        crud.set_content_series(db, mecha.id, "机甲宇宙")
    finally:
        db.close()
    return factory


@pytest.fixture
def wi_terms(wi_factory):
    """作品信息词表（build_info_terms 产物，供纯函数用例直接取用）。"""
    db = wi_factory()
    try:
        return article_tags.build_info_terms(crud.list_work_info_terms(db))
    finally:
        db.close()


@pytest.fixture
def wi_page_env(wi_factory):
    """作品信息页面用例环境：TestClient + get_db 覆盖 + 作品 id 映射。"""
    db = wi_factory()
    try:
        content_ids = {row.title: row.id for row in db.query(Content).order_by(Content.id)}
    finally:
        db.close()

    def override_get_db():
        session = wi_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SimpleNamespace(
            session_factory=wi_factory,
            client=TestClient(app, follow_redirects=False),
            content_ids=content_ids,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


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
    """写关联作品并提交（标题先解析成库内作品对象，与识别链路同口径）。"""
    db = factory()
    try:
        works = []
        for title in titles:
            content = db.scalar(
                select(Content).where(Content.title == title).order_by(Content.id)
            )
            if content is not None:
                works.append(content)
        written = crud.replace_article_content_links(db, article_id, works)
        db.commit()
        return written
    finally:
        db.close()


def _add_article(factory, *, title, body, summary="", published=True, ip_name=None) -> int:
    """直接写一篇资讯，返回其 id。"""
    db = factory()
    try:
        article = Article(
            title=title,
            category="动漫资讯",
            summary=summary or f"{title} 的摘要",
            body=body,
            ip_name=ip_name,
            source=ia.SOURCE_NAME,
            original_url=f"{SITE}/detail.html?id={abs(hash(title)) % 10**6}",
            is_published=published,
        )
        db.add(article)
        db.commit()
        return article.id
    finally:
        db.close()


def _set_published_at(factory, article_id: int, when: datetime) -> None:
    """改一篇资讯的发布时间（相关资讯排序 / 优先级用例用）。"""
    db = factory()
    try:
        article = db.get(Article, article_id)
        article.published_at = when
        db.commit()
    finally:
        db.close()


def _recognize(factory, article_id: int) -> None:
    """跑识别并提交（与导入 / 回填同一入口 load_vocabulary → recognize）。"""
    db = factory()
    try:
        tag_names, title_terms, info_terms = article_tags.load_vocabulary(db)
        article_tags.apply_recognition(
            db,
            article_tags.recognize(
                db.get(Article, article_id),
                tag_names=tag_names,
                title_terms=title_terms,
                info_terms=info_terms,
            ),
        )
        db.commit()
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

    def test_ascii_words_require_word_boundary(self):
        """纯 ASCII 短词要求词边界：「graniph_official」不得命中作品《OFF》。"""
        assert article_tags.score_term("OFF", body="graniph_official") == 0
        assert article_tags.score_term("OFF", body="offer office offsite") == 0
        # 独立出现（含标点/空格分隔）才算命中
        assert article_tags.score_term("OFF", body="全品 20% OFF 实施") == 1
        assert article_tags.score_term("BEM", title="BEM 第2话") == 3
        # 中日文词仍按子串匹配（无空格语言不适用词边界）
        assert article_tags.score_term("机甲", body="机甲动画甲") == 1

    def test_url_and_base64_are_stripped_before_matching(self):
        """URL 与长 base64 串不参与匹配（线上 air / I/O 误关联的根因）。"""
        assert article_tags.score_term("AIR", body="https://abema.tv/now-on-air/anime-live") == 0
        blob = "Kkl8y5YlI8GFdf++acHFeefNGX5OihahHtoeuoGToWkpxi/o+7rBrdnKDp+xRcwUnSstiY9Cw"
        assert article_tags.score_term("I/O", body=blob) == 0
        # 剔除 URL 后正文里的正常文字仍然参与匹配（正文权重 1）
        assert article_tags.score_term("AIR", body="https://x.example/a 剧场版 AIR 公开") == 1


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

    def test_numeric_tags_ignored(self):
        """纯数字标签不参与识别：'10' 会命中正文里的日期（10月/第10话），必须挡掉。

        Tag 表里混进了 '10'、'01'、'201504' 这类历史遗留名，曾把 80 多篇资讯
        全打上「10」这种毫无意义的标签。
        """
        names = article_tags.match_tags(
            title="10月新番开播",
            body="10月1日开播，第10话已上线",
            tag_names=["10", "01", "0_", "新番"],
        )
        assert names == ["新番"]

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

    def test_single_char_title_cannot_ride_extra_boost(self):
        """单字作品标题（心）不得靠 ip_name 子串加分上榜。

        线上真实误关联：ip_name「楽園追放 心のレゾナンス」把作品《心》加进
        关联列表（「心」in extra 恒真），绕过了 MIN_MATCH_LENGTH 下限。
        """
        titles = article_tags.match_content_titles(
            title="剧场版公开纪念",
            body="剧场版公开纪念影像解禁。",
            content_titles=["心", "乐园追放 -Expelled From Paradise-"],
            extra_names=["楽園追放 心のレゾナンス"],
        )
        assert "心" not in titles

    def test_extra_boosts_but_text_spam_can_outrank(self):
        """前缀加分是强信号，但正文反复提及仍可反超（权重设计意图）。"""
        titles = article_tags.match_content_titles(
            title="某作品",
            body="机甲动画甲 " * 10,
            content_titles=["机甲动画甲", "治愈漫画乙"],
            extra_names=["治愈漫画乙"],
        )
        assert titles[0] == "机甲动画甲"

    def test_body_single_hit_is_not_enough(self):
        """正文只提 1 次的常用词作品名不关联（「地下2階」不该命中作品《地下》）。"""
        assert article_tags.match_content_titles(
            title="联动活动举办",
            body="羽田空港第3ターミナル駅 地下2階1番線改札外で開催。",
            content_titles=["地下"],
        ) == []
        # 标题命中（3 分）或正文多次提及（>=2 分）才关联
        assert article_tags.match_content_titles(
            title="作品地下 新情报", body="正文", content_titles=["地下"]
        ) == ["地下"]
        assert article_tags.match_content_titles(
            title="联动活动", body="地下 地下", content_titles=["地下"]
        ) == ["地下"]

    def test_short_ascii_title_needs_headline_hit(self):
        """纯 ASCII 短作品名（OFF）正文多次出现也不关联，除非标题/摘要命中。

        线上实例：体验设施说明里的「振動OFF」出现 3 次，把作品《OFF》挂到了
        魔法使いの夜 的资讯上。
        """
        assert article_tags.match_content_titles(
            title="体验型展览举办",
            body="振動OFFでの体験 振動OFF 振動OFF",
            content_titles=["OFF"],
        ) == []
        assert article_tags.match_content_titles(
            title="OFF 联名活动开幕", body="正文", content_titles=["OFF"]
        ) == ["OFF"]
        # ip_name 明确指向该作品时不受此限（extra 强信号）
        assert article_tags.match_content_titles(
            title="体验型展览举办",
            body="正文",
            content_titles=["OFF"],
            extra_names=["OFF"],
        ) == ["OFF"]

    def test_limit_and_empty(self):
        assert article_tags.match_content_titles(title="x", content_titles=[]) == []
        titles = [f"作品{index}" for index in range(8)]
        matched = article_tags.match_content_titles(
            title=" ".join(titles), content_titles=titles, limit=2
        )
        assert len(matched) == 2


# ---------- 作品信息：词表分组 / 二级补充关联 / 展示标签 ----------


class TestBuildInfoTerms:
    def test_groups_rows_and_dedupes_targets(self):
        """同一 (name, kind) 归一成一个词表项；同作品多角色不去重计分目标。"""
        rows = [
            ("河村动画", "entity", "company", 1, "机甲动画甲", 8.0),
            ("河村动画", "entity", "company", 1, "机甲动画甲", 8.0),  # 多角色重复行
            ("河村动画", "entity", "company", 2, "治愈漫画乙", 7.5),
            ("机甲宇宙", "series", None, 1, "机甲动画甲", 8.0),
            ("Mecha-Kou", "alias", None, 1, "机甲动画甲", 8.0),
        ]
        terms = {term.name: term for term in article_tags.build_info_terms(rows)}
        assert set(terms) == {"河村动画", "机甲宇宙", "Mecha-Kou"}
        studio = terms["河村动画"]
        assert studio.kind == "entity"
        assert studio.entity_type == "company"
        assert studio.targets == ((1, "机甲动画甲", 8.0), (2, "治愈漫画乙", 7.5))
        assert terms["机甲宇宙"].kind == "series"
        assert terms["Mecha-Kou"].kind == "alias"
        assert terms["Mecha-Kou"].targets == ((1, "机甲动画甲", 8.0),)

    def test_blank_names_dropped(self):
        terms = article_tags.build_info_terms(
            [("  ", "entity", "company", 1, "x", 1.0), ("", "series", None, 1, "x", 1.0)]
        )
        assert terms == []


class TestMatchWorkInfo:
    """作品信息增强（二级补充关联 + 展示标签）。"""

    TITLE_TERMS = [(1, "机甲动画甲"), (2, "治愈漫画乙")]

    def test_entity_title_hit_associates_without_title_mention(self, wi_terms):
        """标题只提公司名：经 content_entity_links 反查补关联（二级）。"""
        matches = article_tags.match_article_works(
            title="河村动画 新作发表",
            body="详细情报近日公开。",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        # 两部作品同分（标题 2），按评分降序：机甲动画甲(8.0) 在前
        assert [(m.title, m.tier) for m in matches] == [
            ("机甲动画甲", 2),
            ("治愈漫画乙", 2),
        ]

    def test_entity_single_body_hit_does_not_associate(self, wi_terms):
        """正文单次提及公司（1 分）低于关联门槛：只展示、不关联。"""
        matches = article_tags.match_article_works(
            title="某活动举办",
            body="本场活动由河村动画协力赞助。",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        assert matches == []

    def test_entity_repeated_body_hit_associates(self, wi_terms):
        """正文反复提及（2 次 x 1 分）达到门槛才补关联。"""
        matches = article_tags.match_article_works(
            title="活动举办",
            body="河村动画出品 河村动画制作",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        assert [m.title for m in matches] == ["机甲动画甲", "治愈漫画乙"]
        assert all(m.tier == 2 for m in matches)

    def test_direct_title_hit_ranks_before_entity_only(self, wi_terms):
        """一级（标题命中）永远排在二级（实体补充）之前，且实体分可叠加。"""
        matches = article_tags.match_article_works(
            title="机甲动画甲第二季",
            body="河村动画制作 河村动画出品",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        assert matches[0].title == "机甲动画甲"
        assert matches[0].tier == 1
        assert matches[0].score == 5  # 标题 3 + 实体正文 2
        assert matches[1].title == "治愈漫画乙"
        assert matches[1].tier == 2

    def test_entity_hit_does_not_displace_direct_match(self, wi_terms):
        """保守口径：实体补充不挤占直接命中的名额（limit=1 时只留直接命中）。"""
        matches = article_tags.match_article_works(
            title="机甲动画甲第二季",
            body="河村动画制作 河村动画出品",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
            limit=1,
        )
        assert [m.title for m in matches] == ["机甲动画甲"]

    def test_alias_hit_counts_as_direct(self, wi_terms):
        """译名是作品自身的名字：按标题权重计，属一级命中。"""
        matches = article_tags.match_article_works(
            title="Mecha-Kou 情报解禁",
            body="",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        assert [(m.title, m.tier) for m in matches] == [("机甲动画甲", 1)]

    def test_alias_single_body_hit_does_not_associate(self, wi_terms):
        matches = article_tags.match_article_works(
            title="某情报",
            body="详见 Mecha-Kou 条目。",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        assert matches == []

    def test_series_hit_associates(self, wi_terms):
        """系列名命中同系列作品（二级）。"""
        matches = article_tags.match_article_works(
            title="机甲宇宙 完全设定集发售",
            body="",
            title_terms=self.TITLE_TERMS,
            info_terms=wi_terms,
        )
        assert [(m.title, m.tier) for m in matches] == [("机甲动画甲", 2)]

    def test_short_ascii_entity_needs_headline_hit(self):
        """纯 ASCII 短实体名（KEY）只认标题/摘要：正文普通用法不算数。"""
        terms = [
            article_tags.WorkInfoTerm(
                name="KEY", kind="entity", entity_type="company",
                targets=((1, "某游戏", 8.0),),
            )
        ]
        assert (
            article_tags.match_article_works(
                title="体验报告",
                body="press key 多次 key key",
                title_terms=[(1, "某游戏")],
                info_terms=terms,
            )
            == []
        )
        matches = article_tags.match_article_works(
            title="KEY 新作公布",
            body="",
            title_terms=[(1, "某游戏")],
            info_terms=terms,
        )
        assert [m.title for m in matches] == ["某游戏"]

    def test_display_terms_carry_kind_and_score(self, wi_terms):
        """展示标签带 kind / entity_type；实体与译名都进来，按分数降序。"""
        items = article_tags.match_work_info(
            title="河村动画与林澈 对谈特番",
            body="",
            info_terms=wi_terms,
        )
        by_name = {item.name: item for item in items}
        assert set(by_name) == {"河村动画", "林澈"}
        assert by_name["河村动画"].kind == "entity"
        assert by_name["河村动画"].entity_type == "company"
        assert by_name["河村动画"].score == 2  # 标题 1 次 x 2
        assert by_name["林澈"].entity_type == "person"

    def test_display_alias_carries_content_id(self, wi_terms):
        items = article_tags.match_work_info(
            title="Mecha-Kou 情报", body="", info_terms=wi_terms
        )
        assert len(items) == 1
        assert items[0].kind == "alias"
        assert items[0].content_id == 1  # 机甲动画甲

    def test_display_shows_below_association_threshold(self, wi_terms):
        """展示不设 MIN_LINK_SCORE：正文提及一次的作者也出 chips。"""
        items = article_tags.match_work_info(
            title="活动举办", body="林澈 签字会", info_terms=wi_terms
        )
        assert [item.name for item in items] == ["林澈"]
        assert items[0].score == 1

    def test_display_truncates_to_cap(self):
        """超过 MAX_ARTICLE_ENTITIES 个作品信息按得分截断。"""
        terms = [
            article_tags.WorkInfoTerm(
                name=f"作者{index:02d}",
                kind="entity",
                entity_type="person",
                targets=((1, "某作品", 8.0),),
            )
            for index in range(10)
        ]
        items = article_tags.match_work_info(
            title=" ".join(term.name for term in terms), info_terms=terms
        )
        assert len(items) == crud.MAX_ARTICLE_ENTITIES == 8
        assert [item.name for item in items] == [f"作者{index:02d}" for index in range(8)]

    def test_recognize_combines_tags_links_and_work_info(self, wi_factory):
        """三类识别结果互不干扰：题材标签 / 关联作品 / 作品信息。"""
        article_id = _add_article(
            wi_factory,
            title="机甲动画甲新情报",
            body="机甲设定与热血剧情 河村动画制作。",
        )
        db = wi_factory()
        try:
            article = db.get(Article, article_id)
            tag_names, title_terms, info_terms = article_tags.load_vocabulary(db)
            recognition = article_tags.recognize(
                article,
                tag_names=tag_names,
                title_terms=title_terms,
                info_terms=info_terms,
            )
        finally:
            db.close()
        assert recognition.tags == ["机甲", "热血"]
        assert recognition.content_titles == ["机甲动画甲"]
        assert [item.name for item in recognition.work_info] == ["河村动画"]


    def test_duplicate_titled_entries_collapse_to_one_match(self, tmp_path):
        """同名重复条目只保留排名最高的一条（线上 Fate/Zero 就有重复）。

        作品信息反查（实体 targets）会带出全部同名条目，不去重的话
        关联列表里出现两张同名卡。标题词表本身按标题去重（只认最小
        id），兜不住二级分支，必须在匹配列表收口。
        """
        engine = create_engine(
            f"sqlite:///{tmp_path / 'dup_title.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        db = factory()
        try:
            db.add(Content(title="孤独摇滚", type="anime", summary="甲", rating=8.0))
            db.add(Content(title="孤独摇滚", type="anime", summary="乙", rating=7.0))
            studio = Entity(name="河村动画", entity_type="company")
            db.add(studio)
            db.flush()
            for row in db.scalars(select(Content)):
                db.add(
                    ContentEntityLink(
                        content_id=row.id, entity_id=studio.id, role="动画制作"
                    )
                )
            db.commit()
        finally:
            db.close()
        db = factory()
        try:
            title_terms = crud.list_content_titles(db)
            info_terms = article_tags.build_info_terms(crud.list_work_info_terms(db))
        finally:
            db.close()
        engine.dispose()

        matches = article_tags.match_article_works(
            title="河村动画新发表",
            body="河村动画新企划详情。",
            title_terms=title_terms,
            info_terms=info_terms,
        )
        assert [m.title for m in matches] == ["孤独摇滚"]


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
        """传入作品按得分序，落库后保持该顺序。"""
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

    def test_duplicate_titles_link_only_one(self, factory):
        """库中同名条目只关联 id 最小的一条（页面不出现两张同名卡）。"""
        db = factory()
        try:
            db.add(Content(title="机甲动画甲", type="anime", summary="重复条目", rating=6.0))
            db.commit()
        finally:
            db.close()
        article_id = _add_article(factory, title="资讯", body="正文")
        works = _write_links(factory, article_id, ["机甲动画甲"])
        assert len(works) == 1
        assert works[0].id == 1  # 先入库的那条
        db = factory()
        try:
            assert len(crud.list_article_contents(db, article_id)) == 1
        finally:
            db.close()

    def test_unknown_titles_link_nothing(self, factory):
        article_id = _add_article(factory, title="资讯九", body="正文")
        db = factory()
        try:
            # 词表外的标题解析不到作品（调用方按 id 取不到即跳过），
            # 空列表与 None 混入都不写悬空连接行
            assert crud.replace_article_content_links(db, article_id, []) == []
            assert crud.replace_article_content_links(db, article_id, [None]) == []
            db.commit()
            assert crud.count_article_content_links(db, article_id) == 0
        finally:
            db.close()


# ---------- 作品信息词表（crud.list_work_info_terms） ----------


class TestWorkInfoVocabulary:
    def test_entity_series_alias_rows(self, wi_factory):
        """三类词表行：实体反查作品、系列反查作品、译名指向单部作品。"""
        db = wi_factory()
        try:
            rows = crud.list_work_info_terms(db)
        finally:
            db.close()
        entity_rows = [row for row in rows if row[1] == "entity"]
        studio_targets = {row[3] for row in entity_rows if row[0] == "河村动画"}
        assert studio_targets == {1, 2}  # 机甲动画甲 + 治愈漫画乙
        assert {row[2] for row in entity_rows if row[0] == "河村动画"} == {"company"}
        author_rows = [row for row in entity_rows if row[0] == "林澈"]
        assert [(row[2], row[3]) for row in author_rows] == [("person", 1)]
        series_rows = [row for row in rows if row[1] == "series"]
        assert {row[0] for row in series_rows} == {"机甲宇宙"}
        alias_rows = [row for row in rows if row[1] == "alias"]
        assert {row[0] for row in alias_rows} == {"機甲動畫甲", "Mecha-Kou"}
        assert {row[3] for row in alias_rows} == {1}

    def test_alias_equal_to_title_is_skipped(self, factory):
        """等于自身标题的别名不进词表（标题匹配已覆盖，避免双倍计分）。"""
        db = factory()
        try:
            db.add(
                Content(
                    title="重复名",
                    type="anime",
                    summary="",
                    rating=5.0,
                    title_original="重复名",
                    aliases=["重复名", "异名"],
                )
            )
            db.commit()
            rows = crud.list_work_info_terms(db)
        finally:
            db.close()
        assert {row[0] for row in rows if row[1] == "alias"} == {"异名"}

    def test_empty_library_returns_no_rows(self, factory):
        db = factory()
        try:
            db.execute(Content.__table__.delete())
            db.commit()
            assert crud.list_work_info_terms(db) == []
        finally:
            db.close()


class TestArticleEntityCrud:
    def _terms(self) -> list[tuple]:
        return [
            ("佐藤星野", "entity", "person", None, 2),
            ("河村动画", "entity", "company", None, 6),
            ("机甲宇宙", "series", None, None, 3),
            ("Mecha-Kou", "alias", None, 1, 3),
        ]

    def test_replace_keeps_input_order_and_lists_by_score(self, factory):
        article_id = _add_article(factory, title="资讯", body="正文")
        db = factory()
        try:
            written = crud.replace_article_entities(db, article_id, self._terms())
            # 返回值保持传入序（调用方已按得分排序）
            assert [row.name for row in written] == [
                "佐藤星野",
                "河村动画",
                "机甲宇宙",
                "Mecha-Kou",
            ]
            # 列表按得分降序（replace 不自行 flush，同会话读取前先 flush）
            db.flush()
            assert [row.name for row in crud.list_article_entities(db, article_id)] == [
                "河村动画",
                "机甲宇宙",
                "Mecha-Kou",
                "佐藤星野",
            ]
            stored = crud.list_article_entities(db, article_id)[0]
            assert stored.kind == "entity" and stored.entity_type == "company"
            assert crud.count_article_entities(db, article_id) == 4
        finally:
            db.close()

    def test_cap_and_name_dedupe(self, factory):
        """超过 MAX_ARTICLE_ENTITIES 截断；同名去重（保留先出现的）。"""
        article_id = _add_article(factory, title="资讯", body="正文")
        terms = [(f"实体{index}", "entity", "person", None, index) for index in range(10)]
        terms.append(("实体0", "entity", "person", None, 99))
        db = factory()
        try:
            written = crud.replace_article_entities(db, article_id, terms)
            assert len(written) == crud.MAX_ARTICLE_ENTITIES == 8
            assert written[0].name == "实体0"
            assert len({row.name for row in written}) == 8
        finally:
            db.close()

    def test_replace_is_idempotent_and_clears(self, factory):
        article_id = _add_article(factory, title="资讯", body="正文")
        db = factory()
        try:
            crud.replace_article_entities(db, article_id, self._terms())
            db.flush()
            crud.replace_article_entities(db, article_id, self._terms())
            db.flush()
            assert crud.count_article_entities(db, article_id) == 4
            crud.replace_article_entities(db, article_id, [])
            db.flush()
            assert crud.count_article_entities(db, article_id) == 0
            assert crud.list_article_entities(db, article_id) == []
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
                title_terms=crud.list_content_titles(db),
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
                title_terms=crud.list_content_titles(db),
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

    def test_force_clears_stale_recognition_when_result_empty(self, factory):
        """重算结果为空时必须清掉历史误关联（替换式语义）。

        线上真实场景：旧算法按子串把作品《心》挂到无关资讯上，新算法
        识别为空——若跳过不写，旧行会永远留在页面，用户看到的仍是错的。
        """
        article_id = _add_article(factory, title="无关资讯", body="今天天气不错。")
        _write_tags(factory, article_id, ["机甲"])
        _write_links(factory, article_id, ["机甲动画甲"])
        assert _rows(factory, ArticleTag) == 1
        assert _rows(factory, ArticleContentLink) == 1

        forced = link_articles.run_backfill(factory, apply=True, force=True)
        assert forced.recognized == 1  # 清理旧行计为实际处理
        assert _rows(factory, ArticleTag) == 0
        assert _rows(factory, ArticleContentLink) == 0

    def test_preview_never_clears_rows(self, factory):
        """预览模式绝不写库：有旧行的空结果文章在 dry-run 下保持原样。"""
        article_id = _add_article(factory, title="无关资讯", body="今天天气不错。")
        _write_links(factory, article_id, ["机甲动画甲"])
        link_articles.run_backfill(factory, apply=False, force=True)
        assert _rows(factory, ArticleContentLink) == 1

    def test_article_id_filter(self, factory):
        ids = _seed_articles(factory)
        summary = link_articles.run_backfill(factory, apply=True, article_ids=[ids[0]])
        assert summary.requested == 1
        assert summary.recognized == 1
        assert _rows(factory, ArticleTag) == 2

    def test_backfill_uses_stored_ip_name_as_extra_signal(self, factory):
        """回填用导入时落库的 ip_name 当强信号：正文没提作品名也能关联上。

        这是修复前回填路径的缺口（导入传 extra_names、回填不传），
        存量文章重算时必须和导入路径同口径。
        """
        article_id = _add_article(
            factory,
            title="全新视觉图公开",
            body="全新视觉图与预告影片解禁。",
            ip_name="治愈漫画乙",
        )
        summary = link_articles.run_backfill(factory, apply=True, article_ids=[article_id])
        assert summary.recognized == 1
        assert summary.linked == 1
        db = factory()
        try:
            assert [w.title for w in crud.list_article_contents(db, article_id)] == [
                "治愈漫画乙"
            ]
        finally:
            db.close()

    def test_backfill_without_ip_name_stays_conservative(self, factory):
        """没有 ip_name 的存量文章不凭空关联（仍只认正文里真实出现的标题）。"""
        article_id = _add_article(
            factory, title="全新视觉图公开", body="全新视觉图与预告影片解禁。"
        )
        summary = link_articles.run_backfill(factory, apply=True, article_ids=[article_id])
        assert summary.linked == 0
        db = factory()
        try:
            assert crud.list_article_contents(db, article_id) == []
        finally:
            db.close()

    def test_backfill_counts_and_writes_work_info(self, wi_factory):
        """回填统计 informed 篇数，并把作品信息写入 article_entities。"""
        article_id = _add_article(
            wi_factory,
            title="河村动画新发表",
            body="河村动画",
            summary="企划详情近日公开。",
        )
        summary = link_articles.run_backfill(
            wi_factory, apply=True, article_ids=[article_id]
        )
        assert summary.recognized == 1
        assert summary.informed == 1
        assert summary.linked == 1
        db = wi_factory()
        try:
            entities = crud.list_article_entities(db, article_id)
            assert [e.name for e in entities] == ["河村动画"]
            assert entities[0].kind == "entity"
            assert entities[0].entity_type == "company"
            assert entities[0].score == 3  # 标题 2 + 正文 1
        finally:
            db.close()

    def test_second_run_skips_when_only_work_info(self, wi_factory):
        """只有作品信息（无题材标签、无关联）也算已有识别结果，不重复处理。"""
        article_id = _add_article(
            wi_factory, title="活动举办", body="河村动画协力赞助。"
        )
        first = link_articles.run_backfill(
            wi_factory, apply=True, article_ids=[article_id]
        )
        assert first.recognized == 1
        assert first.informed == 1
        assert first.linked == 0  # 正文单次实体提及不够关联门槛
        again = link_articles.run_backfill(
            wi_factory, apply=True, article_ids=[article_id]
        )
        assert again.recognized == 0
        assert again.skipped == 1
        db = wi_factory()
        try:
            assert [e.name for e in crud.list_article_entities(db, article_id)] == [
                "河村动画"
            ]
            assert crud.list_article_contents(db, article_id) == []
        finally:
            db.close()

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


# ---------- 历史库补列 ----------


class TestLegacyArticleSchemaUpgrade:
    def test_ensure_columns_are_added_idempotently(self, tmp_path):
        """识别/头图上线前创建的 articles 表：补 image_url 与 ip_name，重复调用不报错。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy_articles.db'}")
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE articles ("
                    "id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,"
                    " category VARCHAR(50) NOT NULL, summary TEXT NOT NULL,"
                    " body TEXT NOT NULL, source VARCHAR(200),"
                    " original_url VARCHAR(1000), is_published BOOLEAN NOT NULL,"
                    " published_at DATETIME NOT NULL, created_at DATETIME NOT NULL)"
                ))
            assert crud.ensure_article_image_column(engine) == ["image_url"]
            assert crud.ensure_article_image_column(engine) == []
            assert crud.ensure_article_ip_name_column(engine) == ["ip_name"]
            assert crud.ensure_article_ip_name_column(engine) == []
        finally:
            engine.dispose()

    def test_ensure_skips_missing_table(self, tmp_path):
        """表还不存在时（全新库由 create_all 建表）补列函数直接返回空，不报错。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        try:
            assert crud.ensure_article_ip_name_column(engine) == []
        finally:
            engine.dispose()


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
                    title_terms=crud.list_content_titles(db),
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

    def test_detail_page_shows_work_info_chips(self, wi_page_env):
        """作品信息 chips：实体进花名册页、译名进作品页，chip 带来源标注。"""
        factory = wi_page_env.session_factory
        article_id = _add_article(
            factory,
            title="河村动画 Mecha-Kou 联合企划",
            body="河村动画制作。",
        )
        db = factory()
        try:
            tag_names, title_terms, info_terms = article_tags.load_vocabulary(db)
            article_tags.apply_recognition(
                db,
                article_tags.recognize(
                    db.get(Article, article_id),
                    tag_names=tag_names,
                    title_terms=title_terms,
                    info_terms=info_terms,
                ),
            )
            db.commit()
        finally:
            db.close()

        resp = wi_page_env.client.get(f"/articles/{article_id}")
        assert resp.status_code == 200
        assert "作品信息" in resp.text
        # 公司 chip → 花名册页（URL 编码），chip 内带「公司」来源标注
        assert f'href="/creators/{quote("河村动画")}"' in resp.text
        assert "河村动画" in resp.text
        assert 'class="work-info-kind">公司<' in resp.text
        # 译名 chip → 作品详情页
        work_id = wi_page_env.content_ids["机甲动画甲"]
        assert f'href="/contents/{work_id}"' in resp.text
        assert "Mecha-Kou" in resp.text
        # 译名是直接命中：关联作品里出现机甲动画甲
        assert "关联作品" in resp.text
        assert "机甲动画甲" in resp.text

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
                    title_terms=crud.list_content_titles(db),
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


# ---------- 作品详情页「相关资讯」（反方向：识别关联 ∪ 标题 LIKE） ----------
class TestWorkPageRelatedArticles:
    """作品页反查资讯：显式关联优先占位，标题 LIKE 补剩余名额。

    与资讯页「关联作品」互为反向——识别把「译名 / 作者 / 公司 / 系列」
    命中写进 article_content_links 后，作品详情页靠它把「通篇没提作品
    标题」的资讯也捞出来（旧口径只 LIKE 标题，这类资讯永远看不到）。
    """

    def test_entity_only_article_appears_on_work_page(self, wi_page_env):
        """标题只有公司名的资讯（旧口径完全关联不上）出现在旗下作品页。"""
        factory = wi_page_env.session_factory
        article_id = _add_article(
            factory, title="河村动画新发表", body="河村动画公布全新企划。"
        )
        _recognize(factory, article_id)
        work_id = wi_page_env.content_ids["机甲动画甲"]

        resp = wi_page_env.client.get(f"/contents/{work_id}")
        assert resp.status_code == 200
        assert "相关资讯" in resp.text
        assert "河村动画新发表" in resp.text
        assert f'href="/articles/{article_id}"' in resp.text
        assert "暂无提到该作品的资讯" not in resp.text

    def test_alias_article_hidden_until_recognized(self, wi_page_env):
        """只出现译名的资讯：识别前标题 LIKE 捞不到，识别后立即出现。"""
        factory = wi_page_env.session_factory
        article_id = _add_article(
            factory, title="Mecha-Kou 全新视觉图", body="全新视觉图解禁。"
        )
        work_id = wi_page_env.content_ids["机甲动画甲"]

        # 识别前：正文没有作品标题原文，LIKE 分支匹配不到
        resp = wi_page_env.client.get(f"/contents/{work_id}")
        assert f'href="/articles/{article_id}"' not in resp.text

        # 识别后：译名一级命中写入关联，作品页立即可见
        _recognize(factory, article_id)
        resp = wi_page_env.client.get(f"/contents/{work_id}")
        assert f'href="/articles/{article_id}"' in resp.text

    def test_single_body_mention_still_shows_via_title_like(self, wi_page_env):
        """正文只提一次标题（不够关联分、不落关联）仍经 LIKE 兜底展示。"""
        factory = wi_page_env.session_factory
        article_id = _add_article(
            factory, title="深夜档新番动态", body="本期节目涉及机甲动画甲。"
        )
        _recognize(factory, article_id)
        work_id = wi_page_env.content_ids["机甲动画甲"]

        db = factory()
        try:
            # 得分 1 < MIN_LINK_SCORE：没有关联行，纯粹靠 LIKE 兜底
            assert crud.count_article_content_links(db, article_id) == 0
        finally:
            db.close()

        resp = wi_page_env.client.get(f"/contents/{work_id}")
        assert f'href="/articles/{article_id}"' in resp.text

    def test_linked_articles_take_priority_and_fill_limit(self, wi_page_env):
        """显式关联优先占位：3 篇实体命中的老资讯排在 3 篇 LIKE 新资讯前。"""
        factory = wi_page_env.session_factory
        work_id = wi_page_env.content_ids["机甲动画甲"]
        base = datetime(2026, 1, 1, 9, 0, 0)

        linked_ids = []
        for index in range(3):
            article_id = _add_article(
                factory,
                title=f"河村动画企划第{index + 1}弹",
                body=f"河村动画第{index + 1}弹企划详情。",
            )
            _set_published_at(factory, article_id, base + timedelta(days=index))
            _recognize(factory, article_id)
            linked_ids.append(article_id)

        like_ids = []
        for index in range(3):
            article_id = _add_article(
                factory,
                title=f"深夜档新番动态{index + 1}",
                body=f"本期节目涉及机甲动画甲（第{index + 1}条）。",
            )
            # LIKE 那几篇更新（默认按时间倒序会把它们排前面，必须被关联压过）
            _set_published_at(factory, article_id, base + timedelta(days=30 + index))
            _recognize(factory, article_id)
            like_ids.append(article_id)

        resp = wi_page_env.client.get(f"/contents/{work_id}")
        html = resp.text
        positions = [html.index(f'href="/articles/{article_id}"') for article_id in linked_ids]
        # 3 篇关联全部占位，且彼此仍按发布时间倒序
        assert positions == sorted(positions, reverse=True)
        # 名额 5：3 篇关联 + 最新的 2 篇 LIKE，最老的 LIKE 被挤掉
        assert f'href="/articles/{like_ids[2]}"' in html
        assert f'href="/articles/{like_ids[1]}"' in html
        assert f'href="/articles/{like_ids[0]}"' not in html

    def test_unpublished_article_not_listed(self, wi_page_env):
        """未发布资讯即使被识别关联，也不进作品页（页面只展示已发布）。"""
        factory = wi_page_env.session_factory
        work_id = wi_page_env.content_ids["机甲动画甲"]
        hidden = _add_article(
            factory,
            title="河村动画草稿",
            body="河村动画草稿企划。",
            published=False,
        )
        shown = _add_article(
            factory, title="河村动画正式发表", body="河村动画正式企划。"
        )
        _recognize(factory, hidden)
        _recognize(factory, shown)

        db = factory()
        try:
            # 草稿也写入了关联（识别不看过发布态，是页面层过滤掉的）：
            # 河村动画同时是机甲动画甲（动画制作）与治愈漫画乙（发行）的实体
            assert crud.count_article_content_links(db, hidden) == 2
        finally:
            db.close()

        resp = wi_page_env.client.get(f"/contents/{work_id}")
        assert f'href="/articles/{shown}"' in resp.text
        assert f'href="/articles/{hidden}"' not in resp.text
