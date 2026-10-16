"""认证与访问控制测试：登录、登出、角色守卫与管理 API 保护。

覆盖：
- 登录成功/失败、登出、开放重定向防护、已登录访问登录页；
- 未登录访问管理页面（跳登录页）与普通用户访问管理页面（403）；
- 管理 API 的 401/403 与管理员放行；
- 内容平台对未登录/普通用户/管理员均可用，导航按角色渲染；
- 密码哈希与会话令牌的单元行为。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.security import (
    create_session_token,
    hash_password,
    read_session_token,
    verify_password,
)
from auth_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_USERNAME,
    create_accounts,
    login,
    login_as_admin,
    login_as_user,
)

# 仅管理员可访问的测试管理页面
ADMIN_PAGES = ["/testcases", "/runs", "/batches", "/schedule/runs"]

# 仅管理员可访问的测试管理接口（方法, 路径）
ADMIN_APIS = [
    ("GET", "/api/testcases"),
    ("POST", "/api/testcases"),
    ("GET", "/api/runs"),
    ("GET", "/api/batches"),
    ("POST", "/api/batches"),
    ("GET", "/api/batches/1/regression"),
    ("GET", "/api/schedule/runs"),
]

# 内容平台页面（未登录也应可访问）
CONTENT_PAGES = ["/", "/articles", "/schedule"]


@pytest.fixture
def auth_env(tmp_path):
    """独立临时库 + 管理员/普通用户账号 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
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


# ---------- 登录 / 登出 ----------


class TestLogin:
    def test_login_page_renders_form(self, auth_env):
        resp = auth_env.client.get("/login")
        assert resp.status_code == 200
        assert 'action="/login"' in resp.text
        assert 'name="username"' in resp.text and 'name="password"' in resp.text

    def test_login_success_sets_session_and_redirects(self, auth_env):
        resp = login(auth_env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert auth_env.client.cookies.get("acgn_session")

        home = auth_env.client.get("/")
        assert home.status_code == 200
        assert ADMIN_USERNAME in home.text  # 导航显示当前登录用户

    def test_login_returns_to_requested_page(self, auth_env):
        # 未登录访问管理页面：跳登录页并带上返回地址
        denied = auth_env.client.get("/testcases", follow_redirects=False)
        assert denied.status_code == 303
        assert denied.headers["location"] == "/login?next=/testcases"

        # 登录时携带 next，成功后直接回到目标页面
        resp = auth_env.client.post(
            "/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "/testcases"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/testcases"
        assert auth_env.client.get("/testcases", follow_redirects=False).status_code == 200

    def test_login_wrong_password_rejected(self, auth_env):
        resp = login(auth_env.client, ADMIN_USERNAME, "wrong-password")
        assert resp.status_code == 400
        assert "用户名或密码不正确" in resp.text
        assert not auth_env.client.cookies.get("acgn_session")

    def test_login_unknown_user_rejected(self, auth_env):
        resp = login(auth_env.client, "no-such-user", ADMIN_PASSWORD)
        assert resp.status_code == 400
        assert not auth_env.client.cookies.get("acgn_session")

    def test_login_requires_confirmation_before_entering_admin(self, auth_env):
        """普通用户登录成功后仍未获得管理页面访问权。"""
        login_as_user(auth_env.client)
        resp = auth_env.client.get("/testcases", follow_redirects=False)
        assert resp.status_code == 403

    def test_login_rejects_external_next_url(self, auth_env):
        """开放重定向防护：站外地址被忽略，回到首页。"""
        resp = auth_env.client.post(
            "/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "next": "https://evil.example/steal",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_login_rejects_protocol_relative_next_url(self, auth_env):
        resp = auth_env.client.post(
            "/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "next": "//evil.example"},
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/"

    def test_logged_in_user_visiting_login_is_redirected(self, auth_env):
        login_as_admin(auth_env.client)
        resp = auth_env.client.get("/login", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_tampered_session_cookie_is_ignored(self, auth_env):
        login_as_admin(auth_env.client)
        auth_env.client.cookies.set("acgn_session", "tampered-value")
        resp = auth_env.client.get("/testcases", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


class TestLogout:
    def test_logout_clears_session(self, auth_env):
        login_as_admin(auth_env.client)
        assert auth_env.client.get("/testcases").status_code == 200

        resp = auth_env.client.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        denied = auth_env.client.get("/testcases", follow_redirects=False)
        assert denied.status_code == 303
        assert denied.headers["location"].startswith("/login")

    def test_logout_via_link_works(self, auth_env):
        login_as_admin(auth_env.client)
        resp = auth_env.client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert auth_env.client.get("/testcases", follow_redirects=False).status_code == 303


# ---------- 管理页面保护 ----------


class TestAdminPageGuard:
    @pytest.mark.parametrize("path", ADMIN_PAGES)
    def test_anonymous_is_redirected_to_login(self, auth_env, path):
        resp = auth_env.client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/login?next={path}"

    @pytest.mark.parametrize("path", ADMIN_PAGES)
    def test_normal_user_gets_403(self, auth_env, path):
        login_as_user(auth_env.client)
        resp = auth_env.client.get(path, follow_redirects=False)
        assert resp.status_code == 403
        assert "无权访问测试管理平台" in resp.text

    @pytest.mark.parametrize("path", ADMIN_PAGES)
    def test_admin_can_open(self, auth_env, path):
        login_as_admin(auth_env.client)
        resp = auth_env.client.get(path, follow_redirects=False)
        assert resp.status_code == 200
        assert "测试管理平台" in resp.text

    def test_normal_user_cannot_post_to_admin_page(self, auth_env):
        """POST 管理操作同样被拒绝，不能只靠隐藏按钮。"""
        login_as_user(auth_env.client)
        resp = auth_env.client.post("/testcases/execute-all", follow_redirects=False)
        assert resp.status_code == 403

    def test_admin_detail_pages_are_protected_too(self, auth_env):
        resp = auth_env.client.get("/runs/1", follow_redirects=False)
        assert resp.status_code == 303
        login_as_user(auth_env.client)
        assert auth_env.client.get("/runs/1", follow_redirects=False).status_code == 403


# ---------- 管理 API 保护 ----------


class TestAdminApiGuard:
    @pytest.mark.parametrize(("method", "path"), ADMIN_APIS)
    def test_anonymous_gets_401(self, auth_env, method, path):
        resp = auth_env.client.request(method, path, follow_redirects=False)
        assert resp.status_code == 401

    @pytest.mark.parametrize(("method", "path"), ADMIN_APIS)
    def test_normal_user_gets_403(self, auth_env, method, path):
        login_as_user(auth_env.client)
        resp = auth_env.client.request(method, path, follow_redirects=False)
        assert resp.status_code == 403

    @pytest.mark.parametrize(("method", "path"), ADMIN_APIS)
    def test_admin_is_allowed(self, auth_env, method, path):
        login_as_admin(auth_env.client)
        kwargs: dict = {"follow_redirects": False}
        if method == "POST":
            kwargs["json"] = {"name": "鉴权用例", "method": "GET", "path": "/api/health",
                              "expected_status": 200, "enabled": True}
        resp = auth_env.client.request(method, path, **kwargs)
        # 通过鉴权后进入业务逻辑：200/201 正常，404 表示目标资源不存在（同样说明鉴权已放行）
        assert resp.status_code in (200, 201, 404)
        assert resp.status_code != 403


# ---------- 内容平台按角色可用 ----------


class TestContentPlatformAccess:
    @pytest.mark.parametrize("path", CONTENT_PAGES)
    def test_anonymous_can_browse_content(self, auth_env, path):
        resp = auth_env.client.get(path, follow_redirects=False)
        assert resp.status_code == 200

    @pytest.mark.parametrize("path", CONTENT_PAGES)
    def test_normal_user_can_browse_content(self, auth_env, path):
        login_as_user(auth_env.client)
        assert auth_env.client.get(path, follow_redirects=False).status_code == 200

    @pytest.mark.parametrize("path", CONTENT_PAGES)
    def test_admin_can_browse_content(self, auth_env, path):
        login_as_admin(auth_env.client)
        assert auth_env.client.get(path, follow_redirects=False).status_code == 200

    def test_anonymous_sees_login_entry_only(self, auth_env):
        html = auth_env.client.get("/").text
        assert 'href="/login"' in html
        assert 'href="/register"' in html
        assert "未登录" not in html  # 导航不展示「未登录」状态提示
        assert 'href="/testcases"' not in html  # 不暴露测试管理入口

    def test_normal_user_hides_test_management_entry(self, auth_env):
        login_as_user(auth_env.client)
        html = auth_env.client.get("/").text
        assert USER_USERNAME in html
        assert "普通用户" in html
        assert 'href="/testcases"' not in html
        assert 'href="/logout"' in html

    def test_admin_sees_test_management_entry(self, auth_env):
        login_as_admin(auth_env.client)
        html = auth_env.client.get("/").text
        assert ADMIN_USERNAME in html
        assert "管理员" in html
        assert 'href="/testcases"' in html

    def test_admin_layout_shows_account_and_back_entry(self, auth_env):
        login_as_admin(auth_env.client)
        html = auth_env.client.get("/testcases").text
        assert ADMIN_USERNAME in html
        assert "管理员" in html
        assert 'href="/logout"' in html
        assert "返回内容平台" in html


# ---------- 密码与会话底层行为 ----------


class TestSecurityPrimitives:
    def test_password_hash_hides_plaintext_and_verifies(self):
        stored = hash_password("s3cret-pass")
        assert "s3cret-pass" not in stored
        assert verify_password("s3cret-pass", stored) is True
        assert verify_password("other-pass", stored) is False

    def test_same_password_gets_different_salt(self):
        assert hash_password("same") != hash_password("same")

    def test_verify_password_tolerates_broken_hash(self):
        assert verify_password("any", "not-a-valid-hash") is False
        assert verify_password("any", "") is False

    def test_session_token_roundtrip(self):
        token = create_session_token(42, "secret-key")
        assert read_session_token(token, "secret-key", 3600) == 42

    def test_session_token_rejects_wrong_secret_and_tampering(self):
        token = create_session_token(42, "secret-key")
        assert read_session_token(token, "another-key", 3600) is None
        assert read_session_token(token + "x", "secret-key", 3600) is None
        assert read_session_token("garbage", "secret-key", 3600) is None

    def test_session_token_expires(self):
        token = create_session_token(42, "secret-key", now=1000)
        assert read_session_token(token, "secret-key", 60, now=1030) == 42
        assert read_session_token(token, "secret-key", 60, now=5000) is None

    def test_session_cookie_is_httponly(self, auth_env):
        resp = login(auth_env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        cookie_header = resp.headers["set-cookie"]
        assert "HttpOnly" in cookie_header
        assert "samesite=lax" in cookie_header.lower()


# ---------- 账号数据层 ----------


class TestUserCrud:
    def test_created_user_stores_hash_not_plaintext(self, tmp_path):
        engine = create_engine(
            f"sqlite:///{tmp_path / 'users.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            user = crud.create_user(db, username="tester", password="pw-123456", role=crud.ROLE_ADMIN)
            assert user.password_hash != "pw-123456"
            assert verify_password("pw-123456", user.password_hash)
            assert crud.get_user_by_username(db, "tester").id == user.id
        finally:
            db.close()

    def test_create_user_validates_role_and_duplicate(self, tmp_path):
        engine = create_engine(
            f"sqlite:///{tmp_path / 'users2.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            with pytest.raises(ValueError):
                crud.create_user(db, username="x", password="pw", role="root")
            crud.create_user(db, username="dup", password="pw", role=crud.ROLE_USER)
            with pytest.raises(ValueError):
                crud.create_user(db, username="dup", password="pw", role=crud.ROLE_USER)
        finally:
            db.close()

    def test_ensure_default_users_is_idempotent(self, tmp_path):
        engine = create_engine(
            f"sqlite:///{tmp_path / 'defaults.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            assert crud.ensure_default_users(db) == 2
            assert crud.ensure_default_users(db) == 0  # 已有账号时不再创建、不覆盖
            admin = crud.get_user_by_username(db, settings.default_admin_username)
            assert admin is not None and admin.role == crud.ROLE_ADMIN
            assert verify_password(settings.default_admin_password, admin.password_hash)
        finally:
            db.close()
