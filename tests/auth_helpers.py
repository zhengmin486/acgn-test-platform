"""认证测试辅助：在临时库中创建账号，并让 TestClient 以指定角色登录。

集中放在这里，避免各测试模块重复同样的账号与登录模板代码；
仅供测试使用，不参与应用运行时逻辑。
"""

from app import crud

ADMIN_USERNAME = "auth_admin"
ADMIN_PASSWORD = "admin-test-pass"
USER_USERNAME = "auth_user"
USER_PASSWORD = "user-test-pass"


def create_accounts(session_factory, *, admin: bool = True, user: bool = True) -> None:
    """在 session_factory 对应的库中创建测试账号（口令为测试专用）。"""
    db = session_factory()
    try:
        if admin:
            crud.create_user(
                db, username=ADMIN_USERNAME, password=ADMIN_PASSWORD, role=crud.ROLE_ADMIN
            )
        if user:
            crud.create_user(
                db, username=USER_USERNAME, password=USER_PASSWORD, role=crud.ROLE_USER
            )
    finally:
        db.close()


def login(client, username: str, password: str):
    """提交登录表单；不自动跟随重定向，便于断言跳转目标。"""
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def login_as_admin(client) -> None:
    """以管理员身份登录（供需要访问测试管理平台的既有用例复用）。"""
    resp = login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert resp.status_code == 303, f"管理员登录失败：{resp.status_code} {resp.text}"


def login_as_user(client) -> None:
    """以普通用户身份登录。"""
    resp = login(client, USER_USERNAME, USER_PASSWORD)
    assert resp.status_code == 303, f"普通用户登录失败：{resp.status_code} {resp.text}"


# ---------- 注册流程（邮箱验证码） ----------
# 注册现在是两步：POST /register/code 发码 -> POST /register 校验码 + 建号。
# 测试环境没有配置 SMTP，验证码由注册页的「未配置邮件服务」兜底面板给出。

import re

_DEV_CODE_PATTERN = re.compile(r"本次验证码：<code>(\d{6})</code>")


def request_register_code(client, email: str, **extra):
    """走 /register/code 申请验证码，返回响应（页面里含验证码）。"""
    data = {"email": email}
    data.update(extra)
    return client.post("/register/code", data=data, follow_redirects=False)


def extract_code(response) -> str:
    """从未配置邮件服务时的兜底面板里取出验证码。"""
    match = _DEV_CODE_PATTERN.search(response.text)
    assert match is not None, f"页面未给出验证码：{response.text[:400]}"
    return match.group(1)


def register(client, username: str, password: str = "pw-123456",
             email: str | None = None, code: str | None = None, **extra):
    """按新流程注册：先申请验证码，再提交（不跟随重定向）。

    ``code`` 可显式传入（测错误码 / 复用码 / 过期码）；不传则自动取本次签发的码。
    """
    address = email or f"{username}@example.com"
    if code is None:
        code_response = request_register_code(client, address, **extra)
        if code_response.status_code != 200:
            # 发码这一步就被拒（邀请码无效 / 邮箱已注册 / 限流…）：直接把它交回断言。
            return code_response
        code = extract_code(code_response)
    data = {
        "username": username,
        "password": password,
        "confirm": password,
        "email": address,
        "code": code,
    }
    data.update(extra)
    return client.post("/register", data=data, follow_redirects=False)
