"""三级角色：user（普通用户）< admin（管理员）< owner（站长）。

覆盖：
- 角色等级与展示标签（crud.has_role / ROLE_LABELS）；
- 站长拥有管理员全部能力（管理后台 + 测试管理 API），普通用户 403；
- 站长专属的「角色管理」页与接口：管理员 / 普通用户 403，游客跳登录；
- 站长可调整他人角色，改完即时生效（被提升者立刻能进管理页）；
- 自我保护：不能改自己的角色；保留至少一个站长；非法角色被拒；
- 个人空间徽标按角色显示（站长显示「站长」）。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base, get_db
from app.main import app
from auth_helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    USER_PASSWORD,
    USER_USERNAME,
    create_accounts,
    login_as_admin,
    login_as_user,
)

OWNER_USERNAME = "auth_owner"
OWNER_PASSWORD = "owner-test-pass"


@pytest.fixture
def role_env(tmp_path):
    """独立临时库 + 种子数据 + 普通用户 / 管理员 / 站长三个账号。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'roles.db'}",
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

    db = TestingSession()
    try:
        crud.create_user(
            db,
            username=OWNER_USERNAME,
            password=OWNER_PASSWORD,
            role=crud.ROLE_OWNER,
        )
    finally:
        db.close()

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


def _uid(session_factory, username: str) -> int:
    db = session_factory()
    try:
        return crud.get_user_by_username(db, username).id
    finally:
        db.close()


def _login(client, username: str, password: str):
    resp = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, f"登录失败：{username}"
    return resp


class TestRoleHierarchy:
    def test_role_labels(self):
        assert crud.ROLE_LABELS[crud.ROLE_OWNER] == "站长"
        assert crud.ROLE_LABELS[crud.ROLE_ADMIN] == "管理员"
        assert crud.ROLE_LABELS[crud.ROLE_USER] == "普通用户"

    def test_role_rank_comparison(self):
        assert crud.has_role("owner", "admin") is True
        assert crud.has_role("admin", "admin") is True
        assert crud.has_role("user", "admin") is False
        assert crud.has_role("admin", "owner") is False
        assert crud.has_role("owner", "owner") is True


class TestStaffAccess:
    def test_owner_can_open_admin_pages(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        assert role_env.client.get("/testcases").status_code == 200
        assert role_env.client.get("/manage").status_code == 200
        assert role_env.client.get("/manage/submissions").status_code == 200

    def test_admin_can_open_admin_pages(self, role_env):
        login_as_admin(role_env.client)
        assert role_env.client.get("/testcases").status_code == 200

    def test_user_denied_admin_pages(self, role_env):
        login_as_user(role_env.client)
        assert role_env.client.get("/testcases").status_code == 403


class TestOwnerOnlyPage:
    def test_guest_redirected_to_login(self, role_env):
        assert role_env.client.get("/admin/roles").status_code == 303

    def test_user_cannot_open_roles_page(self, role_env):
        login_as_user(role_env.client)
        assert role_env.client.get("/admin/roles").status_code == 403

    def test_admin_cannot_open_roles_page(self, role_env):
        login_as_admin(role_env.client)
        assert role_env.client.get("/admin/roles").status_code == 403

    def test_owner_can_open_roles_page(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        page = role_env.client.get("/admin/roles")
        assert page.status_code == 200
        assert "角色管理" in page.text
        assert "站长" in page.text


class TestChangeRole:
    def test_owner_promotes_user_to_admin(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        uid = _uid(role_env.session_factory, USER_USERNAME)
        resp = role_env.client.post(
            f"/admin/roles/{uid}", data={"role": "admin"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "changed=" in resp.headers["location"]

        db = role_env.session_factory()
        try:
            assert crud.get_user(db, uid).role == crud.ROLE_ADMIN
        finally:
            db.close()

        # 提升后立刻能进管理页
        login_as_user(role_env.client)
        assert role_env.client.get("/testcases").status_code == 200

    def test_owner_can_promote_and_demote_another_owner(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        uid = _uid(role_env.session_factory, USER_USERNAME)

        role_env.client.post(f"/admin/roles/{uid}", data={"role": "owner"})
        db = role_env.session_factory()
        try:
            assert crud.get_user(db, uid).role == crud.ROLE_OWNER
            assert crud.count_owners(db) == 2
        finally:
            db.close()

        # 还剩一个站长，允许降级
        role_env.client.post(f"/admin/roles/{uid}", data={"role": "admin"})
        db = role_env.session_factory()
        try:
            assert crud.get_user(db, uid).role == crud.ROLE_ADMIN
        finally:
            db.close()

    def test_owner_cannot_change_own_role(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        oid = _uid(role_env.session_factory, OWNER_USERNAME)
        resp = role_env.client.post(
            f"/admin/roles/{oid}", data={"role": "admin"}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

        db = role_env.session_factory()
        try:
            assert crud.get_user(db, oid).role == crud.ROLE_OWNER
        finally:
            db.close()

    def test_invalid_role_rejected(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        uid = _uid(role_env.session_factory, USER_USERNAME)
        resp = role_env.client.post(
            f"/admin/roles/{uid}", data={"role": "superuser"}, follow_redirects=False
        )
        assert "error=" in resp.headers["location"]

        db = role_env.session_factory()
        try:
            assert crud.get_user(db, uid).role == crud.ROLE_USER
        finally:
            db.close()

    def test_admin_cannot_change_role(self, role_env):
        login_as_admin(role_env.client)
        uid = _uid(role_env.session_factory, USER_USERNAME)
        resp = role_env.client.post(
            f"/admin/roles/{uid}", data={"role": "admin"}, follow_redirects=False
        )
        assert resp.status_code == 403


class TestRoleDisplay:
    def test_owner_badge_shown_in_space(self, role_env):
        _login(role_env.client, OWNER_USERNAME, OWNER_PASSWORD)
        page = role_env.client.get("/me")
        assert "站长" in page.text

    def test_user_badge_shown_in_space(self, role_env):
        login_as_user(role_env.client)
        page = role_env.client.get("/me")
        assert "普通用户" in page.text
