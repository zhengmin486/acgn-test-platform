"""注册功能测试：表单渲染、入库结果、校验规则与「只能注册普通用户」这一约束。

覆盖：
- 注册页表单渲染，登录页与内容平台导航栏的注册入口；
- 注册成功：写入 users 表、角色固定为 user、邮箱待验证；
- 校验规则：空 / 过短 / 非法字符用户名、空 / 过短 / 不一致密码、用户名与邮箱占用；
- 安全：表单里传入 role=admin 一律忽略，注册出的账号访问管理页面仍是 403；
- next 参数生效与开放重定向防护；
- 并发下的唯一索引冲突（IntegrityError）也能回显而不是 500。

启用邮箱验证后注册不再直接登录，需先完成邮箱验证（相关流程见
tests/test_email_verification.py）；本文件相应断言已按新行为调整。

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
from app.database import Base, get_db
from app.main import app
from app.models import User
from auth_helpers import ADMIN_USERNAME, create_accounts, login

REGISTER_PATH = "/register"
DEFAULT_EMAIL = "register@example.com"
TOKEN_PATTERN = re.compile(r"/verify/email\?token=([A-Za-z0-9_\-]+)")


def register(env, username: str, password: str, email: str = DEFAULT_EMAIL, **extra):
    """提交注册表单（默认消耗一个有效邀请码）；不跟随重定向。

    传入 ``invite_code=...`` 可指定具体码（测拒用场景）；不传则从夹具的
    邀请码池取一个——默认注册模式是 invite，没有码注册会直接被拒。
    """
    data = {"username": username, "password": password, "confirm": password, "email": email}
    data.update(extra)
    if "invite_code" not in data:
        assert env.invites, "邀请码池已耗尽，请在夹具中补充"
        data["invite_code"] = env.invites.pop(0)
    return env.client.post(REGISTER_PATH, data=data, follow_redirects=False)


def verify_via_link(client, next_url: str = "/") -> str:
    """走「生成验证链接」取得并访问验证地址，返回验证页文本。"""
    resp = client.post("/verify/link", data={"next": next_url}, follow_redirects=False)
    assert resp.status_code == 200, f"生成验证链接失败：{resp.status_code}"
    match = TOKEN_PATTERN.search(resp.text)
    assert match is not None, f"页面未包含验证链接：{resp.text}"
    return client.get(match.group(0), follow_redirects=False).text


@pytest.fixture
def reg_env(tmp_path):
    """独立临时库 + 预置账号（用于重名校验）+ 邀请码池 + get_db 覆盖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'register.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(TestingSession)
    # 默认 invite 模式：预置一批可用邀请码，register() 助手逐个消耗。
    invites = [
        row.code
        for row in crud.create_invite_codes(
            TestingSession(), count=crud.INVITE_CODE_MAX_BATCH
        )
    ]

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


# ---------- 注册成功 ----------


class TestRegisterSuccess:
    def test_register_creates_user_with_user_role(self, reg_env):
        before = len(_users(reg_env))
        resp = register(reg_env, "newbie", "pw-123456")
        # 启用邮箱验证后不再直接登录，而是跳到待验证页。
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/verify/pending")

        rows = _users(reg_env)
        assert len(rows) == before + 1
        created = _find(reg_env, "newbie")
        assert created is not None
        assert created.role == crud.ROLE_USER  # 只可能是普通用户
        assert created.email == DEFAULT_EMAIL
        assert created.email_verified is False

    def test_register_does_not_create_session_before_verification(self, reg_env):
        """未完成邮箱验证前不签发会话，导航栏仍显示登录入口。"""
        register(reg_env, "nav-user", "pw-123456")
        assert not reg_env.client.cookies.get("acgn_session")

        home = reg_env.client.get("/")
        assert home.status_code == 200
        assert "nav-user" not in home.text
        assert 'href="/login"' in home.text

    def test_verified_user_appears_in_nav_after_login(self, reg_env):
        """完成验证后登录，导航栏才显示用户名与普通用户标签。"""
        register(reg_env, "verified-nav", "pw-123456")
        assert "邮箱验证成功" in verify_via_link(reg_env.client)
        assert login(reg_env.client, "verified-nav", "pw-123456").status_code == 303

        home = reg_env.client.get("/")
        assert home.status_code == 200
        assert "verified-nav" in home.text
        assert "普通用户" in home.text

    def test_register_hashes_password(self, reg_env):
        register(reg_env, "hash-check", "plain-secret")
        created = _find(reg_env, "hash-check")
        assert created.password_hash != "plain-secret"
        assert "plain-secret" not in created.password_hash

    def test_registered_user_can_login_with_own_password(self, reg_env):
        register(reg_env, "login-later", "own-pass-1", email="later@example.com")
        verify_via_link(reg_env.client)
        reg_env.client.cookies.clear()
        resp = login(reg_env.client, "login-later", "own-pass-1")
        assert resp.status_code == 303
        assert reg_env.client.cookies.get("acgn_session")

    def test_registered_user_is_still_denied_admin_pages(self, reg_env):
        """核心约束：注册只能得到普通用户，拿不到测试管理平台访问权。"""
        register(reg_env, "plain-jane", "pw-123456", email="jane@example.com")
        verify_via_link(reg_env.client)
        login(reg_env.client, "plain-jane", "pw-123456")
        for path in ("/testcases", "/runs", "/batches", "/manage/sync/runs", "/manage"):
            assert reg_env.client.get(path, follow_redirects=False).status_code == 403, path

    def test_registered_user_is_denied_admin_api(self, reg_env):
        register(reg_env, "api-plain", "pw-123456", email="api@example.com")
        verify_via_link(reg_env.client)
        login(reg_env.client, "api-plain", "pw-123456")
        resp = reg_env.client.get("/api/testcases", follow_redirects=False)
        assert resp.status_code == 403

    def test_register_trims_username_and_email(self, reg_env):
        register(reg_env, "  spaced  ", "pw-123456", email="  Spaced@Example.COM ")
        created = _find(reg_env, "spaced")
        assert created is not None
        assert created.email == "spaced@example.com"

    def test_registered_user_can_browse_content(self, reg_env):
        register(reg_env, "browser", "pw-123456", email="browser@example.com")
        for path in ("/", "/articles", "/schedule"):
            assert reg_env.client.get(path, follow_redirects=False).status_code == 200, path


# ---------- 校验规则 ----------


class TestRegisterValidation:
    @pytest.mark.parametrize(
        ("username", "password", "confirm", "expected"),
        [
            ("", "pw-123456", "pw-123456", "用户名不能为空"),
            ("ab", "pw-123456", "pw-123456", "用户名长度需为 3-50 个字符"),
            ("x" * 51, "pw-123456", "pw-123456", "用户名长度需为 3-50 个字符"),
            ("bad name", "pw-123456", "pw-123456", "用户名只能包含字母、数字、下划线和连字符"),
            ("bad/name", "pw-123456", "pw-123456", "用户名只能包含字母、数字、下划线和连字符"),
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
        ("form", "expected"),
        [
            ({"username": "", "password": "pw-123456", "confirm": "pw-123456"}, "用户名不能为空"),
            ({"username": "ab", "password": "pw-123456", "confirm": "pw-123456"}, "用户名长度需为 3-50 个字符"),
            ({"username": "bad name", "password": "pw-123456", "confirm": "pw-123456"},
             "用户名只能包含字母、数字、下划线和连字符"),
            ({"username": "someone", "password": "", "confirm": ""}, "密码不能为空"),
            ({"username": "someone", "password": "12345", "confirm": "12345"}, "密码长度需为 6-128 位"),
            ({"username": "someone", "password": "pw-123456", "confirm": "pw-654321"}, "两次输入的密码不一致"),
        ],
    )
    def test_invalid_form_returns_400_and_echoes_reason(self, reg_env, form, expected):
        before = len(_users(reg_env))
        resp = reg_env.client.post(REGISTER_PATH, data=form, follow_redirects=False)
        assert resp.status_code == 400
        assert expected in resp.text
        assert len(_users(reg_env)) == before  # 不写库
        assert not reg_env.client.cookies.get("acgn_session")

    def test_duplicate_username_is_rejected(self, reg_env):
        before = len(_users(reg_env))
        resp = reg_env.client.post(
            REGISTER_PATH,
            data={"username": ADMIN_USERNAME, "password": "pw-123456", "confirm": "pw-123456"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "用户名" in resp.text
        assert len(_users(reg_env)) == before
        assert not reg_env.client.cookies.get("acgn_session")

    def test_duplicate_username_is_case_sensitive_like_login(self, reg_env):
        """与登录保持一致的查询语义：按原样精确匹配用户名。"""
        register(reg_env, "CaseUser", "pw-123456", email="case@example.com")
        assert _find(reg_env, "CaseUser") is not None
        assert _find(reg_env, "caseuser") is None


# ---------- 「只能注册用户」的强制约束 ----------


class TestRegisterRoleIsAlwaysUser:
    def test_role_in_form_is_ignored(self, reg_env):
        """即使表单里带了 role=admin，建出来的仍是普通用户。"""
        register(reg_env, "sneaky", "pw-123456",
                 email="sneaky@example.com", role="admin")
        created = _find(reg_env, "sneaky")
        assert created is not None
        assert created.role == crud.ROLE_USER

        verify_via_link(reg_env.client)
        login(reg_env.client, "sneaky", "pw-123456")
        assert reg_env.client.get("/testcases", follow_redirects=False).status_code == 403

    def test_role_in_query_string_is_ignored(self, reg_env):
        resp = reg_env.client.post(
            f"{REGISTER_PATH}?role=admin&role=root",
            data={
                "username": "sneaky2",
                "password": "pw-123456",
                "confirm": "pw-123456",
                "email": "sneaky2@example.com",
                "invite_code": reg_env.invites.pop(0),
            },
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
        """注册后跳待验证页，并把 next 原样带过去（验证完仍回到目标页）。"""
        resp = register(reg_env, "redirected", "pw-123456",
                        email="redirect@example.com", next="/articles")
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/verify/pending")
        assert "next=/articles" in location

        assert "邮箱验证成功" in verify_via_link(reg_env.client, next_url="/articles")
        # auth_helpers.login 不带 next，这里单独提交一次以验证登录后的目标页。
        resp = reg_env.client.post(
            "/login",
            data={"username": "redirected", "password": "pw-123456", "next": "/articles"},
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/articles"

    def test_register_page_passes_next_to_login_link(self, reg_env):
        html = reg_env.client.get(REGISTER_PATH, params={"next": "/articles"}).text
        assert 'href="/login?next=/articles"' in html

    @pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example", "http://a.b"])
    def test_register_rejects_external_next_url(self, reg_env, bad):
        resp = register(reg_env, "no-open-redirect", "pw-123456",
                        email="no-open-redirect@example.com", next=bad)
        assert resp.status_code == 303
        # 外部地址被收敛成 "/"，并随待验证页一起回传。
        assert resp.headers["location"] == "/verify/pending?sent=0&next=/"


# ---------- 并发与异常路径 ----------


class TestRegisterFailureHandling:
    def test_integrity_error_is_echoed_not_500(self, reg_env, monkeypatch):
        """并发注册同一用户名：唯一索引冲突也要回显成可理解的提示。"""

        def boom(*_args, **_kwargs):
            raise IntegrityError("INSERT INTO users ...", {}, Exception("UNIQUE constraint failed"))

        monkeypatch.setattr(auth.crud, "create_user", boom)
        resp = register(reg_env, "racer", "pw-123456")
        assert resp.status_code == 400
        assert "用户名已被注册" in resp.text

    def test_unexpected_error_is_echoed_not_500(self, reg_env, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(auth.crud, "create_user", boom)
        resp = register(reg_env, "boom-user", "pw-123456")
        assert resp.status_code == 400
        assert "注册失败" in resp.text
