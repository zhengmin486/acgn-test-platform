"""资讯收藏测试（2026-10-24 新增功能）。

覆盖：
- 登录守卫：游客在资讯详情页点收藏 -> 跳登录页（next 带回资讯地址）；
- 收藏 / 取消：详情页切换、重复收藏幂等、取消后列表消失；
- 个人空间「资讯收藏」Tab：列表展示、徽标计数、单条取消、清空；
- 跨账号隔离：A 收藏的资讯不出现在 B 的列表里；
- 资讯被删后关系行保留但列表不展示（内连接）。

数据落在独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from app.models import Article
from auth_helpers import (
    USER_USERNAME,
    create_accounts,
    login_as_admin,
    login_as_user,
)


@pytest.fixture
def env(tmp_path):
    """独立临时库 + 种子账号 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'article_fav.db'}",
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


def _db(env):
    """测试里需要同步会话时临时开一个（进程退出即释放）。"""
    return env.session_factory()


def _seed_article(session_factory, title: str) -> int:
    """直接写一条资讯，返回其 id。"""
    db = session_factory()
    try:
        item = Article(
            title=title, category="动画", summary="测试用资讯摘要。", body="测试用正文。"
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return item.id
    finally:
        db.close()


# ---------- 登录守卫 ----------


class TestArticleFavoriteLoginGate:
    def test_guest_redirected_to_login_with_next(self, env):
        article_id = _seed_article(env.session_factory, "游客也能看的资讯")
        resp = env.client.post(f"/articles/{article_id}/favorite")
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next=/articles/{article_id}"

    def test_guest_sees_login_gate_button(self, env):
        article_id = _seed_article(env.session_factory, "游客看到的详情页")
        resp = env.client.get(f"/articles/{article_id}")
        assert resp.status_code == 200
        assert 'data-login-gate="收藏"' in resp.text
        # 未登录不渲染收藏表单（点了才弹登录窗）
        assert f'action="/articles/{article_id}/favorite"' not in resp.text


# ---------- 收藏 / 取消 ----------


class TestToggleArticleFavorite:
    def test_add_then_remove(self, env):
        login_as_user(env.client)
        article_id = _seed_article(env.session_factory, "可以收藏的资讯")
        uid = _user_id(env.session_factory, USER_USERNAME)

        resp = env.client.post(f"/articles/{article_id}/favorite")
        assert resp.status_code == 303
        assert "fav=added" in resp.headers["location"]
        assert crud.is_article_favorite(_db(env), uid, article_id) is True

        resp = env.client.post(f"/articles/{article_id}/favorite")
        assert "fav=removed" in resp.headers["location"]
        assert crud.is_article_favorite(_db(env), uid, article_id) is False

    def test_repeat_add_is_idempotent(self, env):
        login_as_user(env.client)
        article_id = _seed_article(env.session_factory, "重复收藏的资讯")
        uid = _user_id(env.session_factory, USER_USERNAME)
        db = _db(env)
        assert crud.add_article_favorite(db, uid, article_id) is True
        assert crud.add_article_favorite(db, uid, article_id) is False
        assert crud.count_article_favorites(db, uid) == 1

    def test_detail_page_reflects_state(self, env):
        login_as_user(env.client)
        article_id = _seed_article(env.session_factory, "详情页状态")
        assert "收藏资讯" in env.client.get(f"/articles/{article_id}").text
        env.client.post(f"/articles/{article_id}/favorite")
        assert "取消收藏" in env.client.get(f"/articles/{article_id}").text

    def test_missing_article_renders_404(self, env):
        login_as_user(env.client)
        resp = env.client.post("/articles/999999/favorite")
        assert resp.status_code == 404


# ---------- 个人空间「资讯收藏」Tab ----------


class TestSpaceArticleTab:
    def test_tab_lists_favorited_articles(self, env):
        login_as_user(env.client)
        first = _seed_article(env.session_factory, "收藏的第一条")
        second = _seed_article(env.session_factory, "收藏的第二条")
        env.client.post(f"/articles/{first}/favorite")
        env.client.post(f"/articles/{second}/favorite")

        page = env.client.get("/me?tab=articles").text
        assert "收藏的第一条" in page
        assert "收藏的第二条" in page
        assert "资讯收藏（2）" in page

    def test_tab_empty_state(self, env):
        login_as_user(env.client)
        page = env.client.get("/me?tab=articles").text
        assert "还没有收藏资讯" in page
        assert "资讯收藏（0）" in page

    def test_remove_from_space(self, env):
        login_as_user(env.client)
        article_id = _seed_article(env.session_factory, "待取消的资讯")
        env.client.post(f"/articles/{article_id}/favorite")
        resp = env.client.post(f"/me/article-favorites/{article_id}/remove")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/me?tab=articles&fav_removed=1"
        assert "待取消的资讯" not in env.client.get("/me?tab=articles").text

    def test_clear_all(self, env):
        login_as_user(env.client)
        for index in range(3):
            article_id = _seed_article(env.session_factory, f"批量资讯{index}")
            env.client.post(f"/articles/{article_id}/favorite")
        resp = env.client.post("/me/article-favorites/clear")
        assert resp.status_code == 303
        assert "fav_cleared=3" in resp.headers["location"]
        assert "资讯收藏（0）" in env.client.get("/me?tab=articles").text

    def test_deleted_article_not_listed(self, env):
        """资讯被删后关系行保留，但列表内连接查不到，不展示。"""
        login_as_user(env.client)
        article_id = _seed_article(env.session_factory, "会被删掉的资讯")
        env.client.post(f"/articles/{article_id}/favorite")
        db = _db(env)
        db.delete(db.get(Article, article_id))
        db.commit()
        page = env.client.get("/me?tab=articles").text
        assert "会被删掉的资讯" not in page
        assert "资讯收藏（0）" in page


# ---------- 跨账号隔离 ----------


class TestArticleFavoriteIsolation:
    def test_other_users_list_is_empty(self, env):
        article_id = _seed_article(env.session_factory, "只属于某人的资讯")
        login_as_user(env.client)
        env.client.post(f"/articles/{article_id}/favorite")

        admin_client = TestClient(app, follow_redirects=False)
        login_as_admin(admin_client)
        page = admin_client.get("/me?tab=articles").text
        assert "只属于某人的资讯" not in page
        assert "资讯收藏（0）" in page

    def test_admin_can_favorite_same_article(self, env):
        article_id = _seed_article(env.session_factory, "两人都能收藏的资讯")
        login_as_user(env.client)
        env.client.post(f"/articles/{article_id}/favorite")
        login_as_admin(env.client)
        env.client.post(f"/articles/{article_id}/favorite")
        assert "两人都能收藏的资讯" in env.client.get("/me?tab=articles").text
