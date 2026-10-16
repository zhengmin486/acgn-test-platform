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
