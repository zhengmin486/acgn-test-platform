"""注册功能测试：邮箱验证码流程、邀请码可选、校验规则与「只能注册普通用户」。

覆盖：
- 注册页表单渲染，登录页与内容平台导航栏的注册入口；
- 新流程：填邮箱 -> 申请验证码 -> 填验证码 -> 设密码 -> 建号即登录；
- 无邀请码可直接注册；填了邀请码则必须是有效码，并被消费；
- 验证码安全：错误码 / 输错超限 / 过期 / 重复使用 / 换邮箱 / 重复邮箱 / 未发码；
- 发码冷却与邮箱、IP 维度的频率限制；
- 安全：表单里传入 role=admin 一律忽略，注册出的账号访问管理页面仍是 403；
- next 参数生效与开放重定向防护；
- 并发下的唯一索引冲突（IntegrityError）也能回显而不是 500。

注册前完成邮箱验证，因此新账号建好即「邮箱已验证」并可登录；
链接验证那条存量路径（找回密码 / 管理员放行）见 tests/test_email_verification.py。

使用独立临时 SQLite（tmp_path），与开发库 local_dev.db 完全隔离。
"""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import auth, crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import EmailVerificationCode, User
from auth_helpers import (
    ADMIN_USERNAME,
    create_accounts,
    extract_code,
    login,
    register as submit_registration,
    request_register_code,
)

REGISTER_PATH = "/register"
DEFAULT_EMAIL = "register@example.com"


def code_for(client, email: str = DEFAULT_EMAIL, **extra) -> str:
    """申请一枚验证码并取出明文（测试环境未配 SMTP，页面上会给出）。"""
    return extract_code(request_register_code(client, email, **extra))


def reg_form(email: str = DEFAULT_EMAIL, code: str = "000000", **overrides):
    """拼一份注册表单；默认用户名 someone / 密码 pw-123456。"""
    data = {
        "username": "someone",
        "password": "pw-123456",
        "confirm": "pw-123456",
        "email": email,
        "code": code,
    }
    data.update(overrides)
    return data


def register(env, username: str, password: str = "pw-123456",
             email: str = DEFAULT_EMAIL, **extra):
    """按新流程注册（不跟随重定向）；extra 可带 invite_code / next 等字段。"""
    return submit_registration(env.client, username, password, email=email, **extra)


def post_register(client, **form):
    return client.post(REGISTER_PATH, data=form, follow_redirects=False)


@pytest.fixture
def reg_env(tmp_path, monkeypatch):
    """独立临时库 + 预置账号（用于重名校验）+ 邀请码池 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'register.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(TestingSession)
    # 邀请码改为可选：预置一批码，只有显式传 invite_code 的用例才会消耗。
    invites = [
        row.code
        for row in crud.create_invite_codes(
            TestingSession(), count=crud.INVITE_CODE_MAX_BATCH
        )
    ]
    # 默认关掉发码冷却，避免同一用例里多次发码互相干扰；需要测冷却的用例自行打开。
    monkeypatch.setattr(settings, "email_resend_cooldown_seconds", 0)

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
            invites=invites,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _users(env) -> list[User]:
    db = env.session_factory()
    try:
        return list(db.scalars(select(User).order_by(User.id)))
    finally:
        db.close()


def _find(env, username: str) -> User | None:
    return next((u for u in _users(env) if u.username == username), None)


# ---------- 页面渲染与入口 ----------


class TestRegisterPage:
    def test_register_page_renders_form(self, reg_env):
        resp = reg_env.client.get(REGISTER_PATH)
        assert resp.status_code == 200
        assert 'action="/register"' in resp.text
        assert 'name="username"' in resp.text
        assert 'name="password"' in resp.text
        assert 'name="confirm"' in resp.text
        assert 'name="email"' in resp.text
        assert 'name="code"' in resp.text

    def test_register_page_offers_send_code_button(self, reg_env):
        """发送验证码按钮提交到 /register/code。"""
        assert 'formaction="/register/code"' in reg_env.client.get(REGISTER_PATH).text

    def test_register_page_states_user_only(self, reg_env):
        """页面明确说明注册得到的是普通用户。"""
        assert "普通用户" in reg_env.client.get(REGISTER_PATH).text

    def test_register_page_has_login_link(self, reg_env):
        assert 'href="/login"' in reg_env.client.get(REGISTER_PATH).text

    def test_login_page_has_register_entry(self, reg_env):
        """登录页提供注册入口（既有文案不变，仅新增链接）。"""
        html = reg_env.client.get("/login").text
        assert 'href="/register"' in html
        assert "还没有账号" in html

    def test_content_nav_shows_register_entry_when_anonymous(self, reg_env):
        html = reg_env.client.get("/").text
        assert 'href="/register"' in html
        assert 'href="/login"' in html

    def test_content_nav_hides_register_entry_when_logged_in(self, reg_env):
        login(reg_env.client, ADMIN_USERNAME, "admin-test-pass")
        html = reg_env.client.get("/").text
        assert 'href="/register"' not in html
        assert 'href="/logout"' in html


# ---------- 注册成功（邮箱已验证） ----------


class TestRegisterSuccess:
    def test_register_creates_verified_user_and_signs_in(self, reg_env):
        before = len(_users(reg_env))
        resp = register(reg_env, "newbie")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert reg_env.client.cookies.get("acgn_session")

        rows = _users(reg_env)
        assert len(rows) == before + 1
        created = _find(reg_env, "newbie")
        assert created is not None
        assert created.role == crud.ROLE_USER  # 只可能是普通用户
        assert created.email == DEFAULT_EMAIL
        # 邮箱在注册前已用验证码验证过，建号即已验证。
        assert created.email_verified is True
        assert created.email_verified_at is not None

    def test_registered_user_appears_in_nav(self, reg_env):
        register(reg_env, "nav-user")
        home = reg_env.client.get("/")
        assert home.status_code == 200
        assert "nav-user" in home.text
        assert "普通用户" in home.text

    def test_register_hashes_password(self, reg_env):
        register(reg_env, "hash-check")
        created = _find(reg_env, "hash-check")
        assert created.password_hash != "plain-secret"
        assert "plain-secret" not in created.password_hash

    def test_registered_user_can_login_with_own_password(self, reg_env):
        register(reg_env, "login-later", "own-pass-1", email="later@example.com")
        reg_env.client.cookies.clear()
        resp = login(reg_env.client, "login-later", "own-pass-1")
        assert resp.status_code == 303
        assert reg_env.client.cookies.get("acgn_session")

    def test_registered_user_is_still_denied_admin_pages(self, reg_env):
        """核心约束：注册只能得到普通用户，拿不到测试管理平台访问权。"""
        register(reg_env, "plain-jane", email="jane@example.com")
        for path in ("/testcases", "/runs", "/batches", "/manage/sync/runs", "/manage"):
            assert reg_env.client.get(path, follow_redirects=False).status_code == 403, path

    def test_registered_user_is_denied_admin_api(self, reg_env):
        register(reg_env, "api-plain", email="api@example.com")
        resp = reg_env.client.get("/api/testcases", follow_redirects=False)
        assert resp.status_code == 403

    def test_register_trims_username_and_email(self, reg_env):
        register(reg_env, "  spaced  ", email="  Spaced@Example.COM ")
        created = _find(reg_env, "spaced")
        assert created is not None
        assert created.email == "spaced@example.com"

    def test_existing_admin_login_still_works(self, reg_env):
        """改造不影响既有账号登录。"""
        resp = login(reg_env.client, ADMIN_USERNAME, "admin-test-pass")
        assert resp.status_code == 303
        assert reg_env.client.cookies.get("acgn_session")


# ---------- 邀请码：可选 ----------


class TestInviteIsOptional:
    def test_register_without_invite_code_succeeds(self, reg_env):
        before = len(_users(reg_env))
        resp = register(reg_env, "noinvite", email="noinvite@example.com")
        assert resp.status_code == 303
        assert _find(reg_env, "noinvite") is not None
        assert len(_users(reg_env)) == before + 1

    def test_register_with_valid_invite_code_consumes_it(self, reg_env):
        code = reg_env.invites.pop(0)
        resp = register(reg_env, "withinvi", email="withinvi@example.com", invite_code=code)
        assert resp.status_code == 303
        created = _find(reg_env, "withinvi")
        assert created is not None
        db = reg_env.session_factory()
        try:
            row = crud.get_invite_code(db, code)
        finally:
            db.close()
        assert row.used_by == created.id
        assert row.used_at is not None

    def test_invalid_invite_code_is_rejected_when_sending(self, reg_env):
        before = len(_users(reg_env))
        resp = request_register_code(
            reg_env.client, "badinvite@example.com", invite_code="BADCODE12"
        )
        assert resp.status_code == 400
        assert "邀请码" in resp.text
        assert 'value="BADCODE12"' in resp.text
        assert len(_users(reg_env)) == before

    def test_invalid_invite_code_is_rejected_on_submit(self, reg_env):
        """第二步提交时同样校验邀请码，并回显原值。"""
        code = code_for(reg_env.client, "echo@example.com")
        resp = post_register(reg_env.client, **reg_form(username="echo", code=code,
                                                        invite_code="BADCODE12"))
        assert resp.status_code == 400
        assert "邀请码" in resp.text
        assert 'value="BADCODE12"' in resp.text
        assert _find(reg_env, "echo") is None

    def test_invite_required_switch_still_available(self, reg_env, monkeypatch):
        """registration_invite_required=True 可退回「必须邀请码」。"""
        monkeypatch.setattr(settings, "registration_invite_required", True)
        resp = request_register_code(reg_env.client, "needinvite@example.com")
        assert resp.status_code == 400
        assert "请填写邀请码" in resp.text


# ---------- 邮箱验证码安全 ----------


class TestEmailCodeSecurity:
    def test_send_code_returns_six_digit_code(self, reg_env):
        resp = request_register_code(reg_env.client, "six@example.com")
        assert resp.status_code == 200
        assert re.search(r"<code>\d{6}</code>", resp.text)

    def test_wrong_code_is_rejected_and_no_user_created(self, reg_env):
        code = code_for(reg_env.client, "wrong@example.com")
        before = len(_users(reg_env))
        resp = post_register(reg_env.client,
                             **reg_form("wrong@example.com", "000000", username="wrong"))
        assert resp.status_code == 400
        assert "验证码不正确" in resp.text
        assert len(_users(reg_env)) == before
        assert not reg_env.client.cookies.get("acgn_session")
        assert code  # 正确码仍在，只是没用上

    def test_code_is_bound_to_email(self, reg_env):
        """A 邮箱的码不能用来注册 B 邮箱。"""
        code_for(reg_env.client, "a@example.com")
        before = len(_users(reg_env))
        resp = post_register(reg_env.client,
                             **reg_form("b@example.com", "000000", username="bumper"))
        assert resp.status_code == 400
        assert "验证码不存在或已失效" in resp.text
        assert len(_users(reg_env)) == before

    def test_code_cannot_be_reused(self, reg_env):
        code = code_for(reg_env.client, "reuse@example.com")
        first = post_register(reg_env.client,
                              **reg_form("reuse@example.com", code, username="reuse1"))
        assert first.status_code == 303
        before = len(_users(reg_env))
        second = post_register(reg_env.client,
                               **reg_form("reuse@example.com", code, username="reuse2"))
        # 第二次提交被拒（邮箱已被占用 / 验证码已失效），且不会建出第二个账号。
        assert second.status_code == 400
        assert ("邮箱已被注册" in second.text or "验证码不存在或已失效" in second.text)
        assert len(_users(reg_env)) == before

    def test_verified_code_is_consumed_immediately(self, reg_env):
        """数据层直接验证：同一枚码第二次校验即失效。"""
        code = code_for(reg_env.client, "consume@example.com")
        db = reg_env.session_factory()
        try:
            ok, message = crud.verify_email_code(db, "consume@example.com", code)
            assert (ok, message) == (True, "")
            ok_again, message_again = crud.verify_email_code(db, "consume@example.com", code)
        finally:
            db.close()
        assert ok_again is False
        assert "验证码不存在或已失效" in message_again

    def test_expired_code_is_rejected(self, reg_env, monkeypatch):
        monkeypatch.setattr(settings, "email_code_ttl_seconds", -1)
        code = code_for(reg_env.client, "expired@example.com")
        resp = post_register(reg_env.client,
                             **reg_form("expired@example.com", code, username="expired"))
        assert resp.status_code == 400
        assert "验证码不存在或已失效" in resp.text
        assert _find(reg_env, "expired") is None

    def test_too_many_wrong_attempts_invalidates_code(self, reg_env, monkeypatch):
        monkeypatch.setattr(settings, "email_code_max_attempts", 3)
        code = code_for(reg_env.client, "brute@example.com")
        for attempt in range(3):
            resp = post_register(
                reg_env.client,
                **reg_form("brute@example.com", "111111", username="brute"),
            )
            if attempt < 2:
                assert "验证码不正确" in resp.text
            else:
                assert "验证码错误次数过多" in resp.text
        # 该码已作废，之后即便填正确码也不放行。
        resp = post_register(reg_env.client,
                             **reg_form("brute@example.com", code, username="brute"))
        assert resp.status_code == 400
        assert "验证码不存在或已失效" in resp.text
        assert _find(reg_env, "brute") is None

    def test_submitting_without_requesting_code_is_rejected(self, reg_env):
        before = len(_users(reg_env))
        resp = post_register(reg_env.client,
                             **reg_form("nocode@example.com", "123456", username="nocode"))
        assert resp.status_code == 400
        assert "验证码不存在或已失效" in resp.text
        assert len(_users(reg_env)) == before

    def test_duplicate_email_cannot_register_again(self, reg_env):
        register(reg_env, "dup1", email="dup@example.com")
        before = len(_users(reg_env))
        resp = request_register_code(reg_env.client, "dup@example.com")
        assert resp.status_code == 400
        assert "邮箱已被注册" in resp.text

        # 绕过第一步直接提交同样不行。
        resp = post_register(reg_env.client,
                             **reg_form("dup@example.com", "000000", username="dup2"))
        assert resp.status_code == 400
        assert "邮箱已被注册" in resp.text
        assert len(_users(reg_env)) == before

    def test_duplicate_email_is_case_insensitive(self, reg_env):
        register(reg_env, "case1", email="Case@Example.com")
        resp = request_register_code(reg_env.client, "CASE@example.COM")
        assert resp.status_code == 400
        assert "邮箱已被注册" in resp.text

    @pytest.mark.parametrize(
        "email", ["", "   ", "no-at-sign", "a@b", "a@b.", "@example.com", "a b@example.com"]
    )
    def test_invalid_email_is_rejected_before_sending(self, reg_env, email):
        before = len(_users(reg_env))
        resp = request_register_code(reg_env.client, email)
        assert resp.status_code == 400
        assert "邮箱" in resp.text
        assert len(_users(reg_env)) == before

    def test_send_code_has_cooldown(self, reg_env, monkeypatch):
        monkeypatch.setattr(settings, "email_resend_cooldown_seconds", 60)
        assert request_register_code(reg_env.client, "cd@example.com").status_code == 200
        second = request_register_code(reg_env.client, "cd@example.com")
        assert second.status_code == 400
        assert "发送过于频繁" in second.text

    def test_send_code_is_rate_limited_per_email(self, reg_env, monkeypatch):
        monkeypatch.setattr(settings, "email_code_max_per_email", 2)
        assert request_register_code(reg_env.client, "limit1@example.com").status_code == 200
        assert request_register_code(reg_env.client, "limit1@example.com").status_code == 200
        limited = request_register_code(reg_env.client, "limit1@example.com")
        assert limited.status_code == 400
        assert "过于频繁" in limited.text

    def test_send_code_is_rate_limited_per_ip(self, reg_env, monkeypatch):
        monkeypatch.setattr(settings, "email_code_max_per_ip", 2)
        assert request_register_code(reg_env.client, "ip1@example.com").status_code == 200
        assert request_register_code(reg_env.client, "ip2@example.com").status_code == 200
        limited = request_register_code(reg_env.client, "ip3@example.com")
        assert limited.status_code == 400
        assert "过于频繁" in limited.text

    def test_code_is_stored_only_as_hash(self, reg_env):
        code = code_for(reg_env.client, "hashed@example.com")
        db = reg_env.session_factory()
        try:
            rows = list(db.scalars(select(EmailVerificationCode)))
        finally:
            db.close()
        assert rows, "验证码未入库"
        assert all(code not in row.code_hash for row in rows)
        assert rows[-1].code_hash == crud.hash_token(f"hashed@example.com:{code}")

    def test_dev_code_fallback_can_be_disabled(self, reg_env, monkeypatch):
        """线上关闭兜底：未配邮件服务时直接报错，页面不出现验证码。"""
        monkeypatch.setattr(settings, "email_code_dev_fallback", False)
        resp = request_register_code(reg_env.client, "nodev@example.com")
        assert resp.status_code == 400
        assert "未配置邮件服务" in resp.text
        assert not re.search(r"本次验证码：<code>\d{6}</code>", resp.text)

    def test_mail_failure_blocks_registration(self, reg_env, monkeypatch):
        """配了邮件服务却发不出去：不发码、页面不显示验证码、不建号。"""
        monkeypatch.setattr(settings, "smtp_host", "smtp.gmail.com")
        monkeypatch.setattr(settings, "smtp_from_address", "no-reply@example.com")

        def boom(*_args, **_kwargs):
            raise RuntimeError("connection refused")

        monkeypatch.setattr("smtplib.SMTP", boom)
        before = len(_users(reg_env))
        resp = request_register_code(reg_env.client, "mailfail@example.com")
        assert resp.status_code == 400
        assert "邮件发送失败" in resp.text
        assert not re.search(r"本次验证码：<code>\d{6}</code>", resp.text)  # 配了 SMTP 就不显示验证码
        assert len(_users(reg_env)) == before


# ---------- 校验规则 ----------


class TestRegisterValidation:
    @pytest.mark.parametrize(
        ("username", "password", "confirm", "expected"),
        [
            ("", "pw-123456", "pw-123456", "用户名不能为空"),
            ("ab", "pw-123456", "pw-123456", "用户名长度需为 3-50 个字符"),
            ("x" * 51, "pw-123456", "pw-123456", "用户名长度需为 3-50 个字符"),
            ("bad name", "pw-123456", "pw-123456", "用户名只能包含中文、字母、数字、下划线和连字符"),
            ("bad/name", "pw-123456", "pw-123456", "用户名只能包含中文、字母、数字、下划线和连字符"),
            ("someone", "", "", "密码不能为空"),
            ("someone", "12345", "12345", "密码长度需为 6-128 位"),
            ("someone", "pw-123456", "pw-654321", "两次输入的密码不一致"),
        ],
    )
    def test_validate_registration_rules(self, reg_env, username, password, confirm, expected):
        _, error = auth.validate_registration(username, password, confirm)
        assert error == expected

    def test_validate_registration_accepts_valid_input(self, reg_env):
        name, error = auth.validate_registration("good-name_1", "pw-123456", "pw-123456")
        assert (name, error) == ("good-name_1", "")

    def test_validate_registration_allows_unicode_letters(self, reg_env):
        r"""正则用 \w，在 Python 3 下匹配 Unicode，中文用户名可用。"""
        name, error = auth.validate_registration("阿宅一号", "pw-123456", "pw-123456")
        assert (name, error) == ("阿宅一号", "")

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ({"username": ""}, "用户名不能为空"),
            ({"username": "ab"}, "用户名长度需为 3-50 个字符"),
            ({"username": "bad name"}, "用户名只能包含中文、字母、数字、下划线和连字符"),
            ({"password": "", "confirm": ""}, "密码不能为空"),
            ({"password": "12345", "confirm": "12345"}, "密码长度需为 6-128 位"),
            ({"password": "pw-123456", "confirm": "pw-654321"}, "两次输入的密码不一致"),
        ],
    )
    def test_invalid_form_returns_400_and_echoes_reason(self, reg_env, override, expected):
        code = code_for(reg_env.client)
        before = len(_users(reg_env))
        resp = post_register(reg_env.client, **reg_form(code=code, **override))
        assert resp.status_code == 400
        assert expected in resp.text
        assert len(_users(reg_env)) == before  # 不写库
        assert not reg_env.client.cookies.get("acgn_session")

    def test_duplicate_username_is_rejected(self, reg_env):
        code = code_for(reg_env.client)
        before = len(_users(reg_env))
        resp = post_register(reg_env.client, **reg_form(code=code, username=ADMIN_USERNAME))
        assert resp.status_code == 400
        assert "用户名已存在" in resp.text
        assert len(_users(reg_env)) == before
        assert not reg_env.client.cookies.get("acgn_session")

    def test_duplicate_username_is_case_sensitive_like_login(self, reg_env):
        """与登录保持一致的查询语义：按原样精确匹配用户名。"""
        register(reg_env, "CaseUser", email="case@example.com")
        assert _find(reg_env, "CaseUser") is not None
        assert _find(reg_env, "caseuser") is None


# ---------- 「只能注册用户」的强制约束 ----------


class TestRegisterRoleIsAlwaysUser:
    def test_role_in_form_is_ignored(self, reg_env):
        """即使表单里带了 role=admin，建出来的仍是普通用户。"""
        register(reg_env, "sneaky", email="sneaky@example.com", role="admin")
        created = _find(reg_env, "sneaky")
        assert created is not None
        assert created.role == crud.ROLE_USER
        assert reg_env.client.get("/testcases", follow_redirects=False).status_code == 403

    def test_role_in_query_string_is_ignored(self, reg_env):
        code = code_for(reg_env.client, "sneaky2@example.com")
        resp = reg_env.client.post(
            f"{REGISTER_PATH}?role=admin&role=root",
            data=reg_form("sneaky2@example.com", code, username="sneaky2"),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _find(reg_env, "sneaky2").role == crud.ROLE_USER

    def test_registration_role_constant_is_user(self):
        assert auth.REGISTRATION_ROLE == crud.ROLE_USER == "user"


# ---------- 跳转与开放重定向 ----------


class TestRegisterRedirect:
    def test_logged_in_user_visiting_register_is_redirected(self, reg_env):
        login(reg_env.client, ADMIN_USERNAME, "admin-test-pass")
        resp = reg_env.client.get(REGISTER_PATH, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_next_url_is_honoured(self, reg_env):
        resp = register(reg_env, "redirected", email="redirect@example.com", next="/articles")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/articles"

    def test_register_page_passes_next_to_login_link(self, reg_env):
        html = reg_env.client.get(REGISTER_PATH, params={"next": "/articles"}).text
        assert 'href="/login?next=/articles"' in html

    @pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example", "http://a.b"])
    def test_register_rejects_external_next_url(self, reg_env, bad):
        resp = register(reg_env, "no-open-redirect",
                        email="no-open-redirect@example.com", next=bad)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


# ---------- 并发与异常路径 ----------


class TestRegisterFailureHandling:
    def test_integrity_error_is_echoed_not_500(self, reg_env, monkeypatch):
        """并发注册同一用户名：唯一索引冲突也要回显成可理解的提示。"""

        def boom(*_args, **_kwargs):
            raise IntegrityError("INSERT INTO users ...", {}, Exception("UNIQUE constraint failed"))

        monkeypatch.setattr(auth.crud, "create_user", boom)
        code = code_for(reg_env.client)
        resp = post_register(reg_env.client, **reg_form(code=code, username="racer"))
        assert resp.status_code == 400
        assert "用户名已被注册" in resp.text

    def test_unexpected_error_is_echoed_not_500(self, reg_env, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(auth.crud, "create_user", boom)
        code = code_for(reg_env.client)
        resp = post_register(reg_env.client, **reg_form(code=code, username="boom-user"))
        assert resp.status_code == 400
        assert "注册失败" in resp.text
