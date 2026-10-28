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
from auth_helpers import (
    ADMIN_USERNAME,
    create_accounts,
    login,
    login_as_admin,
    register as submit_registration,
)

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


def make_code(env, *, days=None, note="", used=False, revoked=False,
               role=crud.ROLE_USER) -> str:
    """在临时库里造一个指定状态的邀请码，返回明文码。

    ``role`` 为该邀请码登记的目标角色（默认普通用户，可指定 admin 用于提权测试）。
    """
    db = env.session_factory()
    try:
        expires = datetime.now() + timedelta(days=days) if days else None
        row = crud.create_invite_codes(
            db, count=1, expires_at=expires, note=note, role=role
        )[0]
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
    """按新流程注册（先申请邮箱验证码再提交）；invite_code 为 None 时不带该字段。"""
    if invite_code is not None:
        extra["invite_code"] = invite_code
    return submit_registration(
        client, username, password, email=email or f"{username}@example.com", **extra
    )


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

    def test_register_without_invite_code_is_allowed(self, env):
        """邀请码改为可选：不填也能注册成功（默认不赠送任何额外权限）。"""
        before = len(_users(env))
        resp = register(env.client, "nocode")
        assert resp.status_code == 303
        assert len(_users(env)) == before + 1

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
        # 邮箱已用验证码验证过：注册成功即登录。
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        created = next(u for u in _users(env) if u.username == "invited")
        row = next(c for c in _codes(env) if c.code == code)
        assert row.used_by == created.id
        assert row.used_at is not None

    def test_error_page_echoes_submitted_invite_code(self, env):
        resp = register(env.client, "echo", invite_code="BADCODE12")
        assert resp.status_code == 400
        assert 'value="BADCODE12"' in resp.text

    def test_duplicate_username_is_rejected_without_invite(self, env):
        """不填邀请码也能走完整注册流程，重名依旧被拒且不建号。"""
        before = len(_users(env))
        resp = register(env.client, ADMIN_USERNAME)
        assert resp.status_code == 400
        assert "用户名已存在" in resp.text
        assert len(_users(env)) == before

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
        """拼错 / 留空都按 invite 处理——邀请码输入框照旧展示（只是不再必填）。"""
        monkeypatch.setattr(settings, "registration_mode", "oppen")
        html = env.client.get("/register").text
        assert 'name="invite_code"' in html
        assert register(env.client, "typo").status_code == 303

    def test_registered_user_role_is_still_user(self, env):
        code = make_code(env)
        register(env.client, "rolecheck", invite_code=code, role="admin")
        created = next(u for u in _users(env) if u.username == "rolecheck")
        assert created.role == crud.ROLE_USER

    def test_register_with_user_invite_code_is_normal_user(self, env):
        """普通用户邀请码：即便表单里敢塞 role=admin，建出来的也只能是普通用户。"""
        code = make_code(env, role=crud.ROLE_USER)
        register(env.client, "userinv", invite_code=code, role="admin")
        created = next(u for u in _users(env) if u.username == "userinv")
        assert created.role == crud.ROLE_USER

    def test_register_with_admin_invite_code_becomes_admin(self, env):
        """管理员邀请码是唯一的提权入口：用它注册成功即获管理员权限。"""
        code = make_code(env, role=crud.ROLE_ADMIN, note="站长发放")
        resp = register(env.client, "newadmin", invite_code=code)
        assert resp.status_code == 303
        created = next(u for u in _users(env) if u.username == "newadmin")
        assert created.role == crud.ROLE_ADMIN
        # 管理员拥有测试管理平台访问权（注册后会话已是该账号）。
        assert env.client.get("/testcases", follow_redirects=False).status_code == 200

    def test_admin_invite_code_role_comes_from_code_not_form(self, env):
        """安全边界：角色只来自数据库里邀请码记录的 role，表单传什么都无效。"""
        code = make_code(env, role=crud.ROLE_ADMIN)
        # 故意在表单里塞 role=user，想把自己降成普通用户——角色仍由码决定。
        register(env.client, "codepad", invite_code=code, role="user")
        created = next(u for u in _users(env) if u.username == "codepad")
        assert created.role == crud.ROLE_ADMIN

    def test_register_without_code_is_normal_user(self, env):
        """普通用户注册不需要邀请码，且永远是普通用户。"""
        before = len(_users(env))
        resp = register(env.client, "nocode2")
        assert resp.status_code == 303
        assert len(_users(env)) == before + 1
        created = next(u for u in _users(env) if u.username == "nocode2")
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

    def test_generate_with_role_default_is_user(self, env):
        """不传 role（或传普通用户）生成的码登记身份为普通用户。"""
        login_as_admin(env.client)
        resp = env.client.post(
            "/admin/invites/generate", data={"count": "1", "days": "0", "role": "user"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "generated=1" in resp.headers["location"]
        assert _codes(env)[0].role == crud.ROLE_USER

    def test_plain_admin_cannot_generate_admin_code(self, env):
        """普通管理员只能发普通用户码：选管理员角色会被拦下，且不建码。"""
        login_as_admin(env.client)
        resp = env.client.post(
            "/admin/invites/generate", data={"count": "1", "days": "0", "role": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        from urllib.parse import unquote
        assert "仅站长可生成管理员邀请码" in unquote(resp.headers["location"])
        assert _codes(env) == []

    def test_owner_can_generate_admin_code(self, env):
        """站长可生成管理员邀请码：生成的码登记身份为管理员。"""
        db = env.session_factory()
        try:
            crud.create_user(db, username="auth_owner", password="owner-pass",
                             role=crud.ROLE_OWNER)
        finally:
            db.close()
        login(env.client, "auth_owner", "owner-pass")
        resp = env.client.post(
            "/admin/invites/generate", data={"count": "1", "days": "0", "role": "admin"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "generated=1" in resp.headers["location"]
        rows = _codes(env)
        assert len(rows) == 1
        assert rows[0].role == crud.ROLE_ADMIN

    def test_generate_rejects_unknown_role(self, env):
        login_as_admin(env.client)
        resp = env.client.post(
            "/admin/invites/generate", data={"count": "1", "days": "0", "role": "owner"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]
        assert _codes(env) == []

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

    def test_revoke_codes_batch(self, env):
        """批量作废：一次提交多个 id，全部作废并回显真实条数。"""
        login_as_admin(env.client)
        for _ in range(3):
            make_code(env)
        ids = [row.id for row in _codes(env)]
        resp = env.client.post(
            "/admin/invites/revoke", data={"invite_ids": [str(i) for i in ids]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "revoked=3" in resp.headers["location"]
        assert all(row.revoked_at is not None for row in _codes(env))

    def test_revoke_batch_skips_used_and_revoked(self, env):
        """已使用 / 已作废的码不重复计入：回显数量与真实变化一致。"""
        login_as_admin(env.client)
        make_code(env, used=True)
        make_code(env)
        rows = _codes(env)
        used = next(row for row in rows if row.used_at is not None)
        available = next(row for row in rows if row.used_at is None)
        resp = env.client.post(
            "/admin/invites/revoke",
            data={"invite_ids": [str(available.id), str(used.id)]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "revoked=1" in resp.headers["location"]
        after = {row.id: row for row in _codes(env)}
        assert after[available.id].revoked_at is not None
        assert after[used.id].revoked_at is None

    def test_revoke_batch_without_selection_is_noop(self, env):
        """未勾选：不报错，也不写任何作废时间。"""
        login_as_admin(env.client)
        make_code(env)
        resp = env.client.post("/admin/invites/revoke", data={},
                               follow_redirects=False)
        assert resp.status_code == 303
        assert "revoked" not in resp.headers["location"]
        assert all(row.revoked_at is None for row in _codes(env))

    def test_invites_page_supports_pick_and_confirm(self, env):
        """邀请码页：可作废的码带勾选框，单条与批量作废都要二次确认。"""
        make_code(env)
        login_as_admin(env.client)
        body = env.client.get("/admin/invites").text
        assert "data-row-check" in body
        assert 'form="bulk-revoke"' in body
        assert "data-bulk-submit" in body
        assert "data-confirm=" in body
        assert 'data-label="邀请码"' in body

    def test_used_code_appears_in_list(self, env):
        register(env.client, "listed", invite_code=make_code(env))
        # 注册成功会签发会话（顶掉之前的登录态），这里重新以管理员身份进入。
        login_as_admin(env.client)
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

    def test_role_option_creates_admin_code(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'cli4.db'}"
        assert create_invite.main(
            ["--count", "1", "--days", "0", "--role", "admin", "--database-url", db_url]
        ) == 0
        engine = create_engine(db_url)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            row = session.scalars(select(InviteCode)).one()
            assert row.role == "admin"
        finally:
            session.close()

    def test_role_option_defaults_to_user(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'cli5.db'}"
        assert create_invite.main(["--database-url", db_url]) == 0
        engine = create_engine(db_url)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        try:
            row = session.scalars(select(InviteCode)).one()
            assert row.role == "user"
        finally:
            session.close()
