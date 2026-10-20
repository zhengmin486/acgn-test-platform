"""搜索增强的召回与排名测试（title_original / aliases / 季数变体 / 排名）。

种子库手工构造（不依赖 seed_if_empty，便于精确断言召回集合）：
- c1《凉宫春日的忧郁》：title_original 日文原名 + 别名（SOS团）；
- c2《某日常 第二季》：标题带季数写法一，title_original 带写法二；
- c3《治愈时光》：题材标签「治愈」（genre AND 筛选落点）；
- c4《日常》：标题即题材词的边界样本；
- c5《summary 携带者》：简介含关键词但标题/标签不含（排名垫底样本）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.crud import replace_content_tags, search_contents_page  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    Content,
    ContentCast,
    ContentEntityLink,
    Entity,
    Tag,
)
from app.search_query import parse_query  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'recall.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        rows = [
            Content(
                title="凉宫春日的忧郁",
                type="anime",
                summary="sos 团的学园故事",
                rating=8.5,
                title_original="涼宮ハルヒの憂鬱",
                aliases=["SOS团", "Haruhi"],
            ),
            Content(
                title="某日常 第二季",
                type="anime",
                summary="续作",
                rating=7.0,
                title_original="ある日常 2nd season",
            ),
            Content(title="治愈时光", type="anime", summary="温馨小故事", rating=7.5),
            Content(title="日常", type="anime", summary="京阿尼日常番", rating=8.0),
            Content(
                title="summary 携带者",
                type="anime",
                summary="正文里出现了春这个字",
                rating=6.0,
            ),
        ]
        session.add_all(rows)
        session.flush()
        tags = {
            name: Tag(name=name)
            for name in ("科幻", "校园", "治愈", "日常", "搞笑", "恋爱")
        }
        session.add_all(tags.values())
        session.flush()
        replace_content_tags(session, rows[0].id, ["科幻", "校园"])
        replace_content_tags(session, rows[1].id, ["日常"])
        replace_content_tags(session, rows[2].id, ["治愈"])
        replace_content_tags(session, rows[3].id, ["日常", "搞笑"])
        # 作品信息：公司（c4 日常 × 京都动画）与声优（c1 凉宫 × 杉田智和）
        studio = Entity(name="京都动画", entity_type="company")
        person = Entity(name="杉田智和", entity_type="person")
        workshop = Entity(name="春野工作室", entity_type="company")
        session.add_all([studio, person, workshop])
        session.flush()
        session.add_all(
            [
                ContentEntityLink(
                    content_id=rows[3].id, entity_id=studio.id, role="动画制作"
                ),
                ContentEntityLink(
                    content_id=rows[0].id, entity_id=person.id, role="声优"
                ),
                ContentEntityLink(
                    content_id=rows[4].id, entity_id=workshop.id, role="动画制作"
                ),
                ContentCast(
                    content_id=rows[0].id,
                    character="古泉一树",
                    actor="杉田智和",
                ),
            ]
        )
        session.commit()
        yield session
    finally:
        session.close()


def titles(session, **kwargs):
    _, items = search_contents_page(session, **kwargs)
    return [item.title for item in items]


class TestAliasRecall:
    def test_japanese_original_recall(self, db):
        assert titles(db, keyword="ハルヒ") == ["凉宫春日的忧郁"]

    def test_alias_recall(self, db):
        assert titles(db, keyword="SOS团") == ["凉宫春日的忧郁"]

    def test_tag_name_recall(self, db):
        """关键词命中标签名也可召回（旧版只查标题/简介）。"""
        result = titles(db, keyword="恋爱")
        assert result == []  # 种子里没有挂「恋爱」标签的作品

    def test_summary_still_recalled(self, db):
        assert titles(db, keyword="携带者") == ["summary 携带者"]


class TestSeasonSearch:
    def test_season_variant_matches_other_writing(self, db):
        """搜「第二季」能命中标题写「2nd season」的条目（跨写法召回）。"""
        assert titles(db, keyword="某日常", season_number=2) == ["某日常 第二季"]

    def test_season_excludes_unnumbered(self, db):
        """带季数意图时不召回无季数写法的同名作品。"""
        assert titles(db, keyword="日常", season_number=2) == ["某日常 第二季"]


class TestGenreIntent:
    def test_genre_filter_lands_on_tag(self, db):
        assert titles(db, keyword="", genres=["治愈"]) == ["治愈时光"]

    def test_genre_plus_keyword_and(self, db):
        assert titles(db, keyword="时光", genres=["治愈"]) == ["治愈时光"]

    def test_genre_excludes_untagged(self, db):
        assert titles(db, keyword="", genres=["恋爱"]) == []


class TestInfoRecall:
    def test_company_recall(self, db):
        """搜公司名召回其出品作品（旧版完全搜不到）。"""
        assert titles(db, keyword="京都动画") == ["日常"]

    def test_cast_recall(self, db):
        """搜声优名/角色名召回参演作品。"""
        assert titles(db, keyword="杉田智和") == ["凉宫春日的忧郁"]
        assert titles(db, keyword="古泉一树") == ["凉宫春日的忧郁"]

    def test_title_beats_info(self, db):
        """标题命中 > 作品信息命中：c5 的公司名含「春」但排在 c1 标题之后。"""
        assert titles(db, keyword="春") == ["凉宫春日的忧郁", "summary 携带者"]


class TestRanking:
    def test_title_beats_summary(self, db):
        """标题命中排在仅简介命中之前（种子顺序与之相反，验证排名生效）。"""
        assert titles(db, keyword="春") == ["凉宫春日的忧郁", "summary 携带者"]

    def test_browse_mode_keeps_id_order(self, db):
        """纯筛选浏览（无关键词）保持按 id 排序，既有断言不受影响。"""
        _, items = search_contents_page(db, content_type="anime")
        assert [i.id for i in items] == sorted(i.id for i in items)


class TestRobustness:
    @pytest.mark.parametrize(
        "keyword",
        [
            "%",
            "_",
            "' OR 1=1--",
            "<script>",
            "🌸",
            "ａｂｃ",
            "a" * 800,
            "治愈日常 2019年 动画 第二季",
        ],
    )
    def test_weird_input_no_error(self, db, keyword):
        """异常输入不报错、返回列表——搜索永不 500。"""
        parsed = parse_query(keyword)
        result = titles(
            db,
            keyword=parsed.keyword,
            genres=parsed.genres,
            season_number=parsed.season_number,
            content_type=parsed.content_type,
            year=parsed.year,
        )
        assert isinstance(result, list)


class TestPageIntegration:
    def test_contents_page_search_alias(self, tmp_path):
        """页面层端到端：搜索别名出结果（parse_query → CRUD 全链路）。"""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.main import app

        engine = create_engine(
            f"sqlite:///{tmp_path / 'page.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            session.add(
                Content(
                    title="凉宫春日的忧郁",
                    type="anime",
                    summary="s",
                    rating=8.0,
                    title_original="涼宮ハルヒの憂鬱",
                    aliases=["SOS团"],
                )
            )
            session.commit()

        def override():
            s = factory()
            try:
                yield s
            finally:
                s.close()

        app.dependency_overrides[get_db] = override
        try:
            client = TestClient(app)
            resp = client.get("/contents", params={"q": "SOS团"})
            assert resp.status_code == 200
            assert "凉宫春日的忧郁" in resp.text
        finally:
            app.dependency_overrides.pop(get_db, None)
