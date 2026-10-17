"""邀请码注册测试：数据层、注册流程、管理页面与 CLI。

覆盖：
- crud 层：生成（字符集 / 唯一性 / 批量上限）、校验（可用 / 已用 / 过期 / 作废 /
  不存在）、单次消费（条件 UPDATE 兜并发）、作废规则；
- 注册流程：默认 invite 模式下无码 / 错码 / 已用 / 过期 / 作废一律 400 且不建号；
  有效码注册成功并原子消费；open 模式下完全不校验邀请码；
- 管理页 /admin/invites：游客 303、普通用户 403、管理员 200；生成 / 作废 PRG；
- CLI scripts/create_invite.py：生成与列出。

使用独立临时 SQLite（tmp_path），与开发库 local_dev.db 完全隔离。
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import InviteCode, User
from auth_helpers import ADMIN_USERNAME, ADMIN_PASSWORD, create_accounts, login, login_as_admin

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "create_invite.py"

# scripts/ 不是包，按文件路径加载被测 CLI 模块（与直接运行脚本等价）。
_spec = importlib.util.spec_from_file_location("create_invite_for_tests", SCRIPT_PATH)
create_invite = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = create_invite
_spec.loader.exec_module(create_invite)


@pytest.fixture
def env(tmp_path):
    """独立临时库 + 预置管理员账号 + get_db 覆盖（不含任何邀请码）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'invite.db'}",
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


def make_code(env, *, days=None, note="", used=False, revoked=False) -> str:
    """在临时库里造一个指定状态的邀请码，返回明文码。"""
    db = env.session_factory()
    try:
        expires = datetime.now() + timedelta(days=days) if days else None
        row = crud.create_invite_codes(db, count=1, expires_at=expires, note=note)[0]
        if used:
            row.used_by = 999
            row.used_at = datetime.now()
            db.commit()
        if revoked:
            row.revoked_at = datetime.now()
            db.commit()
        return row.code
    finally:
        db.close()


def register(client, username, password="pw-123456", email=None, invite_code=None, **extra):
    """提交注册表单；invite_code 为 None 时完全不带该字段。"""
    data = {
        "username": username,
        "password": password,
        "confirm": password,
        "email": email or f"{username}@example.com",
    }
    if invite_code is not None:
        data["invite_code"] = invite_code
    data.update(extra)
    return client.post("/register", data=data, follow_redirects=False)


def _users(env) -> list[User]:
    db = env.session_factory()
    try:
        return list(db.scalars(select(User).order_by(User.id)))
    finally:
        db.close()


def _codes(env) -> list[InviteCode]:
    db = env.session_factory()
    try:
        return list(db.scalars(select(InviteCode).order_by(InviteCode.id)))
    finally:
        db.close()


# ---------- crud 层 ----------


class TestInviteCodeCrud:
    def test_default_mode_is_invite(self):
        assert settings.registration_invite_only is True

    def test_generated_codes_are_well_formed_and_unique(self, env):
        db = env.session_factory()
        try:
            rows = crud.create_invite_codes(db, count=crud.INVITE_CODE_MAX_BATCH)
        finally:
            db.close()
        assert len(rows) == crud.INVITE_CODE_MAX_BATCH
        assert len({row.code for row in rows}) == len(rows)
        for row in rows:
            assert len(row.code) == crud.INVITE_CODE_LENGTH
            assert set(row.code) <= set(crud.INVITE_CODE_ALPHABET)

    def test_create_rejects_out_of_range_count(self, env):
        db = env.session_factory()
        try:
            with pytest.raises(ValueError):
                crud.create_invite_codes(db, count=0)
            with pytest.raises(ValueError):
                crud.create_invite_codes(db, count=crud.INVITE_CODE_MAX_BATCH + 1)
        finally:
            db.close()

    def test_check_available_code(self, env):
        code = make_code(env)
        db = env.session_factory()
        try:
            row, error = crud.check_invite_code(db, code)
            assert error == "" and row is not None
        finally:
            db.close()

    def test_check_missing_code(self, env):
        db = env.session_factory()
        try:
            row, error = crud.check_invite_code(db, "NOSUCHCODE")
            assert row is None and "不存在" in error
            row, error = crud.check_invite_code(db, "")
            assert row is None and "不存在" in error
        finally:
            db.close()

    def test_check_used_expired_revoked(self, env):
        used = make_code(env, used=True)
        expired = make_code(env, days=-1)
        revoked = make_code(env, revoked=True)
        db = env.session_factory()
        try:
            _, error = crud.check_invite_code(db, used)
            assert "已被使用" in error
            _, error = crud.check_invite_code(db, expired)
            assert "已过期" in error
            _, error = crud.check_invite_code(db, revoked)
            assert "作废" in error
        finally:
            db.close()

    def test_status_priority_revoked_over_used(self, env):
        code = make_code(env, used=True, revoked=True)
        db = env.session_factory()
        try:
            row = crud.get_invite_code(db, code)
            assert crud.invite_code_status(row) == crud.INVITE_STATUS_REVOKED
        finally:
            db.close()

    def test_consume_marks_user_and_is_single_use(self, env):
        code = make_code(env)
        db = env.session_factory()
        try:
            ok, error = crud.consume_invite_code(db, code, user_id=42)
            assert ok and error == ""
            row = crud.get_invite_code(db, code)
            assert row.used_by == 42 and row.used_at is not None
            ok, error = crud.consume_invite_code(db, code, user_id=43)
            assert not ok and "已被使用" in error
        finally:
            db.close()

    def test_consume_missing_code(self, env):
        db = env.session_factory()
        try:
            ok, error = crud.consume_invite_code(db, "NOSUCHCODE", user_id=1)
            assert not ok and "不存在" in error
        finally:
            db.close()

    def test_revoke_rules(self, env):
        fresh = make_code(env)
        used = make_code(env, used=True)
        db = env.session_factory()
        try:
            row = crud.get_invite_code(db, fresh)
            ok, error = crud.revoke_invite_code(db, row.id)
            assert ok and error == ""
            ok, error = crud.revoke_invite_code(db, row.id)
            assert not ok and "已作废" in error
            used_row = crud.get_invite_code(db, used)
            ok, error = crud.revoke_invite_code(db, used_row.id)
            assert not ok and "已被使用" in error
            ok, error = crud.revoke_invite_code(db, 999999)
            assert not ok and "不存在" in error
        finally:
            db.close()


# ---------- 注册流程 ----------


class TestRegisterWithInvite:
    def test_register_page_shows_invite_field(self, env):
        html = env.client.get("/register").text
        assert 'name="invite_code"' in html
        assert "邀请码" in html

    def test_register_without_invite_code_is_rejected(self, env):
        before = len(_users(env))
        resp = register(env.client, "nocode")
        assert resp.status_code == 400
        assert "邀请码" in resp.text
        assert len(_users(env)) == before

    def test_register_with_unknown_invite_code_is_rejected(self, env):
        before = len(_users(env))
        resp = register(env.client, "wrongcode", invite_code="NOSUCHCODE")
        assert resp.status_code == 400
        assert "邀请码不存在" in resp.text
        assert len(_users(env)) == before

    def test_register_with_used_invite_code_is_rejected(self, env):
        code = make_code(env)
        assert register(env.client, "first", invite_code=code).status_code == 303
        before = len(_users(env))
        resp = register(env.client, "second", invite_code=code)
        assert resp.status_code == 400
        assert "已被使用" in resp.text
        assert len(_users(env)) == before

    def test_register_with_expired_invite_code_is_rejected(self, env):
        resp = register(env.client, "late", invite_code=make_code(env, days=-1))
        assert resp.status_code == 400
        assert "已过期" in resp.text

    def test_register_with_revoked_invite_code_is_rejected(self, env):
        resp = register(env.client, "revoked", invite_code=make_code(env, revoked=True))
        assert resp.status_code == 400
        assert "作废" in resp.text

    def test_register_with_valid_invite_code_succeeds_and_consumes(self, env):
        code = make_code(env, note="给朋友")
        resp = register(env.client, "invited", invite_code=code)
        # 邮箱验证默认开启：注册成功但先跳待验证页。
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/verify/pending")

        created = next(u for u in _users(env) if u.username == "invited")
        row = next(c for c in _codes(env) if c.code == code)
        assert row.used_by == created.id
        assert row.used_at is not None

    def test_error_page_echoes_submitted_invite_code(self, env):
        resp = register(env.client, "echo", invite_code="BADCODE12")
        assert resp.status_code == 400
        assert 'value="BADCODE12"' in resp.text

    def test_invite_required_before_duplicate_username_check(self, env):
        """缺少邀请码时先回显邀请码错误，不提前建号（重复名校验在消费之后）。"""
        resp = register(env.client, ADMIN_USERNAME)
        assert resp.status_code == 400
        assert "邀请码" in resp.text

    def test_open_mode_ignores_invite_code(self, env, monkeypatch):
        monkeypatch.setattr(settings, "registration_mode", "open")
        html = env.client.get("/register").text
        assert 'name="invite_code"' not in html
        resp = register(env.client, "openmode")
        assert resp.status_code == 303
        assert any(u.username == "openmode" for u in _users(env))

    def test_open_mode_still_accepts_bogus_code_field(self, env, monkeypatch):
        """open 模式完全不看邀请码：乱填也不影响注册。"""
        monkeypatch.setattr(settings, "registration_mode", "open")
        resp = register(env.client, "openmode2", invite_code=" GARBAGE ")
        assert resp.status_code == 303

    def test_invalid_registration_mode_falls_back_to_invite(self, env, monkeypatch):
        """拼错 / 留空都按 invite 处理——公开注册是敏感开关，默认落收紧一侧。"""
        monkeypatch.setattr(settings, "registration_mode", "oppen")
        resp = register(env.client, "typo")
        assert resp.status_code == 400
        assert "邀请码" in resp.text

    def test_registered_user_role_is_still_user(self, env):
        code = make_code(env)
        register(env.client, "rolecheck", invite_code=code, role="admin")
        created = next(u for u in _users(env) if u.username == "rolecheck")
        assert created.role == crud.ROLE_USER


# ---------- 管理页 ----------


class TestInvitesAdminPage:
    def test_guest_is_redirected_to_login(self, env):
        resp = env.client.get("/admin/invites", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_plain_user_is_forbidden(self, env):
        login(env.client, "auth_user", "user-test-pass")
        assert env.client.get("/admin/invites", follow_redirects=False).status_code == 403

    def test_admin_sees_page_with_nav_entry(self, env):
        login_as_admin(env.client)
        html = env.client.get("/admin/invites").text
        assert "注册邀请码" in html
        assert 'action="/admin/invites/generate"' in html

    def test_generate_creates_codes(self, env):
        login_as_admin(env.client)
        resp = env.client.post(
            "/admin/invites/generate",
            data={"count": "3", "days": "7", "note": "批量测试"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "generated=3" in resp.headers["location"]

        rows = _codes(env)
        assert len(rows) == 3
        assert all(row.note == "批量测试" for row in rows)
        assert all(row.expires_at is not None for row in rows)
        assert all(row.created_by is not None for row in rows)

    def test_generate_rejects_bad_count(self, env):
        login_as_admin(env.client)
        for bad in ("0", "abc", "99"):
            resp = env.client.post(
                "/admin/invites/generate", data={"count": bad, "days": "0"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=" in resp.headers["location"]
        assert _codes(env) == []

    def test_generate_rejects_bad_days(self, env):
        login_as_admin(env.client)
        resp = env.client.post(
            "/admin/invites/generate", data={"count": "1", "days": "永远"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_generate_without_expiry(self, env):
        login_as_admin(env.client)
        resp = env.client.post(
            "/admin/invites/generate", data={"count": "1", "days": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = _codes(env)[0]
        assert row.expires_at is None

    def test_revoke_available_code(self, env):
        login_as_admin(env.client)
        code = make_code(env)
        row = next(c for c in _codes(env) if c.code == code)
        resp = env.client.post(f"/admin/invites/{row.id}/revoke", follow_redirects=False)
        assert resp.status_code == 303
        assert "revoked=1" in resp.headers["location"]
        revoked = next(c for c in _codes(env) if c.code == code)
        assert revoked.revoked_at is not None

    def test_revoke_used_code_shows_error(self, env):
        login_as_admin(env.client)
        code = make_code(env, used=True)
        row = next(c for c in _codes(env) if c.code == code)
        resp = env.client.post(f"/admin/invites/{row.id}/revoke", follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_used_code_appears_in_list(self, env):
        login_as_admin(env.client)
        register(env.client, "listed", invite_code=make_code(env))
        html = env.client.get("/admin/invites").text
        assert "已使用" in html


# ---------- CLI ----------


class TestCreateInviteCli:
    def test_generate_and_list(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'cli.db'}"
        code = create_invite.main(["--count", "2", "--days", "7", "--database-url", db_url])
        assert code == 0

        engine = create_engine(db_url)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            rows = list(session.scalars(select(InviteCode)))
            assert len(rows) == 2
            assert all(row.expires_at is not None for row in rows)
        finally:
            session.close()

        assert create_invite.main(["--list", "--database-url", db_url]) == 0

    def test_rejects_bad_count(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'cli2.db'}"
        assert create_invite.main(["--count", "0", "--database-url", db_url]) == 1

    def test_days_zero_means_never_expires(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'cli3.db'}"
        assert create_invite.main(["--days", "0", "--database-url", db_url]) == 0
        engine = create_engine(db_url)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            row = session.scalars(select(InviteCode)).one()
            assert row.expires_at is None
        finally:
            session.close()
