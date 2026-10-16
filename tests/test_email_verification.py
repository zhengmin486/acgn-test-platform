"""邮箱验证测试：令牌签发/消费、登录拦截、降级兜底、管理员放行与总开关。

覆盖：
- 注册后不签发会话，跳转待验证页；未验证账号登录被拒；
- 验证链接有效 -> 标记已验证；令牌不可重复使用、伪造与过期均失败；
- 重新发送会让旧链接失效，并受冷却限制；
- 邮箱必填、格式校验与唯一性；
- 未配置 SMTP 时的站内链接兜底（/verify/link、管理员生成链接）；
- 配置 SMTP 后真实调用 smtplib，且站内不再泄露验证链接；
- 管理员页面权限：游客 303、普通用户 403、管理员 200；手动放行后可登录；
- 总开关关闭后行为退回「注册即登录」；
- 历史账号（无邮箱、补列后 email_verified=0）不被锁死。

使用独立临时 SQLite（tmp_path），不触网、不写开发库。
"""

import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import EmailVerificationToken, User
from auth_helpers import ADMIN_USERNAME, ADMIN_PASSWORD, create_accounts, login

TOKEN_PATTERN = re.compile(r"/verify/email\?token=([A-Za-z0-9_\-]+)")


@pytest.fixture
def env(tmp_path):
    """独立临时库 + 预置管理员账号 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'verify.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(TestingSession, user=False)

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


def _register(client, username: str, email: str = "user@example.com",
              password: str = "pw-123456", **extra):
    data = {"username": username, "password": password, "confirm": password, "email": email}
    data.update(extra)
    return client.post("/register", data=data, follow_redirects=False)


def _link(client, next_url: str = "/") -> str:
    """通过「生成验证链接」取得验证地址（未配置 SMTP 时的兜底入口）。"""
    resp = client.post("/verify/link", data={"next": next_url}, follow_redirects=False)
    assert resp.status_code == 200, f"生成验证链接失败：{resp.status_code}"
    match = TOKEN_PATTERN.search(resp.text)
    assert match is not None, f"页面未包含验证链接：{resp.text}"
    return match.group(0)


def _user_rows(env) -> list[User]:
    db = env.session_factory()
    try:
        return list(db.scalars(select(User).order_by(User.id)))
    finally:
        db.close()


def _user_by_name(env, username: str) -> User:
    db = env.session_factory()
    try:
        return db.scalar(select(User).where(User.username == username))
    finally:
        db.close()


def _token_rows(env) -> list[EmailVerificationToken]:
    db = env.session_factory()
    try:
        return list(db.scalars(select(EmailVerificationToken).order_by(EmailVerificationToken.id)))
    finally:
        db.close()


# ---------- 注册后的待验证状态 ----------


class TestRegistrationPendingState:
    def test_register_redirects_to_pending_without_session(self, env):
        resp = _register(env.client, "pend", email="pend@example.com")
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/verify/pending")
        assert not env.client.cookies.get("acgn_session")

    def test_register_issues_one_token(self, env):
        _register(env.client, "tokencount", email="tokencount@example.com")
        rows = _token_rows(env)
        assert len(rows) == 1
        assert rows[0].consumed_at is None
        assert rows[0].expires_at > rows[0].created_at

    def test_pending_page_shows_email_and_link_when_smtp_missing(self, env):
        """未配置 SMTP：点「生成验证链接」后页面直接给出链接与邮箱。"""
        _register(env.client, "shown", email="shown@example.com")
        html = env.client.post("/verify/link", data={"next": "/"}).text
        assert "shown@example.com" in html
        assert "/verify/email?token=" in html
        assert "当前未配置邮件服务" in html

    def test_pending_page_without_context_offers_email_form(self, env):
        """没有待验证 Cookie 时，提示按邮箱重新发送。"""
        html = env.client.get("/verify/pending").text
        assert "没有待验证的会话信息" in html
        assert 'action="/verify/resend"' in html


# ---------- 登录拦截 ----------


class TestLoginBlockedUntilVerified:
    def test_unverified_user_cannot_login(self, env):
        _register(env.client, "blocked", email="blocked@example.com")
        resp = login(env.client, "blocked", "pw-123456")
        assert resp.status_code == 400
        assert "邮箱尚未验证" in resp.text
        assert not env.client.cookies.get("acgn_session")

    def test_login_rejection_does_not_resend_mail(self, env, monkeypatch):
        """登录失败不应自动发信，避免被拿去轰炸他人邮箱。"""
        sent: list[str] = []

        def fake_send(address, username, url):
            sent.append(address)
            return True, "ok"

        monkeypatch.setattr("app.auth.mailer.send_verification_email", fake_send)
        _register(env.client, "nospam", email="nospam@example.com")
        sent.clear()
        login(env.client, "nospam", "pw-123456")
        assert sent == []

    def test_user_can_login_after_verification(self, env):
        _register(env.client, "after", email="after@example.com")
        assert "邮箱验证成功" in env.client.get(_link(env.client)).text
        resp = login(env.client, "after", "pw-123456")
        assert resp.status_code == 303
        assert env.client.cookies.get("acgn_session")

    def test_verification_clears_pending_cookie(self, env):
        _register(env.client, "cleared", email="cleared@example.com")
        resp = env.client.get(_link(env.client), follow_redirects=False)
        # 验证成功后待验证 Cookie 被删除（Set-Cookie 里带上过期/空值）。
        assert "acgn_pending" in resp.headers.get("set-cookie", "")


# ---------- 令牌有效性 ----------


class TestTokenLifecycle:
    def test_valid_token_marks_email_verified(self, env):
        _register(env.client, "lifecycle", email="lifecycle@example.com")
        user = _user_by_name(env, "lifecycle")
        assert user.email_verified is False
        assert user.email_verified_at is None

        env.client.get(_link(env.client))
        user = _user_by_name(env, "lifecycle")
        assert user.email_verified is True
        assert user.email_verified_at is not None

    def test_token_cannot_be_reused(self, env):
        _register(env.client, "reuse", email="reuse@example.com")
        url = _link(env.client)
        assert env.client.get(url).status_code == 200
        resp = env.client.get(url, follow_redirects=False)
        assert resp.status_code == 400
        assert "邮箱验证失败" in resp.text

    def test_forged_token_is_rejected(self, env):
        resp = env.client.get("/verify/email?token=forged-token-value")
        assert resp.status_code == 400
        assert "邮箱验证失败" in resp.text

    def test_empty_token_is_rejected(self, env):
        assert env.client.get("/verify/email").status_code == 400

    def test_expired_token_is_rejected(self, env, monkeypatch):
        monkeypatch.setattr(settings, "email_verification_token_ttl_seconds", -1)
        _register(env.client, "expired", email="expired@example.com")
        url = _link(env.client)
        resp = env.client.get(url, follow_redirects=False)
        assert resp.status_code == 400
        user = _user_by_name(env, "expired")
        assert user.email_verified is False

    def test_token_stored_only_as_hash(self, env):
        _register(env.client, "hashed", email="hashed@example.com")
        url = _link(env.client)
        token = TOKEN_PATTERN.search(url).group(1)
        rows = _token_rows(env)
        # 注册时签发一枚，/verify/link 再签发一枚（前一枚随即作废）。
        assert len(rows) == 2
        assert rows[-1].token_hash == crud.hash_token(token)
        assert all(token not in row.token_hash for row in rows)


# ---------- 重新发送 ----------


class TestResend:
    def test_resend_invalidates_previous_link(self, env, monkeypatch):
        monkeypatch.setattr(settings, "email_resend_cooldown_seconds", 0)
        _register(env.client, "resend", email="resend@example.com")
        first = _link(env.client)

        resp = env.client.post("/verify/resend", data={"next": "/"}, follow_redirects=False)
        assert resp.status_code == 303

        second = _link(env.client)
        assert first != second
        assert env.client.get(first, follow_redirects=False).status_code == 400  # 旧链接失效
        assert env.client.get(second, follow_redirects=False).status_code == 200

    def test_resend_is_rate_limited(self, env):
        _register(env.client, "cooldown", email="cooldown@example.com")
        second = env.client.post("/verify/resend", data={"next": "/"}, follow_redirects=False)
        assert second.status_code == 400
        assert "发送过于频繁" in second.text

    def test_resend_by_email_without_cookie(self, env, monkeypatch):
        monkeypatch.setattr(settings, "email_resend_cooldown_seconds", 0)
        _register(env.client, "byemail", email="byemail@example.com")
        fresh = TestClient(app, follow_redirects=False)
        resp = fresh.post(
            "/verify/resend", data={"email": "byemail@example.com", "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "resent=1" in resp.headers["location"]

    def test_resend_unknown_email_is_rejected(self, env):
        resp = env.client.post(
            "/verify/resend", data={"email": "nobody@example.com", "next": "/"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "未找到待验证的账号" in resp.text


# ---------- 邮箱校验规则 ----------


class TestEmailValidation:
    @pytest.mark.parametrize(
        "email",
        ["", "   ", "no-at-sign", "a@b", "a@b.", "@example.com", "a b@example.com"],
    )
    def test_invalid_email_returns_400_and_echoes_reason(self, env, email):
        before = len(_user_rows(env))
        resp = _register(env.client, "mailcheck", email=email)
        assert resp.status_code == 400
        assert "邮箱" in resp.text
        assert len(_user_rows(env)) == before
        assert not env.client.cookies.get("acgn_session")

    def test_duplicate_email_is_rejected(self, env):
        _register(env.client, "first", email="dup@example.com")
        before = len(_user_rows(env))
        resp = _register(env.client, "second", email="dup@example.com")
        assert resp.status_code == 400
        assert "邮箱" in resp.text
        assert len(_user_rows(env)) == before

    def test_duplicate_email_is_case_insensitive(self, env):
        _register(env.client, "case1", email="Case@Example.com")
        resp = _register(env.client, "case2", email="CASE@example.COM")
        assert resp.status_code == 400
        assert "邮箱" in resp.text

    def test_email_is_normalized_before_saving(self, env):
        _register(env.client, "normal", email="  Normal@Example.COM  ")
        assert _user_by_name(env, "normal").email == "normal@example.com"


# ---------- 邮件发送：可插拔 ----------


class FakeSMTP:
    """替换 smtplib.SMTP 的桩：记录连接与投递内容，不触网。"""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent: list = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self, *_args, **_kwargs):
        self.started_tls = True
        return (220, b"ready")

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message, *_args, **_kwargs):
        self.sent.append(message)

    def quit(self):  # pragma: no cover - 兼容上下文退出
        self.closed = True


@pytest.fixture
def smtp_env(env, monkeypatch):
    """在临时库基础上打开 SMTP 配置，并把 smtplib.SMTP 换成桩。"""
    FakeSMTP.instances = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_use_tls", True)
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(settings, "smtp_username", "sender")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_from_address", "no-reply@example.com")
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    yield env


class TestSmtpDelivery:
    def test_registration_sends_real_mail_when_configured(self, smtp_env):
        resp = _register(smtp_env.client, "mailuser", email="mailuser@example.com")
        assert resp.status_code == 303
        assert "sent=1" in resp.headers["location"]

        assert len(FakeSMTP.instances) == 1
        server = FakeSMTP.instances[0]
        assert (server.host, server.port) == ("smtp.example.com", 587)
        assert server.started_tls is True
        assert server.logged_in == ("sender", "secret")
        assert server.closed is True

        assert len(server.sent) == 1
        message = server.sent[0]
        assert message["To"] == "mailuser@example.com"
        assert message["Subject"] == "验证你的 ACGN 平台账号邮箱"
        body = "".join(
            part.get_content()
            for part in message.walk()
            if part.get_content_type() == "text/plain"
        )
        assert "/verify/email?token=" in body

    def test_pending_page_hides_link_when_smtp_configured(self, smtp_env):
        _register(smtp_env.client, "hidden", email="hidden@example.com")
        html = smtp_env.client.get("/verify/pending").text
        assert "hidden@example.com" in html
        assert "/verify/email?token=" not in html

    def test_link_route_unavailable_when_smtp_configured(self, smtp_env):
        _register(smtp_env.client, "nolink", email="nolink@example.com")
        resp = smtp_env.client.post("/verify/link", data={"next": "/"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/verify/pending")

    def test_mail_failure_is_echoed_not_500(self, env, monkeypatch):
        """SMTP 已配置但连接失败：注册仍成功，待验证页如实提示发送失败。"""
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from_address", "no-reply@example.com")

        def boom(*_args, **_kwargs):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("smtplib.SMTP", boom)
        resp = _register(env.client, "mailfail", email="mailfail@example.com")
        assert resp.status_code == 303
        assert "sent=0" in resp.headers["location"]
        assert _user_by_name(env, "mailfail") is not None


# ---------- 管理员放行 ----------


class TestAdminVerification:
    def test_guest_is_redirected_to_login(self, env):
        resp = env.client.get("/admin/accounts", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_plain_user_is_denied(self, env):
        db = env.session_factory()
        try:
            crud.create_user(db, username="plain_acc", password="pw-123456")
        finally:
            db.close()
        login(env.client, "plain_acc", "pw-123456")
        assert env.client.get("/admin/accounts", follow_redirects=False).status_code == 403

    def test_admin_sees_pending_accounts(self, env):
        _register(env.client, "pending1", email="pending1@example.com")
        login(env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        html = env.client.get("/admin/accounts").text
        assert "pending1" in html
        assert "pending1@example.com" in html

    def test_admin_can_manually_verify_account(self, env):
        _register(env.client, "manual", email="manual@example.com")
        target = _user_by_name(env, "manual")
        login(env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = env.client.post(f"/admin/accounts/{target.id}/verify", follow_redirects=False)
        assert resp.status_code == 303
        assert "verified=manual" in resp.headers["location"]

        assert _user_by_name(env, "manual").email_verified is True
        env.client.cookies.clear()
        assert login(env.client, "manual", "pw-123456").status_code == 303

    def test_admin_manual_verify_unknown_account_is_echoed(self, env):
        login(env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = env.client.post("/admin/accounts/999999/verify", follow_redirects=False)
        assert resp.status_code == 303
        assert "error=" in resp.headers["location"]

    def test_admin_can_generate_link_without_smtp(self, env):
        _register(env.client, "adminlink", email="adminlink@example.com")
        target = _user_by_name(env, "adminlink")
        login(env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = env.client.post(f"/admin/accounts/{target.id}/link", follow_redirects=False)
        assert resp.status_code == 303
        query = parse_qs(urlparse(resp.headers["location"]).query)
        assert query["link_user"] == ["adminlink"]
        assert "/verify/email?token=" in query["link"][0]

    def test_admin_link_still_works_when_smtp_configured(self, smtp_env):
        """邮件服务故障时的兜底：即使配了 SMTP，管理员仍能取到转交链接。"""
        _register(smtp_env.client, "adminlink2", email="adminlink2@example.com")
        target = _user_by_name(smtp_env, "adminlink2")
        login(smtp_env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = smtp_env.client.post(f"/admin/accounts/{target.id}/link", follow_redirects=False)
        assert resp.status_code == 303
        query = parse_qs(urlparse(resp.headers["location"]).query)
        assert "/verify/email?token=" in query["link"][0]

    def test_admin_generated_link_can_verify_account(self, smtp_env):
        """管理员生成的链接，用户打开后即可完成验证并登录。"""
        _register(smtp_env.client, "adminlink3", email="adminlink3@example.com")
        target = _user_by_name(smtp_env, "adminlink3")
        login(smtp_env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = smtp_env.client.post(f"/admin/accounts/{target.id}/link", follow_redirects=False)
        query = parse_qs(urlparse(resp.headers["location"]).query)

        user_client = TestClient(app, follow_redirects=False)
        result = user_client.get(query["link"][0])
        assert "邮箱验证成功" in result.text
        assert login(user_client, "adminlink3", "pw-123456").status_code == 303


# ---------- 总开关 ----------


class TestFeatureSwitch:
    def test_registration_signs_in_when_disabled(self, env, monkeypatch):
        monkeypatch.setattr(settings, "email_verification_enabled", False)
        resp = _register(env.client, "noswitch", email="noswitch@example.com")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert env.client.cookies.get("acgn_session")
        assert _user_by_name(env, "noswitch").email_verified is True

    def test_verify_routes_redirect_home_when_disabled(self, env, monkeypatch):
        monkeypatch.setattr(settings, "email_verification_enabled", False)
        for path in ("/verify/pending", "/verify/resend", "/verify/email?token=x"):
            resp = env.client.get(path, follow_redirects=False)
            assert resp.status_code == 303, path
            assert resp.headers["location"] == "/"

    def test_email_not_required_when_disabled(self, env, monkeypatch):
        monkeypatch.setattr(settings, "email_verification_enabled", False)
        resp = env.client.post(
            "/register",
            data={"username": "nomail", "password": "pw-123456", "confirm": "pw-123456"},
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------- 历史账号兼容 ----------


class TestLegacySchemaMigration:
    def test_adds_missing_columns_and_backfills_existing_rows(self, tmp_path):
        """老库（users 表无邮箱列）启动补列：列 + 唯一索引补齐，老行视为已验证。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy_schema.db'}")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(50) NOT NULL,"
                    " password_hash VARCHAR(200) NOT NULL, role VARCHAR(20) NOT NULL,"
                    " created_at DATETIME NOT NULL)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO users (username, password_hash, role, created_at)"
                    " VALUES ('old_admin', 'hash', 'admin', '2026-01-01 00:00:00')"
                )
            )

        added = crud.ensure_user_email_columns(engine)
        assert set(added) == {
            "email",
            "email_verified",
            "email_verified_at",
            "password_changed_at",
        }

        # 个人空间上线后 init_db 还会补简介列（与邮箱列同一套幂等补列模式）
        assert crud.ensure_user_profile_columns(engine) == ["bio"]

        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("users")}
        assert {
            "email",
            "email_verified",
            "email_verified_at",
            "password_changed_at",
            "bio",
        } <= columns
        assert "uq_users_email" in {
            index["name"] for index in inspector.get_indexes("users")
        }

        db = sessionmaker(bind=engine)()
        try:
            legacy = db.scalar(select(User).where(User.username == "old_admin"))
            assert legacy.email is None
            assert legacy.email_verified is True  # 老账号不被锁死
        finally:
            db.close()

        # 幂等：重复执行不再新增列
        assert crud.ensure_user_email_columns(engine) == []
        assert crud.ensure_user_profile_columns(engine) == []
        engine.dispose()


class TestLegacyAccounts:
    def test_account_without_email_can_login(self, env):
        """启用验证后遗留的无邮箱账号（含管理员）仍可正常登录。"""
        db = env.session_factory()
        try:
            crud.create_user(db, username="legacy", password="legacy-pass")
            created = db.scalar(select(User).where(User.username == "legacy"))
            assert created.email is None
            assert created.email_verified is True  # 无邮箱 -> 无需验证
        finally:
            db.close()
        assert login(env.client, "legacy", "legacy-pass").status_code == 303

    def test_legacy_row_with_unverified_flag_still_logs_in(self, env):
        """模拟补列后未回填的历史行：email 为 NULL 时不拦截。"""
        db = env.session_factory()
        try:
            crud.create_user(db, username="legacy2", password="legacy-pass")
            row = db.scalar(select(User).where(User.username == "legacy2"))
            row.email_verified = False  # 模拟迁移中未回填的旧数据
            db.commit()
        finally:
            db.close()
        assert login(env.client, "legacy2", "legacy-pass").status_code == 303
