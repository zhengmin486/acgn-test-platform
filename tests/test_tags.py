"""作品标签测试：CRUD、筛选（SQL 下推）与页面渲染。

覆盖：
- replace_content_tags：截断 / 去重 / 丢弃非法名 / 幂等替换 / 清空 / 返回值；
- list_content_tags / list_tags_with_counts：顺序、计数、上限、空库；
- search_contents_page / search_contents：标签单独筛选、与类型/关键词叠加、
  无命中；
- /contents 页面：标签 chip 渲染与高亮、?tag= 筛选结果、统计行、组合查询保留
  其它条件、翻页保留标签、无标签时不渲染筛选栏；
- 详情页：有标签渲染 chip 链接，无标签时保留占位文案。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from app.models import Content, Tag


@pytest.fixture
def tag_env(tmp_path):
    """独立临时库 + 三部作品 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tags.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        for title, ctype in (("机甲动画甲", "anime"), ("治愈漫画乙", "comic"), ("机甲游戏丙", "game")):
            db.add(Content(title=title, type=ctype, summary=f"{title} 的简介。", rating=8.0))
        db.commit()
    finally:
        db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SimpleNamespace(
            client=TestClient(app, follow_redirects=False),
            session_factory=TestingSession,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def content_ids(factory) -> dict:
    """{标题: id}，便于按标题引用作品。"""
    db = factory()
    try:
        return {row.title: row.id for row in db.query(Content).order_by(Content.id)}
    finally:
        db.close()


def seed_tags(factory) -> dict:
    """三部作品分别打标签：机甲动画甲=[机甲,热血]、机甲游戏丙=[机甲]、治愈漫画乙=[治愈]。"""
    ids = content_ids(factory)
    db = factory()
    try:
        crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲", "热血"])
        crud.replace_content_tags(db, ids["机甲游戏丙"], ["机甲"])
        crud.replace_content_tags(db, ids["治愈漫画乙"], ["治愈"])
    finally:
        db.close()
    return ids


# ---------- 标签 CRUD ----------


class TestReplaceContentTags:
    def test_writes_tags_and_returns_names(self, tag_env):
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            names = crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲", "热血", "科幻"])
            assert names == ["机甲", "热血", "科幻"]
            assert crud.list_content_tags(db, ids["机甲动画甲"]) == ["机甲", "热血", "科幻"]
        finally:
            db.close()

    def test_caps_at_max_and_dedupes(self, tag_env):
        """超过 MAX_CONTENT_TAGS 个只保留前 5；同名去重不占名额。"""
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            names = crud.replace_content_tags(
                db, ids["机甲动画甲"], ["一", "二", "三", "四", "五", "六", "七", "二"]
            )
            assert names == ["一", "二", "三", "四", "五"]
        finally:
            db.close()

    def test_drops_empty_and_overlong_names(self, tag_env):
        """空名与超长名（TAG_NAME_MAX_LENGTH）丢弃，不截断。"""
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            names = crud.replace_content_tags(
                db, ids["机甲动画甲"], ["", "  ", "长" * 81, "正常"]
            )
            assert names == ["正常"]
        finally:
            db.close()

    def test_replace_is_idempotent_without_stacking(self, tag_env):
        """整体替换：旧标签被移除，关联行不堆叠。"""
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲", "热血"])
            names = crud.replace_content_tags(db, ids["机甲动画甲"], ["治愈"])
            assert names == ["治愈"]
            assert crud.list_content_tags(db, ids["机甲动画甲"]) == ["治愈"]
        finally:
            db.close()

    def test_empty_list_clears_tags(self, tag_env):
        """传空列表 = 清空该作品标签。"""
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲"])
            assert crud.replace_content_tags(db, ids["机甲动画甲"], []) == []
            assert crud.list_content_tags(db, ids["机甲动画甲"]) == []
        finally:
            db.close()


class TestTagQueries:
    def test_list_tags_with_counts_orders_by_count_desc(self, tag_env):
        seed_tags(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            assert crud.list_tags_with_counts(db) == [("机甲", 2), ("治愈", 1), ("热血", 1)]
            assert crud.list_tags_with_counts(db, limit=1) == [("机甲", 2)]
        finally:
            db.close()

    def test_list_tags_with_counts_empty_db(self, tag_env):
        db = tag_env.session_factory()
        try:
            assert crud.list_tags_with_counts(db) == []
        finally:
            db.close()

    def test_orphaned_tags_not_listed_but_reusable(self, tag_env):
        """标签被替换掉后不出现在筛选列表，但 Tag 行保留（再次导入可直接复用）。"""
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲"])
            crud.replace_content_tags(db, ids["机甲动画甲"], ["治愈"])

            names = [name for name, _ in crud.list_tags_with_counts(db)]
            assert names == ["治愈"]
            # Tag 行仍在：重新写回「机甲」不会新建行（全局按名称去重）
            crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲"])
            assert db.scalar(select(func.count()).select_from(Tag)) == 2
        finally:
            db.close()


# ---------- 标签筛选（SQL 下推） ----------


class TestSearchByTag:
    def test_tag_filter_alone(self, tag_env):
        seed_tags(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            total, items = crud.search_contents_page(db, tag="机甲")
            assert total == 2
            assert {item.title for item in items} == {"机甲动画甲", "机甲游戏丙"}
        finally:
            db.close()

    def test_tag_combined_with_type(self, tag_env):
        seed_tags(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            total, items = crud.search_contents_page(db, content_type="game", tag="机甲")
            assert total == 1
            assert items[0].title == "机甲游戏丙"
        finally:
            db.close()

    def test_tag_combined_with_keyword(self, tag_env):
        seed_tags(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            total, items = crud.search_contents_page(db, keyword="游戏", tag="机甲")
            assert total == 1
            assert items[0].title == "机甲游戏丙"
        finally:
            db.close()

    def test_unknown_tag_returns_empty(self, tag_env):
        seed_tags(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            assert crud.search_contents_page(db, tag="不存在的标签") == (0, [])
        finally:
            db.close()

    def test_non_paged_search_supports_tag(self, tag_env):
        seed_tags(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            items = crud.search_contents(db, "", tag="治愈")
            assert [item.title for item in items] == ["治愈漫画乙"]
        finally:
            db.close()


# ---------- 页面渲染 ----------


class TestContentsPageTags:
    def test_tag_chips_rendered_with_counts(self, tag_env):
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/contents")
        assert resp.status_code == 200
        # 筛选栏 chip 链接（q/type 为空时也带上，保持结构稳定）+ 关联作品数
        assert 'href="/contents?q=&amp;type=&amp;tag=' in resp.text
        assert '<span class="tag-count">2</span>' in resp.text

    def test_tag_filter_narrows_results_and_highlights_chip(self, tag_env):
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/contents?tag=机甲")
        assert resp.status_code == 200
        assert "机甲动画甲" in resp.text and "机甲游戏丙" in resp.text
        assert "治愈漫画乙" not in resp.text
        # 当前标签 chip 高亮 + 统计行带标签 + 出现「清除标签」
        assert 'class="tag-chip active"' in resp.text
        assert "标签：机甲" in resp.text
        assert "共 2 条" in resp.text
        assert "清除标签" in resp.text

    def test_tag_filter_empty_state(self, tag_env):
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/contents?tag=没有这个标签")
        assert resp.status_code == 200
        assert "没有找到匹配的内容" in resp.text
        assert "机甲动画甲" not in resp.text

    def test_tag_filter_keeps_type_in_chips(self, tag_env):
        """标签 + 类型组合：chip 链接保留另一条件，结果只留交集。"""
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/contents?type=game&tag=机甲")
        assert resp.status_code == 200
        assert "机甲游戏丙" in resp.text
        assert "机甲动画甲" not in resp.text
        assert 'href="/contents?q=&amp;type=game&amp;tag=' in resp.text

    def test_tag_filter_pager_preserves_tag(self, tag_env):
        """翻页不丢标签：分页链接带 tag 参数，第 2 页仍是筛选结果。"""
        db = tag_env.session_factory()
        try:
            for index in range(25):
                title = f"机甲作品 {index:02d}"
                db.add(Content(title=title, type="anime", summary=f"{title} 简介。", rating=7.0))
            db.commit()
            rows = db.query(Content).filter(Content.title.like("机甲作品%")).all()
            for row in rows:
                crud.replace_content_tags(db, row.id, ["机甲"])
        finally:
            db.close()

        resp = tag_env.client.get("/contents?tag=机甲&page=2")
        assert resp.status_code == 200
        # 25 部带「机甲」标签的作品：第 1 页 20 部（00-19），第 2 页 5 部（20-24）
        assert "机甲作品 20" in resp.text  # 第 2 页第一条
        assert "机甲作品 19" not in resp.text  # 第 1 页最后一条不出现
        assert "tag=%E6%9C%BA%E7%94%B2&amp;page=1" in resp.text  # 上一页保留标签

    def test_no_tags_no_filter_bar(self, tag_env):
        """没有任何标签时不渲染筛选栏。"""
        resp = tag_env.client.get("/contents")
        assert resp.status_code == 200
        assert 'class="tag-filter"' not in resp.text


class TestDetailPageTags:
    def test_detail_shows_tag_chips(self, tag_env):
        ids = content_ids(tag_env.session_factory)
        db = tag_env.session_factory()
        try:
            crud.replace_content_tags(db, ids["机甲动画甲"], ["机甲", "热血"])
        finally:
            db.close()

        resp = tag_env.client.get(f"/contents/{ids['机甲动画甲']}")
        assert resp.status_code == 200
        assert 'href="/contents?tag=' in resp.text
        assert "机甲" in resp.text and "热血" in resp.text
        # 有标签时不再显示「暂无其他标签数据」
        assert "暂无其他标签数据" not in resp.text

    def test_detail_without_tags_keeps_placeholder(self, tag_env):
        ids = content_ids(tag_env.session_factory)
        resp = tag_env.client.get(f"/contents/{ids['治愈漫画乙']}")
        assert resp.status_code == 200
        assert "暂无其他标签数据" in resp.text
        assert 'class="tag-chip"' not in resp.text


class TestApiTagFilter:
    def test_api_tag_filter_alone(self, tag_env):
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/api/contents?tag=机甲")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        titles = {item["title"] for item in body["items"]}
        assert titles == {"机甲动画甲", "机甲游戏丙"}

    def test_api_tag_combined_with_type(self, tag_env):
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/api/contents?type=game&tag=机甲")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "机甲游戏丙"

    def test_api_tag_unknown_returns_empty(self, tag_env):
        seed_tags(tag_env.session_factory)
        resp = tag_env.client.get("/api/contents?tag=不存在")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}
