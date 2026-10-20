"""个人空间测试：用户信息 / 简介 / 作品收藏 / 浏览记录。

覆盖：
- 登录守卫：游客访问 /me 跳登录页（next 带回），已登录（普通用户 / 管理员）可进；
- 资料 / 简介：已迁到账号设置（见 tests/test_account_settings.py），本页只留入口；
- 浏览记录：详情页自动写入、重复浏览不堆叠、游客不记录、跨账号隔离、
  单条删除、清空、每人上限裁剪；
- Tab 非法值回退到收藏；
- 收藏分类：按内容类型筛选（追番 / 漫画 / 游戏 / 轻小说）、分类计数、分类空态、
  取消与清空保留当前分类、分页保留分类、非法分类回退为全部；
- 作品标注（已并入收藏行）：看过 / 未看 / 想看的设置、更新幂等、跨账号隔离、
  标注自动加入收藏、取消收藏保留标注、清除标注保留收藏、未标注筛选、
  收藏列表行内切换与分页保留筛选、历史库标注表迁移。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
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
        assert "账号设置" in resp.text
        assert "编辑资料" not in resp.text

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


# ---------- 收藏分类（追番 / 追漫 / 追书 / 追游） ----------


class TestFavoriteCategories:
    """收藏按内容类型分开：各分类可单独查看，取消/清空/分页都保留当前分类。"""

    def test_category_filter_shows_only_matching_type(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})  # 动画
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})  # 游戏

        anime_page = space_env.client.get("/me?tab=favorites&type=anime")
        assert anime_page.status_code == 200
        assert "星海航线" in anime_page.text
        assert "迷宫回响" not in anime_page.text
        assert "共 1 条追番" in anime_page.text

        game_page = space_env.client.get("/me?tab=favorites&type=game")
        assert "迷宫回响" in game_page.text
        assert "星海航线" not in game_page.text
        assert "共 1 条游戏" in game_page.text

    def test_category_counts_on_chips(self, space_env):
        login_as_user(space_env.client)
        for content_id in (1, 6, 3):  # 动画 ×2 + 游戏 ×1
            space_env.client.post(
                f"/contents/{content_id}/favorite",
                data={"next": f"/contents/{content_id}"},
            )

        page = space_env.client.get("/me?tab=favorites")
        assert "全部<span class=\"tag-count\">3</span>" in page.text
        assert "追番<span class=\"tag-count\">2</span>" in page.text
        assert "游戏<span class=\"tag-count\">1</span>" in page.text
        # 没有收藏的分类也展示为 0，便于一眼看出分布
        assert "漫画<span class=\"tag-count\">0</span>" in page.text
        assert "轻小说<span class=\"tag-count\">0</span>" in page.text

    def test_category_empty_state(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})

        page = space_env.client.get("/me?tab=favorites&type=comic")
        assert "还没有漫画" in page.text
        assert "/contents?type=comic" in page.text
        # 有其它分类的收藏时，不再显示全局空态文案
        assert "还没有收藏任何作品" not in page.text

    def test_invalid_category_falls_back_to_all(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})

        page = space_env.client.get("/me?tab=favorites&type=unknown")
        assert page.status_code == 200
        assert "星海航线" in page.text and "迷宫回响" in page.text
        assert "共 2 条收藏" in page.text

    def test_remove_favorite_preserves_category(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})

        resp = space_env.client.post(
            "/me/favorites/1/remove", data={"cat": "anime"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=favorites&fav_removed=1&type=anime"

        page = space_env.client.get(resp.headers["location"])
        assert "星海航线" not in page.text
        assert "还没有追番" in page.text

    def test_remove_ignores_unknown_category(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})

        resp = space_env.client.post(
            "/me/favorites/1/remove", data={"cat": "evil"}, follow_redirects=False
        )
        assert resp.status_code == 303
        # 非已知分类不会被拼进跳转地址
        assert resp.headers["location"] == "/me?tab=favorites&fav_removed=1"

    def test_clear_category_only(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})  # 动画
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})  # 游戏

        resp = space_env.client.post(
            "/me/favorites/clear", data={"cat": "anime"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=favorites&fav_cleared=1&type=anime"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.count_favorites(db, uid) == 1
            assert crud.count_favorites(db, uid, content_type="anime") == 0
            assert crud.count_favorites(db, uid, content_type="game") == 1
        finally:
            db.close()

        # 未清空的分类仍然保留
        page = space_env.client.get("/me?tab=favorites&type=game")
        assert "迷宫回响" in page.text

    def test_pager_preserves_category(self, space_env):
        login_as_user(space_env.client)
        extra = [
            _seed_content(space_env.session_factory, f"分页动画 {i}")
            for i in range(10)
        ]
        for content_id in [1, 6, *extra]:  # 12 部动画 → 2 页
            space_env.client.post(
                f"/contents/{content_id}/favorite",
                data={"next": f"/contents/{content_id}"},
            )

        page = space_env.client.get("/me?tab=favorites&type=anime")
        assert "共 12 条追番" in page.text
        assert "type=anime" in page.text
        assert "page=2" in page.text


# ---------- 作品标注（看过 / 未看 / 想看） ----------


class TestContentStatus:
    """作品标注与收藏同一行：标注自动收藏，取消收藏 / 清除标注都不互相级联。"""

    def test_detail_shows_status_selector_for_user(self, space_env):
        login_as_user(space_env.client)
        page = space_env.client.get("/contents/1")
        assert page.status_code == 200
        assert 'action="/contents/1/status"' in page.text
        assert "看过" in page.text and "未看" in page.text and "想看" in page.text

    def test_guest_sees_status_login_prompt(self, space_env):
        page = space_env.client.get("/contents/1")
        assert "登录后可标注" in page.text
        assert 'action="/contents/1/status"' not in page.text

    def test_set_status_persists_and_auto_favorites(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.post(
            "/contents/1/status",
            data={"status": "seen", "next": "/contents/1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/contents/1?status_ok=1"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_content_status(db, uid, 1) == "seen"
            # 标注过的作品自动进入收藏名单，不需要再点一次收藏
            assert crud.is_favorite(db, uid, 1) is True
            assert crud.count_favorites(db, uid) == 1
        finally:
            db.close()

        page = space_env.client.get("/contents/1?status_ok=1")
        assert "已收藏并标注为「看过」" in page.text
        # 已标注的作品在详情页给出「清除标注」入口，链接指向收藏列表
        assert 'action="/contents/1/status/clear"' in page.text
        assert 'href="/me?tab=favorites"' in page.text

    def test_status_update_is_idempotent(self, space_env):
        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            crud.set_content_status(db, uid, 1, "seen")
            crud.set_content_status(db, uid, 1, "want")
            assert crud.get_content_status(db, uid, 1) == "want"
            # 同一作品只保留一条关系行：更新而非堆叠
            assert crud.count_content_status(db, uid) == 1
            assert crud.count_favorites(db, uid) == 1
        finally:
            db.close()

    def test_invalid_status_ignored(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.post(
            "/contents/1/status",
            data={"status": "bogus", "next": "/contents/1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/contents/1?status_err=1"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_content_status(db, uid, 1) is None
            # 非法标注不落库，也不会把作品塞进收藏
            assert crud.is_favorite(db, uid, 1) is False
        finally:
            db.close()

    def test_status_isolated_between_users(self, space_env):
        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            crud.set_content_status(db, uid, 1, "seen")
        finally:
            db.close()

        admin_uid = _user_id(space_env.session_factory, ADMIN_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_content_status(db, admin_uid, 1) is None
        finally:
            db.close()

    def test_status_missing_content_returns_404(self, space_env):
        login_as_user(space_env.client)
        resp = space_env.client.post(
            "/contents/9999/status",
            data={"status": "seen", "next": "/contents/9999"},
            follow_redirects=False,
        )
        assert resp.status_code == 404
        resp = space_env.client.post(
            "/contents/9999/status/clear",
            data={"next": "/contents/9999"},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_favorites_tab_lists_and_filters_by_status(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post(
            "/contents/1/status", data={"status": "seen", "next": "/contents/1"}
        )
        space_env.client.post(
            "/contents/3/status", data={"status": "want", "next": "/contents/3"}
        )

        all_page = space_env.client.get("/me?tab=favorites")
        assert "星海航线" in all_page.text and "迷宫回响" in all_page.text
        assert "共 2 条收藏" in all_page.text
        assert "看过<span class=\"tag-count\">1</span>" in all_page.text
        assert "想看<span class=\"tag-count\">1</span>" in all_page.text

        seen_page = space_env.client.get("/me?tab=favorites&status=seen")
        assert "星海航线" in seen_page.text
        assert "迷宫回响" not in seen_page.text
        assert "共 1 条收藏（看过）" in seen_page.text

    def test_favorites_tab_empty_state(self, space_env):
        login_as_user(space_env.client)
        page = space_env.client.get("/me?tab=favorites")
        assert "还没有收藏任何作品" in page.text
        # 标注不再是独立 Tab：旧地址回退到收藏
        assert space_env.client.get("/me?tab=marks").status_code == 200

    def test_unmarked_filter(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        space_env.client.post(
            "/contents/3/status", data={"status": "seen", "next": "/contents/3"}
        )

        page = space_env.client.get("/me?tab=favorites&status=unmarked")
        assert "星海航线" in page.text  # 收藏了但没标注
        assert "迷宫回响" not in page.text  # 标注过的过滤掉
        assert "共 1 条收藏（未标注）" in page.text
        assert "未标注<span class=\"tag-count\">1</span>" in page.text

    def test_invalid_status_filter_falls_back(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post(
            "/contents/1/status", data={"status": "seen", "next": "/contents/1"}
        )
        page = space_env.client.get("/me?tab=favorites&status=bogus")
        assert "星海航线" in page.text
        assert "共 1 条收藏" in page.text

    def test_unfavorite_keeps_status(self, space_env):
        """取消收藏只移出名单，标注不丢（收藏了但没看是常态）。"""
        login_as_user(space_env.client)
        space_env.client.post(
            "/contents/1/status", data={"status": "seen", "next": "/contents/1"}
        )
        resp = space_env.client.post(
            "/contents/1/favorite", data={"next": "/contents/1"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "fav=removed" in resp.headers["location"]

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.is_favorite(db, uid, 1) is False
            assert crud.get_content_status(db, uid, 1) == "seen"
        finally:
            db.close()

        # 名单里没有了，详情页仍然显示已标注
        assert "星海航线" not in space_env.client.get("/me?tab=favorites").text
        assert "已取消收藏；标注仍保留" in space_env.client.get(
            "/contents/1?fav=removed"
        ).text

        # 重新收藏：旧标注原样恢复可见
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        page = space_env.client.get("/me?tab=favorites")
        assert "星海航线" in page.text
        assert '<span class="badge bg-success">看过</span>' in page.text

    def test_clear_status_keeps_favorite(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post(
            "/contents/1/status", data={"status": "want", "next": "/contents/1"}
        )
        resp = space_env.client.post(
            "/contents/1/status/clear",
            data={"next": "/me?tab=favorites"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=favorites&status_cleared=1"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_content_status(db, uid, 1) is None
            assert crud.is_favorite(db, uid, 1) is True
        finally:
            db.close()

        page = space_env.client.get("/me?tab=favorites&status_cleared=1")
        assert "星海航线" in page.text
        assert "已清除标注，作品仍在收藏里" in page.text
        # 详情页的「清除标注」入口随之消失（已无标注可清）
        detail = space_env.client.get("/contents/1")
        assert 'action="/contents/1/status/clear"' not in detail.text

    def test_clear_favorites_keeps_status(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post(
            "/contents/1/status", data={"status": "seen", "next": "/contents/1"}
        )
        space_env.client.post("/contents/3/favorite", data={"next": "/contents/3"})
        space_env.client.post("/me/favorites/clear", data={})

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.count_favorites(db, uid) == 0
            assert crud.get_content_status(db, uid, 1) == "seen"
        finally:
            db.close()

    def test_favorite_card_shows_status_badge(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        space_env.client.post(
            "/contents/1/status", data={"status": "seen", "next": "/contents/1"}
        )
        page = space_env.client.get("/me?tab=favorites")
        assert '<span class="badge bg-success">看过</span>' in page.text

    def test_inline_status_switch(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post(
            "/contents/1/status", data={"status": "seen", "next": "/contents/1"}
        )
        # 收藏列表里直接把「看过」改成「想看」，回到收藏 Tab
        resp = space_env.client.post(
            "/contents/1/status",
            data={"status": "want", "next": "/me?tab=favorites"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=favorites&status_ok=1"

        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            assert crud.get_content_status(db, uid, 1) == "want"
        finally:
            db.close()

        page = space_env.client.get("/me?tab=favorites&status_ok=1")
        assert "已更新标注" in page.text

    def test_pager_preserves_type_and_status(self, space_env):
        login_as_user(space_env.client)
        uid = _user_id(space_env.session_factory, USER_USERNAME)
        content_ids = [
            _seed_content(space_env.session_factory, f"标注分页 {i}")
            for i in range(12)
        ]
        db = space_env.session_factory()
        try:
            for content_id in content_ids:
                crud.set_content_status(db, uid, content_id, "want")
        finally:
            db.close()

        page = space_env.client.get("/me?tab=favorites&status=want")
        assert "共 12 条收藏（想看）" in page.text
        assert "status=want" in page.text
        assert "page=2" in page.text

    def test_single_all_chip_and_status_toggle(self, space_env):
        """筛选只留一个「全部」：分类行那个同时清掉分类与标注；
        标注行不再重复挂「全部」，选中项自己就是开关（再点一次取消标注筛选）。"""
        login_as_user(space_env.client)
        uid = _user_id(space_env.session_factory, USER_USERNAME)
        db = space_env.session_factory()
        try:
            crud.set_content_status(db, uid, 1, "seen")
            crud.set_content_status(db, uid, 3, "want")
        finally:
            db.close()

        page = space_env.client.get("/me?tab=favorites&status=seen")
        # 页面上只出现一个「全部」chip（旧的标注行还有第二个）
        assert page.text.count('全部<span class="tag-count">') == 1
        # 分类行的「全部」是真·全部：链接不带 type / status
        assert 'href="/me?tab=favorites"' in page.text
        # 选中的「看过」chip 变成开关，链接回到不带 status 的地址
        assert re.search(r'class="tag-chip active"\s+href="/me\?tab=favorites"', page.text)
        # 未选中的标注 chip 仍然带上自己的 status
        assert "status=unmarked" in page.text and "status=want" in page.text

    def test_unmarked_chip_is_toggle_when_active(self, space_env):
        login_as_user(space_env.client)
        space_env.client.post("/contents/1/favorite", data={"next": "/contents/1"})
        page = space_env.client.get("/me?tab=favorites&status=unmarked")
        assert re.search(r'class="tag-chip active"\s+href="/me\?tab=favorites"', page.text)


class TestFavoriteStatusMigration:
    """历史库迁移：收藏行补列，旧标注表 content_status 搬入后删掉。"""

    def _legacy_engine(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE content_favorites ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "content_id INTEGER NOT NULL, created_at DATETIME NOT NULL, "
                    "UNIQUE (user_id, content_id))"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE content_status ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "content_id INTEGER NOT NULL, status VARCHAR(20) NOT NULL, "
                    "updated_at DATETIME NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO content_favorites (user_id, content_id, created_at) "
                    "VALUES (1, 6, '2026-01-01 10:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO content_status "
                    "(user_id, content_id, status, updated_at) "
                    "VALUES (1, 5, 'seen', '2026-01-02 10:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO content_status "
                    "(user_id, content_id, status, updated_at) "
                    "VALUES (1, 6, 'want', '2026-01-03 10:00:00')"
                )
            )
        return engine

    def test_migrates_legacy_marks_into_favorites(self, tmp_path):
        engine = self._legacy_engine(tmp_path)
        try:
            added = crud.ensure_favorite_status_columns(engine)
            assert set(added) == {"favorited", "status", "status_updated_at"}

            with engine.begin() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        text("SELECT name FROM sqlite_master WHERE type = 'table'")
                    )
                }
                assert "content_status" not in tables
                rows = {
                    content_id: (favorited, status, created_at)
                    for content_id, favorited, status, created_at in connection.execute(
                        text(
                            "SELECT content_id, favorited, status, created_at "
                            "FROM content_favorites ORDER BY content_id"
                        )
                    )
                }
            # 只有标注的作品补建收藏行；两者都有的保留原收藏时间并补状态
            assert set(rows) == {5, 6}
            assert rows[5][0] == 1 and rows[5][1] == "seen"
            assert rows[6][0] == 1 and rows[6][1] == "want"
            assert str(rows[6][2]).startswith("2026-01-01 10:00:00")
        finally:
            engine.dispose()

    def test_migration_is_idempotent(self, tmp_path):
        engine = self._legacy_engine(tmp_path)
        try:
            crud.ensure_favorite_status_columns(engine)
            assert crud.ensure_favorite_status_columns(engine) == []
        finally:
            engine.dispose()

    def test_fresh_schema_skips_migration(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
        try:
            Base.metadata.create_all(engine)
            assert crud.ensure_favorite_status_columns(engine) == []
        finally:
            engine.dispose()
