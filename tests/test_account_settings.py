"""账号设置测试：个人资料（/settings/profile）+ 邮箱 / 手机的绑定与解绑（/settings/security）。

覆盖：
- 入口与守卫：游客跳登录页（next 带回）、/settings 重定向到个人资料分区、
  /me 不再有资料表单但留有「账号设置」入口、导航头像菜单带入口；
- 个人资料：保存 / 留空（保密）/ 生日越界与非法性别整表不落库 /
  星座与年龄为派生值 / 资料不出现在任何公开页面；
- 简介：保存 / 超长回错 / 空串清除（与资料同一个表单，一次提交）；
- 邮箱：当前密码校验、格式与唯一性、确认链接落库（模拟 SMTP）、
  未配置 SMTP 时的站内兜底（配置了 SMTP 后该路径自动关闭）、取消待确认、解绑；
- 手机：号码校验与唯一性、验证码绑定（未配置网关时验证码直接显示）、
  错码 / 过期码 / 试错次数上限、解绑需验证当前号码、取消验证；
- 隐私：页面上只出现脱敏值（a***@d / 138****8000），完整凭据不进 HTML。

依赖覆盖 + 独立临时 SQLite，不触网、不写开发库。
"""

import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud, mailer, sms
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import PhoneVerificationCode, User
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

OTHER_USERNAME = "other_user"
OTHER_PASSWORD = "other-test-pass"

DEV_CODE_PATTERN = re.compile(r'acct-dev-code">(\d{6})<')


@pytest.fixture(autouse=True)
def _no_sms_cooldown(monkeypatch):
    """测试里连着发验证码：关掉冷却，冷却本身用单独用例覆盖。"""
    monkeypatch.setattr(settings, "sms_resend_cooldown_seconds", 0)


@pytest.fixture
def env(tmp_path):
    """独立临时库 + 两个普通账号（邮箱 / 手机唯一性用例需要第二个账号）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'settings.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        crud.create_user(
            db, username=OTHER_USERNAME, password=OTHER_PASSWORD, role=crud.ROLE_USER
        )
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


def _account(session_factory, username=USER_USERNAME) -> User:
    """按用户名取账号快照（session 已关闭，用 expunge 后的属性值）。"""
    db = session_factory()
    try:
        user = crud.get_user_by_username(db, username)
        db.expunge(user)
        return user
    finally:
        db.close()


def _patch(session_factory, **values):
    """直接改库（造过期码 / 预置邮箱手机等前置状态用）。"""
    db = session_factory()
    try:
        user = crud.get_user_by_username(db, USER_USERNAME)
        for key, value in values.items():
            setattr(user, key, value)
        db.commit()
    finally:
        db.close()


def _send_phone_code(client, phone: str, purpose: str = "bind"):
    """发验证码并取回页面直接显示的明文（未配置短信网关时的兜底路径）。"""
    resp = client.post(
        "/settings/security/phone/code",
        data={"phone": phone, "purpose": purpose},
    )
    found = DEV_CODE_PATTERN.search(resp.text)
    assert found, f"页面没有回显验证码：{resp.status_code}"
    return found.group(1)


# ---------- 入口与守卫 ----------


class TestSettingsAccess:
    def test_guest_redirected_to_login(self, env):
        resp = env.client.get("/settings/profile")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=/settings/profile"

        resp = env.client.get("/settings/security")
        assert resp.headers["location"] == "/login?next=/settings/security"

    def test_root_redirects_to_profile(self, env):
        login_as_user(env.client)
        resp = env.client.get("/settings")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings/profile"

    def test_both_tabs_open_for_user_and_admin(self, env):
        login_as_user(env.client)
        for path in ("/settings/profile", "/settings/security"):
            page = env.client.get(path)
            assert page.status_code == 200
            assert "账号设置" in page.text
            # 两个分区的导航同时在场，方便互相跳
            assert 'href="/settings/profile"' in page.text
            assert 'href="/settings/security"' in page.text

        env.client.cookies.clear()
        login_as_admin(env.client)
        assert env.client.get("/settings/security").status_code == 200

    def test_nav_menu_has_settings_entry(self, env):
        login_as_user(env.client)
        home = env.client.get("/")
        assert 'href="/settings/profile"' in home.text

    def test_space_page_links_to_settings_without_form(self, env):
        """/me 不再承载资料表单，只留入口（用户要求「不要直接放个人空间」）。"""
        login_as_user(env.client)
        page = env.client.get("/me")
        assert "账号设置" in page.text
        assert "编辑资料" not in page.text
        assert 'action="/settings/profile"' not in page.text
        assert "仅你本人可见" not in page.text


# ---------- 个人资料 ----------


class TestProfilePage:
    def test_save_and_persist(self, env):
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/profile",
            data={
                "bio": "看番十年。",
                "gender": "male",
                "birthday": "1998-03-14",
                "location": "浙江 杭州",
            },
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings/profile?msg=saved"

        saved = _account(env.session_factory)
        assert saved.gender == "male"
        assert saved.birthday == date(1998, 3, 14)
        assert saved.location == "浙江 杭州"
        assert saved.bio == "看番十年。"
        expected_age = crud.age_from_birthday(saved.birthday)

        page = env.client.get(resp.headers["location"])
        assert "个人资料已保存。" in page.text
        assert "仅你本人可见" in page.text
        assert "1998-03-14" in page.text
        assert "双鱼座" in page.text
        assert f"{expected_age} 岁" in page.text
        assert "浙江 杭州" in page.text
        assert "看番十年。" in page.text

    def test_blank_means_secret(self, env):
        login_as_user(env.client)
        env.client.post(
            "/settings/profile",
            data={"bio": "", "gender": "", "birthday": "", "location": ""},
        )
        saved = _account(env.session_factory)
        assert saved.gender is None
        assert saved.birthday is None
        assert saved.location is None
        assert saved.bio is None

    def test_future_birthday_rejected_without_partial_save(self, env):
        login_as_user(env.client)
        env.client.post("/settings/profile", data={"bio": "原始简介"})
        resp = env.client.post(
            "/settings/profile",
            data={"bio": "被改掉的简介", "birthday": "2999-01-01"},
        )
        assert resp.headers["location"] == "/settings/profile?msg=invalid"

        saved = _account(env.session_factory)
        assert saved.birthday is None
        assert saved.bio == "原始简介"

    def test_bad_gender_rejected(self, env):
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/profile", data={"gender": "unknown-value"}
        )
        assert resp.headers["location"] == "/settings/profile?msg=invalid"

    def test_location_too_long_rejected(self, env):
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/profile",
            data={"location": "地" * (crud.MAX_LOCATION_LENGTH + 1)},
        )
        assert resp.headers["location"] == "/settings/profile?msg=invalid"

    def test_profile_never_rendered_on_public_pages(self, env):
        """隐私边界：资料只在本人的设置页出现。"""
        login_as_user(env.client)
        env.client.post(
            "/settings/profile",
            data={"birthday": "1998-03-14", "location": "浙江 杭州"},
        )
        for path in ("/", "/contents", "/me"):
            body = env.client.get(path).text
            assert "1998-03-14" not in body
            assert "浙江 杭州" not in body

    def test_cross_account_isolation(self, env):
        """A 保存的资料不会出现在 B 的设置页。"""
        env.client.post(
            "/login", data={"username": USER_USERNAME, "password": USER_PASSWORD}
        )
        # 用占位符里没有的地名，避免和「如：浙江 杭州」的 placeholder 撞车
        env.client.post("/settings/profile", data={"location": "四川 成都"})
        env.client.cookies.clear()

        env.client.post(
            "/login", data={"username": OTHER_USERNAME, "password": OTHER_PASSWORD}
        )
        page = env.client.get("/settings/profile")
        assert "四川 成都" not in page.text

    def test_zodiac_and_age_derivation(self):
        """星座 / 年龄为纯派生值：边界日（跨年、生日前一天）都要对。"""
        assert crud.zodiac_sign(None) is None
        assert crud.zodiac_sign(date(1990, 1, 19)) == "摩羯座"
        assert crud.zodiac_sign(date(1990, 1, 20)) == "水瓶座"
        assert crud.zodiac_sign(date(1990, 3, 14)) == "双鱼座"
        assert crud.zodiac_sign(date(1990, 12, 21)) == "射手座"
        assert crud.zodiac_sign(date(1990, 12, 22)) == "摩羯座"

        assert crud.age_from_birthday(None) is None
        assert crud.age_from_birthday(date(2000, 3, 14), today=date(2026, 3, 14)) == 26
        assert crud.age_from_birthday(date(2000, 3, 14), today=date(2026, 3, 13)) == 25
        assert crud.age_from_birthday(date(2026, 12, 1), today=date(2026, 3, 14)) is None


# ---------- 邮箱：绑定 / 换绑 / 解绑 ----------


class TestEmailBinding:
    def test_requires_current_password(self, env):
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/email",
            data={"email": "new@example.com", "password": "wrong-pass"},
        )
        assert resp.headers["location"] == "/settings/security?msg=email_pwd&panel=email-change"
        assert _account(env.session_factory).email is None

    def test_invalid_email_rejected(self, env):
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/email",
            data={"email": "not-an-email", "password": USER_PASSWORD},
        )
        assert "msg=email_invalid" in resp.headers["location"]

    def test_email_taken_by_other_account(self, env):
        _patch(env.session_factory, email="taken@example.com", email_verified=True)
        login(env.client, OTHER_USERNAME, OTHER_PASSWORD)
        resp = env.client.post(
            "/settings/security/email",
            data={"email": "taken@example.com", "password": OTHER_PASSWORD},
        )
        assert resp.headers["location"] == "/settings/security?msg=email_taken&panel=email-change"

    def test_same_email_rejected(self, env):
        _patch(env.session_factory, email="mine@example.com", email_verified=True)
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/email",
            data={"email": "MINE@example.com", "password": USER_PASSWORD},
        )
        assert resp.headers["location"] == "/settings/security?msg=email_same&panel=email-change"

    def test_bind_through_confirmation_link(self, env, monkeypatch):
        """配置了 SMTP：确认链接落库，链接点开前 users.email 不变。"""
        captured: dict[str, str] = {}

        def fake_send(to_address, username, confirm_url):
            captured["to"] = to_address
            captured["url"] = confirm_url
            return True, "已发送"

        monkeypatch.setattr(mailer, "send_email_change_email", fake_send)
        monkeypatch.setattr(mailer, "smtp_enabled", lambda: True)

        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/email",
            data={"email": "New@Example.com", "password": USER_PASSWORD},
        )
        assert resp.headers["location"] == "/settings/security?msg=email_sent"
        # 收件人用归一化后的小写地址
        assert captured["to"] == "new@example.com"
        # 还没点链接：库里什么都没变
        assert _account(env.session_factory).email is None

        page = env.client.get("/settings/security")
        assert "n***@example.com" in page.text
        # 配置了 SMTP 就不再提供站内兜底按钮
        assert "直接确认" not in page.text

        token = parse_qs(urlparse(captured["url"]).query)["token"][0]
        confirm = env.client.get(f"/settings/security/email/confirm?token={token}")
        assert confirm.headers["location"] == "/settings/security?msg=email_bound"

        saved = _account(env.session_factory)
        assert saved.email == "new@example.com"
        assert saved.email_verified is True
        assert saved.email_verified_at is not None

    def test_fallback_confirm_works_without_smtp(self, env):
        """未配置邮件服务：设置页给出「直接确认」，本地也能走完绑定。"""
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/email",
            data={"email": "dev@example.com", "password": USER_PASSWORD},
        )
        assert resp.headers["location"] == "/settings/security?msg=email_sent_dev"

        page = env.client.get("/settings/security")
        assert "待确认" in page.text
        assert "直接确认" in page.text

        confirm = env.client.post("/settings/security/email/confirm")
        assert confirm.headers["location"] == "/settings/security?msg=email_bound"
        assert _account(env.session_factory).email == "dev@example.com"

    def test_fallback_confirm_disabled_when_smtp_configured(self, env, monkeypatch):
        monkeypatch.setattr(mailer, "smtp_enabled", lambda: True)
        monkeypatch.setattr(
            mailer, "send_email_change_email", lambda *a, **k: (True, "已发送")
        )
        login_as_user(env.client)
        env.client.post(
            "/settings/security/email",
            data={"email": "x@example.com", "password": USER_PASSWORD},
        )
        confirm = env.client.post("/settings/security/email/confirm")
        assert confirm.headers["location"] == "/settings/security?msg=email_link_invalid"
        assert _account(env.session_factory).email is None

    def test_cancel_pending_keeps_old_email(self, env):
        _patch(env.session_factory, email="old@example.com", email_verified=True)
        login_as_user(env.client)
        env.client.post(
            "/settings/security/email",
            data={"email": "new@example.com", "password": USER_PASSWORD},
        )
        assert "待确认" in env.client.get("/settings/security").text

        resp = env.client.post("/settings/security/email/cancel")
        assert resp.headers["location"] == "/settings/security?msg=email_cancelled"
        page = env.client.get("/settings/security")
        assert "待确认" not in page.text
        assert _account(env.session_factory).email == "old@example.com"

    def test_cooldown_blocks_immediate_resend(self, env):
        """连点两次：第二次被冷却拦住，不能拿这个端点刷别人邮箱。"""
        login_as_user(env.client)
        first = env.client.post(
            "/settings/security/email",
            data={"email": "one@example.com", "password": USER_PASSWORD},
        )
        assert "msg=email_sent" in first.headers["location"]

        second = env.client.post(
            "/settings/security/email",
            data={"email": "two@example.com", "password": USER_PASSWORD},
        )
        assert second.headers["location"] == "/settings/security?msg=email_cooldown&panel=email-change"

    def test_unbind_cancels_pending_rebind(self, env):
        """解绑会作废待确认的换绑：否则旧链接能在解绑后把邮箱改回去。"""
        _patch(env.session_factory, email="old@example.com", email_verified=True)
        login_as_user(env.client)
        env.client.post(
            "/settings/security/email",
            data={"email": "new@example.com", "password": USER_PASSWORD},
        )
        env.client.post("/settings/security/email/unbind", data={"password": USER_PASSWORD})

        assert _account(env.session_factory).email is None
        # 待确认记录已作废，站内兜底确认也无事可做
        resp = env.client.post("/settings/security/email/confirm")
        assert resp.headers["location"] == "/settings/security?msg=email_none_pending"

    def test_invalid_token_rejected(self, env):
        login_as_user(env.client)
        resp = env.client.get("/settings/security/email/confirm?token=not-a-token")
        assert resp.headers["location"] == "/settings/security?msg=email_link_invalid"
        assert _account(env.session_factory).email is None

    def test_unbind_requires_password_then_clears(self, env):
        _patch(env.session_factory, email="old@example.com", email_verified=True)

        login_as_user(env.client)
        bad = env.client.post(
            "/settings/security/email/unbind", data={"password": "wrong"}
        )
        assert bad.headers["location"] == "/settings/security?msg=email_pwd&panel=email-unbind"
        assert _account(env.session_factory).email == "old@example.com"

        ok = env.client.post(
            "/settings/security/email/unbind", data={"password": USER_PASSWORD}
        )
        assert ok.headers["location"] == "/settings/security?msg=email_unbound"
        saved = _account(env.session_factory)
        assert saved.email is None
        assert saved.email_verified is False

        page = env.client.get("/settings/security")
        assert "尚未绑定邮箱" in page.text

    def test_full_address_never_rendered(self, env):
        """隐私：页面只出现脱敏地址，完整邮箱不进 HTML。"""
        _patch(env.session_factory, email="alice@example.com", email_verified=True)
        login_as_user(env.client)
        page = env.client.get("/settings/security").text
        assert "a***@example.com" in page
        assert "alice@example.com" not in page


# ---------- 手机：绑定 / 换绑 / 解绑 ----------


class TestPhoneBinding:
    def test_invalid_phone_rejected(self, env):
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/phone/code", data={"phone": "12345", "purpose": "bind"}
        )
        assert resp.headers["location"] == "/settings/security?msg=phone_invalid&panel=phone-bind"

    def test_phone_taken_by_other_account(self, env):
        _patch(env.session_factory, phone="13900139000")
        login(env.client, OTHER_USERNAME, OTHER_PASSWORD)
        resp = env.client.post(
            "/settings/security/phone/code",
            data={"phone": "13900139000", "purpose": "bind"},
        )
        assert resp.headers["location"] == "/settings/security?msg=phone_taken&panel=phone-bind"

    def test_bind_with_dev_code(self, env):
        """未配置短信网关：验证码直接显示在页面上，输对即绑定。"""
        login_as_user(env.client)
        code = _send_phone_code(env.client, "13800138000")
        assert _account(env.session_factory).phone is None

        bad = env.client.post("/settings/security/phone/confirm", data={"code": "000000"})
        assert bad.headers["location"] == "/settings/security?msg=phone_code_bad"
        assert _account(env.session_factory).phone is None

        ok = env.client.post("/settings/security/phone/confirm", data={"code": code})
        assert ok.headers["location"] == "/settings/security?msg=phone_bound"
        saved = _account(env.session_factory)
        assert saved.phone == "13800138000"
        assert saved.phone_bound_at is not None

        page = env.client.get("/settings/security").text
        assert "138****8000" in page
        assert "13800138000" not in page

    def test_phone_normalized_on_input(self, env):
        """+86 / 空格 / 连字符都认，落库统一成 11 位。"""
        login_as_user(env.client)
        code = _send_phone_code(env.client, "+86 138-0013-8001")
        env.client.post("/settings/security/phone/confirm", data={"code": code})
        assert _account(env.session_factory).phone == "13800138001"

    def test_code_expires(self, env):
        login_as_user(env.client)
        code = _send_phone_code(env.client, "13800138002")

        db = env.session_factory()
        try:
            record = db.query(PhoneVerificationCode).order_by(
                PhoneVerificationCode.id.desc()
            ).first()
            record.expires_at = datetime.now() - timedelta(seconds=1)
            db.commit()
        finally:
            db.close()

        resp = env.client.post("/settings/security/phone/confirm", data={"code": code})
        assert resp.headers["location"] == "/settings/security?msg=phone_code_bad"
        assert _account(env.session_factory).phone is None

    def test_attempts_limit_invalidates_code(self, env):
        """6 位码只有百万种组合：连续错到上限就作废，正确的码也不能再用。"""
        login_as_user(env.client)
        code = _send_phone_code(env.client, "13800138003")
        for _ in range(crud.PHONE_CODE_MAX_ATTEMPTS):
            env.client.post("/settings/security/phone/confirm", data={"code": "999999"})

        resp = env.client.post("/settings/security/phone/confirm", data={"code": code})
        assert resp.headers["location"] == "/settings/security?msg=phone_code_bad"
        assert _account(env.session_factory).phone is None

    def test_unbind_needs_code_for_current_number(self, env):
        _patch(env.session_factory, phone="13800138004")
        login_as_user(env.client)

        # 拿别的号码发起解绑 → 直接拒绝（不能借这个端点给别人发短信）
        mismatch = env.client.post(
            "/settings/security/phone/code",
            data={"phone": "13800138005", "purpose": "unbind"},
        )
        assert mismatch.headers["location"] == "/settings/security?msg=phone_mismatch&panel=phone-unbind"

        code = _send_phone_code(env.client, "13800138004", purpose="unbind")
        ok = env.client.post("/settings/security/phone/confirm", data={"code": code})
        assert ok.headers["location"] == "/settings/security?msg=phone_unbound"
        saved = _account(env.session_factory)
        assert saved.phone is None
        assert saved.phone_bound_at is None

    def test_cancel_discards_code(self, env):
        login_as_user(env.client)
        code = _send_phone_code(env.client, "13800138006")
        resp = env.client.post("/settings/security/phone/cancel")
        assert resp.headers["location"] == "/settings/security?msg=phone_cancelled"

        confirm = env.client.post("/settings/security/phone/confirm", data={"code": code})
        assert confirm.headers["location"] == "/settings/security?msg=phone_code_bad"
        assert _account(env.session_factory).phone is None

    def test_resend_reuses_pending_number(self, env):
        """「重新发送」不带号码：沿用待验证记录的号码，不接受前端另填。"""
        login_as_user(env.client)
        _send_phone_code(env.client, "13800138007")
        resp = env.client.post("/settings/security/phone/code", data={})
        found = DEV_CODE_PATTERN.search(resp.text)
        assert found is not None
        env.client.post("/settings/security/phone/confirm", data={"code": found.group(1)})
        assert _account(env.session_factory).phone == "13800138007"

    def test_cooldown_blocks_immediate_resend(self, env, monkeypatch):
        monkeypatch.setattr(settings, "sms_resend_cooldown_seconds", 60)
        login_as_user(env.client)
        _send_phone_code(env.client, "13800138008")
        resp = env.client.post("/settings/security/phone/code", data={})
        assert resp.headers["location"] == "/settings/security?msg=phone_cooldown"

    def test_send_via_gateway_hides_code(self, env, monkeypatch):
        """配置了网关：页面上不再显示验证码，改为「已发送」。"""
        sent: dict[str, str] = {}

        def fake_send(phone, code, purpose="bind"):
            sent["phone"] = phone
            sent["code"] = code
            return True, "验证码已发送"

        monkeypatch.setattr(sms, "send_verification_code", fake_send)
        login_as_user(env.client)
        resp = env.client.post(
            "/settings/security/phone/code", data={"phone": "13800138009", "purpose": "bind"}
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings/security?msg=phone_code_sent"
        assert sent["phone"] == "13800138009"
        assert re.fullmatch(r"\d{6}", sent["code"])

        page = env.client.get("/settings/security").text
        assert "acct-dev-code" not in page
        assert sent["code"] not in page


# ---------- 工具函数 ----------


class TestPhoneHelpers:
    def test_normalize(self):
        assert crud.normalize_phone(None) is None
        assert crud.normalize_phone("  ") is None
        assert crud.normalize_phone("138 0013 8000") == "13800138000"
        assert crud.normalize_phone("138-0013-8000") == "13800138000"
        assert crud.normalize_phone("+8613800138000") == "13800138000"
        assert crud.normalize_phone("008613800138000") == "13800138000"

    def test_validate(self):
        assert crud.validate_phone("13800138000") == ("13800138000", "")
        assert crud.validate_phone("")[1] != ""
        assert crud.validate_phone("12345678901")[1] != ""
        assert crud.validate_phone("1380013800")[1] != ""

    def test_mask(self):
        assert crud.mask_phone("13800138000") == "138****8000"
        assert crud.mask_phone(None) == ""
        assert crud.mask_email("alice@example.com") == "a***@example.com"
        assert crud.mask_email(None) == ""
