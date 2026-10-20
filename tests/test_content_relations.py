"""详情页三个关联区块的行为测试：系列作品 / 相关内容推荐 / 相关资讯。

覆盖（对应本次改造的验收点）：
- 作品关系（content_relations）：双向镜像、替换式写入幂等；
- 系列作品区：真实关系 ∪ 同系列，去重且关系标签优先，按关系远近 + 年份排序；
- 相关内容推荐：实体关系优先于题材、跨类型只降权「无真实关系」的候选、
  默认排除系列区作品、无信号才回落同类型榜单、理由可追溯；
- 相关资讯：短标题不走 LIKE，只认真实关联。

独立临时 SQLite，不联网、不触碰开发库。
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base
from app.models import (
    Article,
    ArticleContentLink,
    Content,
    ContentRelation,
)


def _content(db, title, type_="anime", rating=7.0, year=None, bangumi_id=None):
    row = Content(
        title=title,
        type=type_,
        summary=f"{title}的简介",
        rating=rating,
        year=year,
        bangumi_id=bangumi_id,
    )
    db.add(row)
    db.commit()
    return row


def _links(db, content, links):
    """写入作者 / 公司关联：(名称, 类型, 角色)。"""
    crud.replace_content_entity_links(db, content.id, links)


@pytest.fixture
def factory(tmp_path):
    """独立临时库（每次清空题材 IDF 缓存，避免用例间串味）。"""
    crud._TAG_IDF_CACHE.update({"at": 0.0, "values": {}})
    engine = create_engine(
        f"sqlite:///{tmp_path / 'relations.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False
    )
    yield session_factory
    engine.dispose()
    crud._TAG_IDF_CACHE.update({"at": 0.0, "values": {}})


# ---------- 作品关系（前作 / 续作 / 衍生作） ----------


class TestContentRelations:
    def test_relations_are_mirrored_for_reverse_lookup(self, factory):
        """本作 → 对方 存「续作」；从对方反查时本作是它的「前作」。"""
        db = factory()
        try:
            first = _content(db, "第一季", year=2016)
            second = _content(db, "第二季", year=2018)
            crud.replace_content_relations(
                db, first.id, [(second.id, crud.RELATION_SEQUEL)]
            )
            assert [
                (row.title, label) for row, label in crud.list_content_relations(db, first.id)
            ] == [("第二季", "续作")]
            assert [
                (row.title, label) for row, label in crud.list_content_relations(db, second.id)
            ] == [("第一季", "前作")]
        finally:
            db.close()

    def test_replace_is_idempotent(self, factory):
        """重复回填同一部作品不堆叠行。"""
        db = factory()
        try:
            first = _content(db, "A")
            second = _content(db, "B")
            crud.replace_content_relations(
                db, first.id, [(second.id, crud.RELATION_SPINOFF)]
            )
            crud.replace_content_relations(
                db, first.id, [(second.id, crud.RELATION_SPINOFF)]
            )
            rows = db.scalars(
                select(ContentRelation).where(
                    ContentRelation.from_content_id == first.id
                )
            ).all()
            assert len(rows) == 1
        finally:
            db.close()

    def test_unknown_relation_type_is_rejected(self, factory):
        """映射外的关系（如「联动」）不入库。"""
        db = factory()
        try:
            first = _content(db, "A")
            second = _content(db, "B")
            written = crud.replace_content_relations(
                db, first.id, [(second.id, "联动")]
            )
            assert written == 0
        finally:
            db.close()


# ---------- 系列作品区 ----------


class TestSeriesSection:
    def test_relations_and_series_are_merged_without_duplicates(self, factory):
        """关系（前作/续作/衍生作）与同系列并集，关系标签优先，按远近+年份排序。"""
        db = factory()
        try:
            main = _content(db, "本篇", year=2020)
            prequel = _content(db, "前传篇", year=2018)
            sequel = _content(db, "第二季", year=2022)
            spinoff = _content(db, "外传", year=2021)
            same_series = _content(db, "剧场版", year=2019)

            crud.replace_content_relations(
                db,
                main.id,
                [
                    (prequel.id, crud.RELATION_PREQUEL),
                    (sequel.id, crud.RELATION_SEQUEL),
                    (spinoff.id, crud.RELATION_SPINOFF),
                ],
            )
            crud.set_content_series(db, main.id, "测试系列")
            crud.set_content_series(db, same_series.id, "测试系列")

            items = crud.list_series_section(db, main)
            assert [(row.title, label) for row, label in items] == [
                ("前传篇", "前作"),
                ("第二季", "续作"),
                ("外传", "衍生作"),
                ("剧场版", "同系列"),
            ]
        finally:
            db.close()

    def test_relation_label_wins_over_same_series(self, factory):
        """同一部作品既有关系又同系列：保留关系标签，不退化成「同系列」。"""
        db = factory()
        try:
            main = _content(db, "本篇", year=2020)
            other = _content(db, "续篇", year=2021)
            crud.replace_content_relations(
                db, main.id, [(other.id, crud.RELATION_SEQUEL)]
            )
            crud.set_content_series(db, main.id, "系列X")
            crud.set_content_series(db, other.id, "系列X")
            labels = {
                row.title: label for row, label in crud.list_series_section(db, main)
            }
            assert labels["续篇"] == "续作"
        finally:
            db.close()

    def test_empty_when_no_relation_and_no_series(self, factory):
        """没有任何关系与系列时返回空（由页面展示空态）。"""
        db = factory()
        try:
            lonely = _content(db, "孤零零")
            assert crud.list_series_section(db, lonely) == []
        finally:
            db.close()


# ---------- 相关内容推荐 ----------


class TestRelatedContents:
    def test_shared_original_author_beats_shared_tags(self, factory):
        """同原作作者排在只同题材的作品前面。"""
        db = factory()
        try:
            main = _content(db, "本篇", rating=7.0)
            by_author = _content(db, "同原作作品", rating=6.0)
            by_tag = _content(db, "同题材作品", rating=9.5)

            _links(db, main, [("麻枝准", "person", "原作")])
            _links(db, by_author, [("麻枝准", "person", "原作")])
            crud.replace_content_tags(db, main.id, ["冒险"])
            crud.replace_content_tags(db, by_tag.id, ["冒险"])

            ranked = crud.list_related_contents(db, main, limit=6)
            assert [row.title for row in ranked] == ["同原作作品", "同题材作品"]
        finally:
            db.close()

    def test_real_relation_is_not_downgraded_by_cross_type(self, factory):
        """真实创作者关系的跨类型作品不降权，排在纯同题材的跨类型作品前面。"""
        db = factory()
        try:
            main = _content(db, "本篇", type_="anime")
            cross_author = _content(db, "原作小说", type_="novel")
            cross_tag = _content(db, "同题材漫画", type_="comic")

            _links(db, main, [("麻枝准", "person", "原作")])
            _links(db, cross_author, [("麻枝准", "person", "原作")])
            crud.replace_content_tags(db, main.id, ["冒险"])
            crud.replace_content_tags(db, cross_tag.id, ["冒险"])

            ranked = crud.list_related_contents(db, main, limit=6)
            assert [row.title for row in ranked][0] == "原作小说"
        finally:
            db.close()

    def test_studio_and_publisher_are_weaker_than_author(self, factory):
        """同制作公司（30）低于同导演（40）、高于同音乐（15）。"""
        db = factory()
        try:
            main = _content(db, "本篇")
            same_director = _content(db, "同导演作", rating=5.0)
            same_studio = _content(db, "同公司作", rating=9.9)
            same_music = _content(db, "同音乐作", rating=9.9)

            _links(
                db,
                main,
                [
                    ("新海诚", "person", "导演"),
                    ("京都动画", "company", "动画制作"),
                    ("RADWIMPS", "company", "音乐"),
                ],
            )
            _links(db, same_director, [("新海诚", "person", "导演")])
            _links(db, same_studio, [("京都动画", "company", "动画制作")])
            _links(db, same_music, [("RADWIMPS", "company", "音乐")])

            ranked = [row.title for row in crud.list_related_contents(db, main, limit=6)]
            assert ranked == ["同导演作", "同公司作", "同音乐作"]
        finally:
            db.close()

    def test_series_section_items_are_excluded(self, factory):
        """系列区已展示的作品不重复出现在推荐区。"""
        db = factory()
        try:
            main = _content(db, "本篇", year=2020)
            in_series = _content(db, "第二季", year=2021)
            other = _content(db, "同原作另一部", year=2019)

            crud.replace_content_relations(
                db, main.id, [(in_series.id, crud.RELATION_SEQUEL)]
            )
            _links(db, main, [("麻枝准", "person", "原作")])
            _links(db, in_series, [("麻枝准", "person", "原作")])
            _links(db, other, [("麻枝准", "person", "原作")])

            series_ids = {row.id for row, _ in crud.list_series_section(db, main)}
            ranked = crud.list_related_contents(
                db, main, limit=6, exclude_ids=series_ids
            )
            assert [row.title for row in ranked] == ["同原作另一部"]
        finally:
            db.close()

    def test_no_padding_when_candidates_are_few(self, factory):
        """候选不足就少给几条，不拿无关作品凑满 limit。"""
        db = factory()
        try:
            main = _content(db, "本篇")
            only_one = _content(db, "唯一相关")
            _links(db, main, [("麻枝准", "person", "原作")])
            _links(db, only_one, [("麻枝准", "person", "原作")])
            for index in range(5):
                _content(db, f"无关作品{index}", rating=9.9)

            ranked = crud.list_related_contents(db, main, limit=6)
            assert [row.title for row in ranked] == ["唯一相关"]
        finally:
            db.close()

    def test_type_ranking_is_last_resort_only(self, factory):
        """既无关系又无题材时才回落同类型榜单，且理由如实标注。"""
        db = factory()
        try:
            main = _content(db, "无信号本篇", rating=1.0)
            top = _content(db, "同类型高分", rating=9.9)
            _content(db, "同类型低分", rating=2.0)

            pairs = crud.list_related_contents_with_reasons(db, main, limit=6)
            # 兜底层如实标注「同类型高分」，按评分降序
            assert [(row.title, reason) for row, reason in pairs] == [
                ("同类型高分", "同类型高分"),
                ("同类型低分", "同类型高分"),
            ]
            assert pairs[0][0].id == top.id
        finally:
            db.close()

    def test_reason_is_traceable_to_real_data(self, factory):
        """推荐理由写明命中的真实关系与题材名。"""
        db = factory()
        try:
            main = _content(db, "本篇")
            other = _content(db, "相关作")
            _links(db, main, [("麻枝准", "person", "原作")])
            _links(db, other, [("麻枝准", "person", "原作")])
            crud.replace_content_tags(db, main.id, ["冒险"])
            crud.replace_content_tags(db, other.id, ["冒险"])

            _row, reason = crud.list_related_contents_with_reasons(db, main, limit=6)[0]
            assert "同原作：麻枝准" in reason
            assert "同题材：冒险" in reason
        finally:
            db.close()

    def test_self_is_never_recommended(self, factory):
        """推荐里不会出现作品自己。"""
        db = factory()
        try:
            main = _content(db, "本篇")
            _links(db, main, [("麻枝准", "person", "原作")])
            assert crud.list_related_contents(db, main, limit=6) == []
        finally:
            db.close()


# ---------- 相关资讯 ----------


class TestRelatedArticles:
    def _article(self, db, title, body, published=None):
        row = Article(
            title=title,
            category="动画",
            summary=body[:40],
            body=body,
            source="测试源",
            original_url=f"https://news.example/{abs(hash(title)) % 99999}",
            is_published=True,
            published_at=published or datetime(2026, 10, 20, 12, 0),
        )
        db.add(row)
        db.commit()
        return row

    def test_short_title_does_not_match_by_like(self, factory):
        """1~2 字标题不做 LIKE 召回（正文里到处都是，必然假相关）。"""
        db = factory()
        try:
            main = _content(db, "空")
            noise = self._article(db, "无关资讯", "这条正文里出现了一个空字")
            assert crud.list_related_articles(db, main) == []
            assert noise.id is not None
        finally:
            db.close()

    def test_linked_article_is_related(self, factory):
        """识别关联（article_content_links）命中即相关。"""
        db = factory()
        try:
            main = _content(db, "空")
            article = self._article(db, "提到该作品的资讯", "正文没写标题也能关联")
            db.add(ArticleContentLink(article_id=article.id, content_id=main.id))
            db.commit()
            assert [row.title for row in crud.list_related_articles(db, main)] == [
                "提到该作品的资讯"
            ]
        finally:
            db.close()
