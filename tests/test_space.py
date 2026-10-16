"""个人空间测试：用户信息 / 简介 / 作品收藏 / 浏览记录。

覆盖：
- 登录守卫：游客访问 /me 跳登录页（next 带回），已登录（普通用户 / 管理员）可进；
- 简介：保存 / 超长回错且不落库 / 空串清除；
- 收藏：详情页切换收藏、空间列表与统计、单条取消、清空、幂等、跨账号隔离、404；
- 浏览记录：详情页自动写入、重复浏览不堆叠、游客不记录、跨账号隔离、
  单条删除、清空、每人上限裁剪；
- Tab 非法值回退到收藏。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from app.models import Content
from auth_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    USER_USERNAME,
    create_accounts,
    login,
    login_as_admin,
    login_as_user,
)


@pytest.fixture
def space_env(tmp_path):
    """独立临时库 + 种子作品 + 管理员/普通用户账号 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'space.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        crud.seed_if_empty(db)
    finally:
        db.close()
    create_accounts(TestingSession)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SimpleNamespace(
            client=TestClient(app, follow_redirects=False),
            session_factory=TestingSession,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _user_id(session_factory, username: str) -> int:
    db = session_factory()
    try:
        return crud.get_user_by_username(db, username).id
    finally:
        db.close()


def _seed_content(session_factory, title: str) -> int:
    """直接写一部作品，返回其 id（供上限裁剪等批量造数场景）。"""
    db = session_factory()
    try:
        item = Content(title=title, type="anime", rating=7.5, summary="测试用作品条目。")
        db.add(item)
        db.commit()
        db.refresh(item)
        return item.id
    finally:
        db.close()


# ---------- 访问控制 ----------


class TestSpaceAccess:
    def test_guest_redirected_to_login(self, space_env):
        resp = space_env.client.get("/me")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=/me"

    def test_user_can_open_space(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.get("/me")
        assert resp.status_code == 200
        assert "个人空间" in resp.text
        assert USER_USERNAME in resp.text
        assert "作品收藏" in resp.text and "浏览记录" in resp.text
        assert "个人简介" in resp.text

    def test_admin_can_open_space(self, space_env):
        # 个人空间不限制角色：管理员同样可用
        login_as_admin(space_env.client)
        assert space_env.client.get("/me").status_code == 200

    def test_nav_shows_space_entry_after_login(self, space_env):
        login_as_user(space_env.client)
        home = space_env.client.get("/")
        assert 'href="/me"' in home.text

    def test_invalid_tab_falls_back_to_favorites(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.get("/me?tab=unknown")
        assert resp.status_code == 200
        # 回退到收藏 Tab：展示收藏空态而非浏览记录空态
        assert "还没有收藏任何作品" in resp.text


# ---------- 个人简介 ----------


class TestProfileBio:
    def test_save_bio_and_persist(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.post(
            "/me/profile", data={"bio": "喜欢治愈系作品。"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?bio=ok"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_user(db, uid).bio == "喜欢治愈系作品。"
        finally:
            db.close()

        page = space_env.client.get("/me")
        assert "喜欢治愈系作品。" in page.text

    def test_bio_too_long_rejected(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/me/profile", data={"bio": "初始简介"})
        resp = space_env.client.post(
            "/me/profile", data={"bio": "长" * (crud.MAX_BIO_LENGTH + 1)}
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?bio=error"

        # 跟随 303 回跳（浏览器行为）：错误回显 + 原简介保持
        page = space_env.client.get(resp.headers["location"])
        assert "简介保存失败" in page.text
        assert "初始简介" in page.text

    def test_empty_bio_clears(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/me/profile", data={"bio": "临时简介"})
        resp = space_env.client.post("/me/profile", data={"bio": ""})
        assert resp.headers["location"] == "/me?bio=ok"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_user(db, uid).bio is None
        finally:
            db.close()
        assert "还没有填写简介" in space_env.client.get("/me").text


# ---------- 作品收藏 ----------


class TestFavorites:
    def test_detail_page_shows_favorite_button(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.get("/contents/1")
        assert resp.status_code == 200
        assert "收藏作品" in resp.text
        assert 'action="/contents/1/favorite"' in resp.text

    def test_guest_sees_login_prompt(self, space_env):
        resp = space_env.client.get("/contents/1")
        assert "登录后可收藏作品" in resp.text

    def test_toggle_favorite_roundtrip(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.post(
            "/contents/1/favorite", data={"next": "/contents/1"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/contents/1?fav=added"
        assert "取消收藏" in space_env.client.get("/contents/1").text

        resp2 = space_env.client.post(
            "/contents/1/favorite", data={"next": "/contents/1"}, follow_redirects=False
        )
        assert resp2.headers["location"] == "/contents/1?fav=removed"
        assert "收藏作品" in space_env.client.get("/contents/1").text

    def test_favorite_listed_in_space(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})

        page = space_env.client.get("/me?tab=favorites")
        assert "星海航线" in page.text
        assert "迷宫回响" in page.text
        assert "共 2 条收藏" in page.text

        # 统计卡同步反映收藏数
        assert '<div class="stat-num">2</div>' in page.text

    def test_favorite_isolated_between_users(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})

        # 同一 client 切换为管理员：看不到普通用户的收藏
        login(space_env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        page = space_env.client.get("/me?tab=favorites")
        assert "星海航线" not in page.text
        assert "还没有收藏任何作品" in page.text

    def test_remove_favorite_from_space(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})

        resp = space_env.client.post(
            "/me/favorites/1/remove", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=favorites&fav_removed=1"

        # 跟随 303 回跳（浏览器行为）：取消回显 + 列表已移除
        page = space_env.client.get(resp.headers["location"])
        assert "星海航线" not in page.text
        assert "已取消收藏" in page.text

    def test_clear_favorites(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})

        resp = space_env.client.post("/me/favorites/clear", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=favorites&fav_cleared=2"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.count_favorites(db, uid) == 0
        finally:
            db.close()

    def test_add_favorite_idempotent(self, space_env):
        # 重复收藏不产生第二行；重复取消返回 False
        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.add_favorite(db, uid, 1) is True
            assert crud.add_favorite(db, uid, 1) is False
            assert crud.count_favorites(db, uid) == 1
            assert crud.remove_favorite(db, uid, 1) is True
            assert crud.remove_favorite(db, uid, 1) is False
        finally:
            db.close()

    def test_favorite_missing_content_returns_404(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.post(
            "/contents/9999/favorite", data={"next": "/contents/9999"},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------- 浏览记录 ----------


class TestViewHistory:
    def test_detail_visit_records_history(self, space_env):
        login_as_user(space_env.client)
        space_env.client.get("/contents/1")
        space_env.client.get("/contents/3")

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            total, history = crud.list_view_history(db, uid)
            assert total == 2
            assert {item.id for item, _ in history} == {1, 3}
        finally:
            db.close()

        page = space_env.client.get("/me?tab=history")
        assert "星海航线" in page.text and "迷宫回响" in page.text
        assert "共 2 条记录" in page.text

    def test_repeat_visit_does_not_duplicate(self, space_env):
        login_as_user(space_env.client)
        space_env.client.get("/contents/1")
        space_env.client.get("/contents/1")

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.count_view_history(db, uid) == 1
        finally:
            db.close()

    def test_guest_visit_not_recorded(self, space_env):
        space_env.client.get("/contents/1")

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.count_view_history(db, uid) == 0
        finally:
            db.close()

    def test_history_isolated_between_users(self, space_env):
        login_as_user(space_env.client)
        space_env.client.get("/contents/1")

        login(space_env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        page = space_env.client.get("/me?tab=history")
        assert "星海航线" not in page.text
        assert "还没有浏览记录" in page.text

    def test_remove_history_item(self, space_env):
        login_as_user(space_env.client)
        space_env.client.get("/contents/1")

        resp = space_env.client.post(
            "/me/history/1/remove", follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=history&history_removed=1"

        page = space_env.client.get("/me?tab=history")
        assert "星海航线" not in page.text
        assert "还没有浏览记录" in page.text

    def test_clear_history(self, space_env):
        login_as_user(space_env.client)
        space_env.client.get("/contents/1")
        space_env.client.get("/contents/3")

        resp = space_env.client.post("/me/history/clear", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=history&history_cleared=2"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.count_view_history(db, uid) == 0
        finally:
            db.close()

    def test_history_capped_per_user(self, space_env):
        # 超过 VIEW_HISTORY_LIMIT 后裁剪最旧记录，保留最近 N 条
        uid = _user_id(space_env.session_factory, USER_USERNAME)
        content_ids = [_seed_content(space_env.session_factory, f"上限作品 {i}")
                       for i in range(crud.VIEW_HISTORY_LIMIT + 5)]

        db = space_env.session_factory()
        try:
            for content_id in content_ids:
                crud.record_content_view(db, uid, content_id)
            total, history = crud.list_view_history(db, uid, page=1, page_size=200)
            assert total == crud.VIEW_HISTORY_LIMIT
            kept = [item.id for item, _ in history]
            assert content_ids[0] not in kept      # 最旧一条被裁掉
            assert content_ids[-1] in kept          # 最新一条保留
        finally:
            db.close()
