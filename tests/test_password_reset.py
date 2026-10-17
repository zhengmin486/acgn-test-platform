"""找回密码测试：申请链接、一次性令牌、改密生效、旧会话失效与兜底降级。

覆盖：
- 申请页渲染，登录页「忘记密码」入口，已登录访问申请页回跳；
- 提交已注册邮箱 -> 签发一个重置令牌（库中只存哈希），跳转 /reset/pending；
- 提交未注册邮箱 -> 提示与成功完全一致，不签发令牌，不泄露账号是否存在；
- 无邮箱的历史账号（脚本创建的 admin）同样走统一提示——没有投递目标；
- 令牌一次性：改密成功后同一链接不可再用；过期与伪造令牌均被拒；
- 改密后旧密码失效、新密码可登录，且此前签发的会话全部失效；
- 未验证邮箱的账号通过重置链接改密后，邮箱顺带被标记为已验证；
- 未配置 SMTP 时可在站内生成重置链接；配置 SMTP 后不再泄露链接；
- 密码规则（过短 / 不一致）回显 400 且不改动数据。

使用独立临时 SQLite（tmp_path），不触网、不写开发库。
"""

import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import PasswordResetToken, User
from auth_helpers import ADMIN_USERNAME, ADMIN_PASSWORD, create_accounts, login

RESET_TOKEN_PATTERN = re.compile(r"/reset-password\?token=([A-Za-z0-9_\-]+)")

FORGOT_PATH = "/forgot-password"
RESET_PATH = "/reset-password"

MAIL = "resetuser@example.com"


@pytest.fixture(autouse=True)
def _open_registration(monkeypatch):
    """本文件聚焦找回密码流程：显式切到公开注册模式。

    默认的 invite 模式下每个注册用例都要配邀请码，会淹没重置本身的断言；
    邀请码与注册流程的交互在 tests/test_invites.py 专门覆盖。
    """
    monkeypatch.setattr(settings, "registration_mode", "open")


@pytest.fixture
def env(tmp_path):
    """独立临时库 + 预置管理员账号 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reset.db'}",
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


def _register(client, username: str, email: str = MAIL,
              password: str = "pw-123456", **extra):
    data = {"username": username, "password": password, "confirm": password, "email": email}
    data.update(extra)
    return client.post("/register", data=data, follow_redirects=False)


def _request_reset(client, email: str, next_url: str = "/"):
    return client.post(
        FORGOT_PATH,
        data={"email": email, "next": next_url},
        follow_redirects=False,
    )


def _reset_link(client, next_url: str = "/") -> str:
    """通过站内兜底入口取得重置地址（未配置 SMTP 时可用）。"""
    resp = client.post("/reset/link", data={"next": next_url}, follow_redirects=False)
    assert resp.status_code == 200, f"生成重置链接失败：{resp.status_code}"
    match = RESET_TOKEN_PATTERN.search(resp.text)
    assert match is not None, f"页面未包含重置链接：{resp.text}"
    return match.group(0)


def _token_rows(env) -> list[PasswordResetToken]:
    db = env.session_factory()
    try:
        return list(db.scalars(select(PasswordResetToken).order_by(PasswordResetToken.id)))
    finally:
        db.close()


def _user_by_name(env, username: str) -> User:
    db = env.session_factory()
    try:
        return db.scalar(select(User).where(User.username == username))
    finally:
        db.close()


def _verify_user(env, username: str) -> None:
    """把账号标记为已验证，便于测试登录相关行为。"""
    db = env.session_factory()
    try:
        user = db.scalar(select(User).where(User.username == username))
        user.email_verified = True
        db.commit()
    finally:
        db.close()


def _expire_tokens(env) -> None:
    db = env.session_factory()
    try:
        for row in db.scalars(select(PasswordResetToken)):
            row.expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()


# ---------- 申请页 ----------


class TestForgotPasswordPage:
    def test_page_renders(self, env):
        resp = env.client.get(FORGOT_PATH)
        assert resp.status_code == 200
        assert "找回密码" in resp.text

    def test_login_page_has_forgot_entry(self, env):
        html = env.client.get("/login").text
        assert "/forgot-password" in html

    def test_authenticated_user_is_redirected(self, env):
        login(env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        resp = env.client.get(f"{FORGOT_PATH}?next=/articles", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/articles"


# ---------- 申请重置链接 ----------


class TestRequestReset:
    def test_known_email_issues_one_token(self, env):
        _register(env.client, "resetok", email=MAIL)
        resp = _request_reset(env.client, MAIL)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/reset/pending")

        rows = _token_rows(env)
        assert len(rows) == 1
        assert rows[0].consumed_at is None
        assert rows[0].expires_at > rows[0].created_at

    def test_token_stored_only_as_hash(self, env):
        _register(env.client, "resethash", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        rows = _token_rows(env)
        assert rows
        assert token not in rows[-1].token_hash
        assert rows[-1].token_hash == crud.hash_token(token)

    def test_unknown_email_shows_same_message(self, env):
        """未注册邮箱与成功路径提示一致，且不签发任何令牌。"""
        _register(env.client, "resetknown", email=MAIL)
        resp = _request_reset(env.client, "nobody@example.com")
        assert resp.status_code == 200
        assert "如果该邮箱已在平台注册" in resp.text
        assert "nobody@example.com" not in resp.text

    def test_unknown_email_issues_no_token(self, env):
        _register(env.client, "resetonly", email=MAIL)
        before = len(_token_rows(env))
        _request_reset(env.client, "ghost@example.com")
        assert len(_token_rows(env)) == before

    def test_email_is_required(self, env):
        resp = _request_reset(env.client, "")
        assert resp.status_code == 400
        assert "邮箱不能为空" in resp.text

    def test_email_format_is_validated(self, env):
        resp = _request_reset(env.client, "not-an-email")
        assert resp.status_code == 400
        assert "邮箱格式不正确" in resp.text

    def test_next_url_is_kept(self, env):
        _register(env.client, "resetnext", email=MAIL)
        resp = _request_reset(env.client, MAIL, next_url="/articles")
        query = parse_qs(urlparse(resp.headers["location"]).query)
        assert query["next"] == ["/articles"]

    def test_account_without_email_is_not_processed(self, env):
        """无邮箱的历史账号（脚本创建的 admin）没有投递目标，不签发令牌。"""
        resp = _request_reset(env.client, "legacy@example.com")
        assert resp.status_code == 200
        assert "如果该邮箱已在平台注册" in resp.text
        assert _token_rows(env) == []


# ---------- 改密 ----------


class TestResetPassword:
    def test_valid_token_renders_form(self, env):
        _register(env.client, "formuser", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        resp = env.client.get(url, follow_redirects=False)
        assert resp.status_code == 200
        assert "设置新密码" in resp.text

    def test_invalid_token_is_rejected(self, env):
        resp = env.client.get(f"{RESET_PATH}?token=forged-token", follow_redirects=False)
        assert resp.status_code == 400
        assert "重置链接无效或已过期" in resp.text

    def test_resets_password_and_allows_login(self, env):
        _register(env.client, "pwchange", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)

        resp = env.client.post(
            RESET_PATH,
            data={"token": RESET_TOKEN_PATTERN.search(url).group(1),
                  "password": "brand-new-pass", "confirm": "brand-new-pass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?reset=1")

        env.client.cookies.clear()
        resp = login(env.client, "pwchange", "brand-new-pass")
        assert resp.status_code == 303
        assert env.client.cookies.get("acgn_session")

    def test_old_password_stops_working(self, env):
        _register(env.client, "pwold", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        env.client.post(
            RESET_PATH,
            data={"token": RESET_TOKEN_PATTERN.search(url).group(1),
                  "password": "another-new-pass", "confirm": "another-new-pass"},
            follow_redirects=False,
        )
        env.client.cookies.clear()
        resp = login(env.client, "pwold", "pw-123456")
        assert resp.status_code == 400

    def test_token_cannot_be_reused(self, env):
        _register(env.client, "pwreuse", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        payload = {"token": token, "password": "first-new-pass", "confirm": "first-new-pass"}
        assert env.client.post(RESET_PATH, data=payload, follow_redirects=False).status_code == 303

        again = {"token": token, "password": "second-new-pass", "confirm": "second-new-pass"}
        resp = env.client.post(RESET_PATH, data=again, follow_redirects=False)
        assert resp.status_code == 400
        assert "重置链接无效或已过期" in resp.text

    def test_expired_token_is_rejected(self, env):
        """先取链接再让它过期：重发会签发新令牌，顺序反了就测不到过期。"""
        _register(env.client, "pwexpire", email=MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        _expire_tokens(env)
        resp = env.client.post(
            RESET_PATH,
            data={"token": token, "password": "expired-pass", "confirm": "expired-pass"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "重置链接无效或已过期" in resp.text

    def test_old_session_is_invalidated_after_reset(self, env):
        """改密后此前签发的会话必须失效，否则盗号者不会被踢下线。"""
        _register(env.client, "pwkick", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        _verify_user(env, "pwkick")

        assert login(env.client, "pwkick", "pw-123456").status_code == 303
        assert "pwkick" in env.client.get("/").text

        # 会话签发时间（iat）与改密时间都只精确到秒：等到下一秒，确保旧会话
        # 严格早于改密时刻，否则判定会因同秒而失效。
        time.sleep(1.1)

        resp = env.client.post(
            RESET_PATH,
            data={"token": token, "password": "kick-old-pass", "confirm": "kick-old-pass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "pwkick" not in env.client.get("/").text

    def test_resetting_also_verifies_email(self, env):
        """能收到重置邮件即证明邮箱可控，顺带完成邮箱验证。"""
        _register(env.client, "pwverify", email=MAIL)
        assert _user_by_name(env, "pwverify").email_verified is False

        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        env.client.post(
            RESET_PATH,
            data={"token": token, "password": "verify-new-pass", "confirm": "verify-new-pass"},
            follow_redirects=False,
        )
        assert _user_by_name(env, "pwverify").email_verified is True

    @pytest.mark.parametrize(
        "password, confirm, expected",
        [
            ("short", "short", "密码长度需为"),
            ("good-pass-1", "different-1", "两次输入的密码不一致"),
        ],
    )
    def test_password_rules_are_enforced(self, env, password, confirm, expected):
        _register(env.client, "pwrule", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        before = _user_by_name(env, "pwrule").password_hash

        resp = env.client.post(
            RESET_PATH,
            data={"token": token, "password": password, "confirm": confirm},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert expected in resp.text
        assert _user_by_name(env, "pwrule").password_hash == before

    def test_reset_does_not_sign_in_directly(self, env):
        """改密成功不签发会话，必须重新走登录。"""
        _register(env.client, "pwnosession", email=MAIL)
        _request_reset(env.client, MAIL)
        url = _reset_link(env.client)
        token = RESET_TOKEN_PATTERN.search(url).group(1)
        resp = env.client.post(
            RESET_PATH,
            data={"token": token, "password": "no-session-pass", "confirm": "no-session-pass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert not env.client.cookies.get("acgn_session")


# ---------- 无邮件服务时的站内兜底 ----------


class TestResetLinkFallback:
    def test_link_is_generated_without_smtp(self, env):
        _register(env.client, "fallback", email=MAIL)
        url = _reset_link(env.client, next_url="/articles")
        assert "/reset-password?token=" in url

    def test_generated_link_works(self, env):
        _register(env.client, "fallbackwork", email=MAIL)
        url = _reset_link(env.client)
        resp = env.client.get(url, follow_redirects=False)
        assert resp.status_code == 200
        assert "设置新密码" in resp.text

    def test_link_form_unavailable_when_smtp_configured(self, env, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from_address", "no-reply@example.com")
        _register(env.client, "fallbacksmtp", email=MAIL)
        resp = env.client.post("/reset/link", data={"next": "/"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/reset/pending")

    def test_pending_page_hides_link_when_smtp_configured(self, env, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_from_address", "no-reply@example.com")
        _register(env.client, "fallbackhide", email=MAIL)
        _request_reset(env.client, MAIL)
        html = env.client.get("/reset/pending").text
        assert "/reset-password?token=" not in html


# ---------- 历史账号不受影响 ----------


class TestLegacyAccounts:
    def test_existing_admin_can_still_login(self, env):
        """补列后无邮箱的老账号仍可登录，不被验证流程挡住。"""
        resp = login(env.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        assert resp.status_code == 303
        assert env.client.cookies.get("acgn_session")
        assert env.client.get("/manage").status_code == 200
