"""认证与访问控制：登录/登出路由、当前用户解析与管理员守卫。

设计要点：
- 会话使用「签名 Cookie + 服务端查库」：Cookie 只保存用户 id，角色每次请求
  从数据库读取，账号被停用或改角色后即时生效；
- 页面与 API 使用两套守卫，拒绝方式不同：
  * 页面：未登录 -> 303 跳转登录页；已登录但非管理员 -> 403 页面；
  * API ：未登录 -> 401；已登录但非管理员 -> 403（均为 JSON 响应）；
- 仅「测试管理平台」（页面 /testcases、/runs、/batches、/schedule/runs 与
  对应的测试管理 API）需要管理员；内容平台对所有人（含未登录）开放。
"""

from dataclasses import dataclass
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import crud
from app.config import settings
from app.page_common import DbSession, templates
from app.security import create_session_token, read_session_token, verify_password

SESSION_COOKIE = "acgn_session"

ROLE_ADMIN = crud.ROLE_ADMIN
ROLE_USER = crud.ROLE_USER


@dataclass(frozen=True)
class CurrentUser:
    """模板与守卫共用的当前用户视图对象（不持有数据库会话）。"""

    id: int
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def role_label(self) -> str:
        return "管理员" if self.is_admin else "普通用户"


class LoginRequired(Exception):
    """未登录访问受保护页面；由异常处理器重定向到登录页。"""

    def __init__(self, next_url: str) -> None:
        super().__init__("login required")
        self.next_url = next_url


class PermissionDenied(Exception):
    """已登录但角色不足；由异常处理器渲染 403 页面。"""


def get_current_user(request: Request, db: Session) -> CurrentUser | None:
    """解析当前登录用户；无 Cookie、令牌无效/过期或账号已删除时返回 None。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = read_session_token(
        token, settings.auth_secret_key, settings.session_max_age_seconds
    )
    if user_id is None:
        return None
    account = crud.get_user(db, user_id)
    if account is None:
        return None
    return CurrentUser(id=account.id, username=account.username, role=account.role)


def load_current_user(request: Request, db: DbSession) -> None:
    """应用级依赖：把当前用户挂到 request.state，供模板上下文与各守卫复用。

    作为应用级依赖注册后，所有路由（含内容页面）都能拿到登录状态，
    模板上下文处理器只需读取 request.state.current_user。
    """
    request.state.current_user = get_current_user(request, db)


def safe_next_url(next_url: str | None) -> str:
    """只接受站内相对路径，避免开放重定向；不合法时回到首页。"""
    if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def require_admin_page(request: Request, db: DbSession) -> CurrentUser:
    """页面守卫：未登录跳登录页；已登录但非管理员拒绝访问。"""
    user = get_current_user(request, db)
    if user is None:
        raise LoginRequired(next_url=request.url.path)
    if not user.is_admin:
        raise PermissionDenied()
    return user


def require_admin_api(request: Request, db: DbSession) -> CurrentUser:
    """API 守卫：未登录 401；已登录但非管理员 403（均返回 JSON）。"""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录后才能访问测试管理接口")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限才能访问测试管理接口")
    return user


router = APIRouter(tags=["auth"])


def _login_page(request: Request, *, next_url: str, error: str = "",
                username: str = "", status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"next": next_url, "error": error, "username": username},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: DbSession,
    next_url: str = Query(default="/", alias="next"),
):
    """登录页；已登录用户直接回到目标页面。"""
    target = safe_next_url(next_url)
    if get_current_user(request, db) is not None:
        return RedirectResponse(target, status_code=303)
    return _login_page(request, next_url=target)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DbSession,
    username: str = Form(""),
    password: str = Form(""),
    next_url: str = Form("/", alias="next"),
):
    """校验账号密码：成功写入会话 Cookie 并跳转，失败回显错误（400）。"""
    target = safe_next_url(next_url)
    account = crud.get_user_by_username(db, username.strip())
    if account is None or not verify_password(password, account.password_hash):
        return _login_page(
            request,
            next_url=target,
            error="用户名或密码不正确",
            username=username.strip(),
            status_code=400,
        )
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(account.id, settings.auth_secret_key),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def _logout_response() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    """登出：清除会话 Cookie 并回到内容平台首页。"""
    return _logout_response()


@router.get("/logout")
def logout_via_link() -> RedirectResponse:
    """登出的 GET 入口，便于导航栏直接放置链接。"""
    return _logout_response()


def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    """未登录访问受保护页面 -> 跳转登录页并记录返回地址。"""
    return RedirectResponse(
        f"/login?next={quote(exc.next_url, safe='/')}", status_code=303
    )


def permission_denied_handler(request: Request, exc: PermissionDenied) -> HTMLResponse:
    """已登录但无权访问管理区域 -> 渲染 403 页面。"""
    return templates.TemplateResponse(
        request=request, name="403.html", context={}, status_code=403
    )
