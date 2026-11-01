"""认证与访问控制：登录/登出路由、当前用户解析与管理员守卫。

设计要点：
- 会话使用「签名 Cookie + 服务端查库」：Cookie 只保存用户 id，角色每次请求
  从数据库读取，账号被停用或改角色后即时生效；
- 角色分三级：user（普通用户）< admin（管理员）< owner（站长）。
  管理员与站长都能进测试管理平台（``is_staff``），站长额外独占角色管理等站点级操作。
- 页面与 API 使用两套守卫，拒绝方式不同：
  * 页面：未登录 -> 303 跳转登录页；已登录但角色不足 -> 403 页面；
  * API ：未登录 -> 401；已登录但角色不足 -> 403（均为 JSON 响应）；
- 仅「测试管理平台」（页面 /testcases、/runs、/batches、/manage/sync/runs 与
  对应的测试管理 API）需要管理员及以上；内容平台对所有人（含未登录）开放。
- **注册角色只由服务端决定，绝不取信前端**：普通用户注册不需要邀请码（默认即开放注册）；若填了**管理员邀请码**（仅站长可发放），注册出的账号即为 ``admin``。表单 / 查询串里的任何 role 值一律忽略，角色只来自数据库里那张邀请码的记录。站长（owner）不通过此通道发放，仍走 scripts/create_user.py / 角色管理页。
  出现的任何 role 值一律忽略，管理员账号只能通过 scripts/create_user.py 创建。
- **注册前验证邮箱**：注册流程为「填邮箱 -> 收 6 位验证码 -> 填验证码 -> 设密码
  -> 建号」，验证码在服务端签发与校验（入库只存哈希，校验通过立即失效），
  因此新账号建好即「邮箱已验证」并可登录，不再有待验证中间态。
  邮件经 Gmail SMTP 发送（详见 app/mailer.py）；未配置 SMTP 时验证码只显示在
  注册页（本地联调），配置后发送失败一律如实回显、不伪造成功。
  关闭 ``email_verification_enabled`` 即完全退回「注册即登录」的旧行为。
- **邀请码可选，且只决定「是否管理员」**：留空直接注册为普通用户；填了普通用户
  邀请码仍是普通用户；只有管理员邀请码才会注册成管理员。``registration_invite_required``
  置 True 可退回「必须填邀请码」的旧行为。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crud, mailer
from app.config import settings
from app.page_common import DbSession, templates
from app.security import create_session_token, decode_session, verify_password

SESSION_COOKIE = "acgn_session"
# 注册后到完成验证前的临时 Cookie：只存用户 id 的签名令牌，用于在待验证页面
# 找回「刚注册的账号」，避免把用户名暴露在 URL 上。
PENDING_COOKIE = "acgn_pending"
# 待验证 Cookie 的有效期（秒）。刻意与令牌有效期解耦——令牌过期只影响验证链接，
# 不应连带把待验证页变成「找不到账号」（否则无法重新发送）。
PENDING_COOKIE_MAX_AGE = 24 * 60 * 60

ROLE_ADMIN = crud.ROLE_ADMIN
ROLE_USER = crud.ROLE_USER
ROLE_OWNER = crud.ROLE_OWNER

# ---------- 注册规则 ----------
# 注册只开放给普通用户：角色固定为 ROLE_USER，不接受任何外部传入值。
REGISTRATION_ROLE = ROLE_USER

# 用户名：3-50 个字符，仅字母 / 数字 / 下划线 / 连字符（Unicode 字母亦允许）。
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 50
_USERNAME_PATTERN = re.compile(r"^[\w-]+$", re.UNICODE)

# 密码：6-128 位；PBKDF2 无长度上限，这里只做常规强度约束。
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128

# 邮箱：常规「本地部分@域名.后缀」校验，不做 RFC 全量语法，避免误杀正常邮箱。
MAX_EMAIL_LENGTH = 200
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class CurrentUser:
    """模板与守卫共用的当前用户视图对象（不持有数据库会话）。"""

    id: int
    username: str
    role: str
    avatar_url: str | None = None

    @property
    def is_staff(self) -> bool:
        """是否可进入管理后台：站长与管理员均可，普通用户不可。"""
        return crud.has_role(self.role, ROLE_ADMIN)

    @property
    def is_owner(self) -> bool:
        """是否为站长——最高级角色，独占角色管理等站点级操作。"""
        return self.role == ROLE_OWNER

    @property
    def role_label(self) -> str:
        return crud.ROLE_LABELS.get(self.role, self.role)


class LoginRequired(Exception):
    """未登录访问受保护页面；由异常处理器重定向到登录页。"""

    def __init__(self, next_url: str) -> None:
        super().__init__("login required")
        self.next_url = next_url


class PermissionDenied(Exception):
    """已登录但角色不足；由异常处理器渲染 403 页面。"""


def get_current_user(request: Request, db: Session) -> CurrentUser | None:
    """解析当前登录用户；无 Cookie、令牌无效/过期或账号已删除时返回 None。

    另有两种情况同样视为未登录：
    - 邮箱未验证（仅当功能开启且该账号有邮箱），避免绕过验证流程；
    - 会话签发时间早于最近一次改密时间——改密后旧设备/旧标签页应被踢下线，
      会话 Cookie 无法逐条吊销，只能靠 ``iat`` 与 ``password_changed_at`` 比较。
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    claims = decode_session(
        token, settings.auth_secret_key, settings.session_max_age_seconds
    )
    if claims is None:
        return None
    account = crud.get_user(db, claims["uid"])
    if account is None:
        return None
    if account.password_changed_at is not None:
        if claims["iat"] < int(account.password_changed_at.timestamp()):
            return None
    if settings.email_verification_enabled and account.email and not account.email_verified:
        # 邮箱未验证：即使持有已签发的会话也视作未登录，避免绕过验证流程。
        # 无邮箱的历史账号（脚本创建）不受影响。
        return None
    return CurrentUser(
        id=account.id,
        username=account.username,
        role=account.role,
        avatar_url=account.avatar_url,
    )


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
    """页面守卫：未登录跳登录页；已登录但角色不足（非管理员 / 站长）拒绝访问。

    站长等级高于管理员，因此同样放行。
    """
    user = get_current_user(request, db)
    if user is None:
        raise LoginRequired(next_url=request.url.path)
    if not user.is_staff:
        raise PermissionDenied()
    return user


def require_owner_page(request: Request, db: DbSession) -> CurrentUser:
    """页面守卫：未登录跳登录页；已登录但非站长拒绝（站长专属，如角色管理）。"""
    user = get_current_user(request, db)
    if user is None:
        raise LoginRequired(next_url=request.url.path)
    if not user.is_owner:
        raise PermissionDenied()
    return user


def require_login_page(request: Request, db: DbSession) -> CurrentUser:
    """页面守卫：只要求登录、不限制角色（个人空间等登录用户功能）。

    未登录 -> LoginRequired（303 跳登录页，next 带回当前地址）；
    已登录即可访问，普通用户与管理员一视同仁。
    """
    user = get_current_user(request, db)
    if user is None:
        raise LoginRequired(next_url=request.url.path)
    return user


def require_admin_api(request: Request, db: DbSession) -> CurrentUser:
    """API 守卫：未登录 401；已登录但角色不足 403（均返回 JSON）。

    站长等级高于管理员，同样放行。
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录后才能访问测试管理接口")
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="需要管理员权限才能访问测试管理接口")
    return user


def require_owner_api(request: Request, db: DbSession) -> CurrentUser:
    """API 守卫：未登录 401；已登录但非站长 403（均返回 JSON）。"""
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="需要登录后才能访问")
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="需要站长权限才能访问")
    return user


def validate_email(email: str | None) -> tuple[str | None, str]:
    """校验邮箱，返回 ``(清理后的邮箱, 错误信息)``；错误信息为空串表示通过。

    清理规则与入库一致（去空格 + 转小写），保证「重复邮箱」判断不受大小写影响。
    """
    address = crud.normalize_email(email)
    if not address:
        return None, "邮箱不能为空"
    if len(address) > MAX_EMAIL_LENGTH:
        return address, f"邮箱长度不能超过 {MAX_EMAIL_LENGTH} 个字符"
    if not _EMAIL_PATTERN.match(address):
        return address, "邮箱格式不正确"
    return address, ""


def validate_password(password: str, confirm: str) -> str:
    """校验密码与确认密码，返回错误信息；空串表示校验通过。

    注册与找回密码共用同一套规则，避免两处强度要求不一致。
    """
    if not password:
        return "密码不能为空"
    if len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        return f"密码长度需为 {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} 位"
    if password != confirm:
        return "两次输入的密码不一致"
    return ""


def validate_registration(
    username: str,
    password: str,
    confirm: str,
    email: str | None = None,
) -> tuple[str, str]:
    """校验注册表单，返回 ``(清理后的用户名, 错误信息)``。

    ``错误信息`` 为空串表示校验通过。只做输入合法性检查，不写库：
    用户名/邮箱占用由 ``crud.create_user`` 兜底（含并发下的唯一索引冲突）。
    ``email`` 传入 None 表示本次不校验邮箱（兼容不启用验证的场景）。
    """
    name = (username or "").strip()
    if not name:
        return "", "用户名不能为空"
    if not (MIN_USERNAME_LENGTH <= len(name) <= MAX_USERNAME_LENGTH):
        return name, f"用户名长度需为 {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} 个字符"
    if not _USERNAME_PATTERN.match(name):
        return name, "用户名只能包含中文、字母、数字、下划线和连字符"
    password_error = validate_password(password, confirm)
    if password_error:
        return name, password_error
    if email is not None and settings.email_verification_enabled:
        _, mail_error = validate_email(email)
        if mail_error:
            return name, mail_error
    return name, ""


router = APIRouter(tags=["auth"])


def _login_page(request: Request, *, next_url: str, error: str = "",
                username: str = "", notice: str = "",
                status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next": next_url,
            "error": error,
            "username": username,
            "notice": notice,
        },
        status_code=status_code,
    )


def _session_redirect(target: str, account) -> RedirectResponse:
    """签发会话 Cookie 并跳转到目标地址（登录与注册共用）。"""
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


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: DbSession,
    next_url: str = Query(default="/", alias="next"),
    reset: int = Query(default=0),
):
    """登录页；已登录用户直接回到目标页面。

    ``reset=1`` 由找回密码流程跳转而来，此时提示用新密码登录。
    """
    target = safe_next_url(next_url)
    if get_current_user(request, db) is not None:
        return RedirectResponse(target, status_code=303)
    notice = "密码已重置，请使用新密码登录。" if reset else ""
    return _login_page(request, next_url=target, notice=notice)


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
    if settings.email_verification_enabled and account.email and not account.email_verified:
        # 未验证邮箱不放行；不给自动重发，避免被拿去轰炸他人邮箱。
        return _login_page(
            request,
            next_url=target,
            error="邮箱尚未验证：请查收验证邮件，或前往 /verify/resend 重新发送。",
            username=username.strip(),
            status_code=400,
        )
    return _session_redirect(target, account)


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


def _register_page(request: Request, *, next_url: str, error: str = "",
                   username: str = "", email: str = "", invite_code: str = "",
                   code_sent: bool = False, notice: str = "", dev_code: str = "",
                   cooldown: int = 0, status_code: int = 200,
                   invite_role: str = "") -> HTMLResponse:
    """渲染注册页。

    ``code_sent=True`` 表示验证码已签发，页面切到「填验证码 + 设密码」阶段；
    ``dev_code`` **只在完全没有配置邮件服务时**由路由层传入（本地联调用）——
    一旦配置了 SMTP，哪怕发送失败也不会出现，避免任何人靠页面白拿验证码。
    ``invite_role`` 为已校验邀请码登记的目标角色（'admin' 表示管理员邀请码），
    用于页面提示「将以管理员身份注册」；空串表示普通注册（或邀请码尚未校验）。
    """
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "next": next_url,
            "error": error,
            "notice": notice,
            "username": username,
            "email": email,
            "invite_code": invite_code,
            # 邀请码：默认展示输入框（可留空），只有 open 模式才完全隐藏。
            "invite_field_visible": settings.registration_mode.strip().lower() != "open",
            "invite_required": settings.registration_invite_required,
            "require_email": settings.email_verification_enabled,
            "code_sent": code_sent,
            "dev_code": dev_code,
            "cooldown": cooldown,
            "invite_role": invite_role,
            "code_ttl_minutes": max(1, settings.email_code_ttl_seconds // 60),
            "max_email_length": MAX_EMAIL_LENGTH,
            "min_username_length": MIN_USERNAME_LENGTH,
            "max_username_length": MAX_USERNAME_LENGTH,
            "min_password_length": MIN_PASSWORD_LENGTH,
            "max_password_length": MAX_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )


def _client_ip(request: Request) -> str:
    """取请求来源 IP（反代后取 X-Forwarded-For 首段），用于发码频率限制。"""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def resolve_registration_role(db: Session, invite: str) -> tuple[str, str]:
    """根据邀请码解析注册角色：返回 ``(角色, 错误信息)``，错误信息空串表示通过。

    - open 模式（``registration_invite_only=False``）：完全不碰邀请码，恒为普通用户；
    - invite 模式：留空且非必填 -> 普通用户（**普通用户注册不需要邀请码**）；
      留空但必填 -> 报错；填了 -> 校验码，有效则取码内 ``role``（管理员邀请码即 admin），
      无效则报错。角色只来自数据库里那张邀请码的记录，绝不信任前端传入值。
    """
    if not settings.registration_invite_only:
        return ROLE_USER, ""
    invite = (invite or "").strip()
    if not invite:
        if settings.registration_invite_required:
            return "", "请填写邀请码"
        return ROLE_USER, ""
    row, invite_error = crud.check_invite_code(db, invite)
    if invite_error:
        return "", invite_error
    return row.role, ""


def _consume_optional_invite(db: Session, invite: str, account) -> str:
    """建号后消费邀请码（只在 invite 模式且填了码时消费）；失败由调用方删号回显。"""
    if not invite or not settings.registration_invite_only:
        return ""
    consumed, consume_error = crud.consume_invite_code(db, invite, account.id)
    return "" if consumed else consume_error


# ---------- 邮箱验证 ----------


def _action_url(request: Request, token: str, path: str = "/verify/email") -> str:
    """生成一次性链接：优先用配置的对外基址，否则取当前请求的 host。

    邮箱验证与找回密码共用，只是落地路径不同。
    """
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}{path}?token={quote(token)}"


def _set_pending_cookie(response, user_id: int) -> None:
    """写入待验证 Cookie（签名、httpOnly），用于待验证页找回账号。"""
    response.set_cookie(
        PENDING_COOKIE,
        create_session_token(user_id, settings.auth_secret_key),
        max_age=PENDING_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _pending_user(request: Request, db: Session, *, kind: str = "verify"):
    """从待处理 Cookie 解析出「有邮箱」的账号；否则返回 None。

    Cookie 本身只带账号 id，语义是「刚注册 / 刚申请重置的那个账号」；是否额外
    要求「邮箱未验证」由场景决定：
    - ``kind="verify"``：只对尚未验证的账号有效，验证完成后自然失效；
    - ``kind="reset"``：已验证的账号同样可以找回密码，不能因已验证就被拒。
    """
    token = request.cookies.get(PENDING_COOKIE)
    if not token:
        return None
    claims = decode_session(token, settings.auth_secret_key, PENDING_COOKIE_MAX_AGE)
    if claims is None:
        return None
    account = crud.get_user(db, claims["uid"])
    if account is None or not account.email:
        return None
    if kind == "verify" and account.email_verified:
        return None
    return account


def _resend_remaining_seconds(db: Session, account, kind: str = "verify") -> int:
    """距下次可重发的剩余秒数（0 表示可以立即重发）。

    ``kind`` 指定令牌种类：``"verify"`` 邮箱验证（默认）或 ``"reset"`` 找回密码。
    """
    latest = (
        crud.latest_password_reset_created_at(db, account.id)
        if kind == "reset"
        else crud.latest_token_created_at(db, account.id)
    )
    if latest is None:
        return 0
    remaining = settings.email_resend_cooldown_seconds - (
        datetime.now() - latest
    ).total_seconds()
    return int(remaining) if remaining > 0 else 0


def _send_verification(request: Request, db: Session, account) -> tuple[bool, str]:
    """为账号签发新验证令牌并尝试发送邮件，返回 ``(是否真发出, 说明)``。"""
    token = crud.issue_email_token(db, account)
    return mailer.send_verification_email(
        account.email, account.username, _action_url(request, token)
    )


def _send_reset(request: Request, db: Session, account) -> tuple[bool, str]:
    """为账号签发重置令牌并尝试发送邮件，返回 ``(是否真发出, 说明)``。"""
    token = crud.issue_password_reset_token(db, account)
    return mailer.send_password_reset_email(
        account.email,
        account.username,
        _action_url(request, token, "/reset-password"),
    )


def _render_pending(
    request: Request,
    db: Session,
    account,
    *,
    next_url: str,
    kind: str = "verify",
    sent: bool = False,
    resent: bool = False,
    error: str = "",
    verify_url: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """渲染「链接已发出」提示页；邮箱验证与找回密码共用一个模板。

    ``kind="reset"`` 时文案与表单提交地址切换到找回密码流程。
    """
    is_reset = kind == "reset"
    return templates.TemplateResponse(
        request=request,
        name="verify_pending.html",
        context={
            "next": next_url,
            "kind": kind,
            "is_reset": is_reset,
            "email": account.email if account is not None else "",
            "sent": sent,
            "resent": resent,
            "error": error,
            "verify_url": verify_url,
            "smtp_enabled": mailer.smtp_enabled(),
            "resend_action": "/reset/resend" if is_reset else "/verify/resend",
            "link_action": "/reset/link" if is_reset else "/verify/link",
            "cooldown": (
                _resend_remaining_seconds(db, account, kind)
                if account is not None
                else 0
            ),
        },
        status_code=status_code,
    )


@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    db: DbSession,
    next_url: str = Query(default="/", alias="next"),
):
    """注册页（仅注册普通用户）；已登录用户直接回到目标页面。"""
    target = safe_next_url(next_url)
    if get_current_user(request, db) is not None:
        return RedirectResponse(target, status_code=303)
    return _register_page(request, next_url=target)


@router.post("/register/code", response_class=HTMLResponse)
def register_send_code(
    request: Request,
    db: DbSession,
    email: str = Form(""),
    username: str = Form(""),
    invite_code: str = Form(""),
    next_url: str = Form("/", alias="next"),
):
    """注册第一步：给邮箱发一枚 6 位数字验证码（服务端签发，入库只存哈希）。

    - 邮箱格式、是否已注册、可选邀请码都在**发码前**校验，避免白扔邮件；
    - 冷却（默认 60 秒）与邮箱 / IP 维度的频率限制在此拦截；
    - **发信失败一律如实回显**（连接失败 / 认证失败 / 发送失败），不伪造成功，
      也不在页面上显示验证码——只有「压根没配邮件服务」时才显示（本地联调）。
    - 出口分两路（只看请求头，业务分支完全一样）：
      * 普通表单提交（无 JS / fetch 失败回退）→ 整页重渲染；
      * 前端带 ``X-Requested-With: fetch`` → 只回轻量 JSON。
      注册页已改成「先填账号信息，再点发送验证码」，整页刷新会把已经填好的用户名
      清掉；所以无 JS 回退路径也要把 username 回填（setdefault 一处统一补，
      免得每个 return 都记得带）。密码一律不回填——不把明文塞进 HTML 响应。
    """
    target = safe_next_url(next_url)
    if not settings.email_verification_enabled:
        return RedirectResponse(
            f"/register?next={quote(target, safe='/')}", status_code=303
        )
    typed_name = (username or "").strip()

    def respond(**kw):
        """发码结果统一出口：包一层是为了「同一份业务分支」既能出 HTML 也能出 JSON。"""
        kw.setdefault("username", typed_name)
        if request.headers.get("x-requested-with", "").strip().lower() != "fetch":
            return _register_page(request, **kw)
        return JSONResponse(
            {
                "ok": not kw.get("error") and bool(kw.get("code_sent")),
                "error": kw.get("error", ""),
                "notice": kw.get("notice", ""),
                "dev_code": kw.get("dev_code", ""),
                "cooldown": int(kw.get("cooldown") or 0),
                "code_sent": bool(kw.get("code_sent")),
            },
            status_code=kw.get("status_code", 200),
        )

    invite = (invite_code or "").strip()
    address, mail_error = validate_email(email)
    if mail_error:
        return respond(
            next_url=target, error=mail_error,
            email=email, invite_code=invite, status_code=400,
        )
    if crud.get_user_by_email(db, address) is not None:
        return respond(
            next_url=target, error=f"邮箱已被注册：{address}",
            email=address, invite_code=invite, status_code=400,
        )
    invite_error = ""
    role, invite_error = resolve_registration_role(db, invite)
    if invite_error:
        return respond(
            next_url=target, error=invite_error,
            email=address, invite_code=invite, status_code=400,
        )

    remaining = crud.email_code_cooldown_remaining(db, address)
    if remaining > 0:
        return respond(
            next_url=target,
            error=f"发送过于频繁，请 {remaining} 秒后再试",
            email=address, invite_code=invite,
            code_sent=True, cooldown=remaining, status_code=400,
        )
    ip = _client_ip(request)
    blocked = crud.email_code_send_blocked_reason(db, address, ip)
    if blocked:
        return respond(
            next_url=target, error=blocked,
            email=address, invite_code=invite, status_code=400,
        )

    code = crud.issue_email_code(db, address, ip=ip)
    delivered, detail = mailer.send_registration_code(address, code)
    if not delivered and mailer.smtp_enabled():
        # 配了邮件服务却发不出去：如实报错，本次不发码（验证码不出现在页面上）。
        return respond(
            next_url=target, error=detail,
            email=address, invite_code=invite, status_code=400,
        )
    if not delivered and not settings.email_code_dev_fallback:
        # 没配邮件服务、又不允许把验证码显示在页面上：注册暂时走不通，
        # 宁可挡住也不泄露验证码（线上在补 SMTP 凭据之前就是这个状态）。
        return respond(
            next_url=target,
            error="当前未配置邮件服务，暂时无法发送验证码，请联系管理员",
            email=address, invite_code=invite, status_code=400,
        )
    return respond(
        next_url=target,
        email=address,
        invite_code=invite,
        code_sent=True,
        notice=detail,
        dev_code="" if delivered else code,
        invite_role=role,
        cooldown=settings.email_resend_cooldown_seconds,
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    db: DbSession,
    username: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    email: str = Form(""),
    code: str = Form(""),
    invite_code: str = Form(""),
    next_url: str = Form("/", alias="next"),
):
    """注册账号（第二步：校验验证码 + 设置密码 + 建号）。

    - 角色由邀请码决定：填了**管理员邀请码**即注册成管理员，普通邀请码 / 不填
      则注册成普通用户；前端传的任何 role 值一律不信，只认数据库里邀请码记录的 role；
    - **邮箱验证码在服务端校验**：验证码与邮箱绑定、过期 / 输错超限 / 已用过
      一律拒绝，校验通过立即失效；只有这一步过了才建号，因此新账号建好即
      「邮箱已验证」，可以直接登录（不再有待验证中间态）；
    - **邀请码可选**：填了才校验并原子消费（条件 UPDATE 兜并发，失败删号回显）；
      留空也能注册（普通用户）；
    - 用户名/邮箱被占用、参数不合法一律回显原因（400），不抛异常。
    """
    target = safe_next_url(next_url)
    invite = (invite_code or "").strip()
    if not settings.email_verification_enabled:
        return _register_without_verification(
            request, db, username=username, password=password, confirm=confirm,
            email=email, invite=invite, next_url=target,
        )

    address, mail_error = validate_email(email)
    if mail_error:
        return _register_page(
            request, next_url=target, error=mail_error,
            email=email, invite_code=invite, status_code=400,
        )
    if crud.get_user_by_email(db, address) is not None:
        return _register_page(
            request, next_url=target, error=f"邮箱已被注册：{address}",
            email=address, invite_code=invite, status_code=400,
        )
    invite_error = ""
    role, invite_error = resolve_registration_role(db, invite)
    if invite_error:
        return _register_page(
            request, next_url=target, error=invite_error,
            email=address, invite_code=invite, status_code=400,
        )
    # 用户名 / 密码规则先查（纯格式校验，不碰库），避免手滑打错就白白烧掉验证码。
    name, error = validate_registration(username, password, confirm, address)
    if error:
        return _register_page(
            request, next_url=target, error=error, username=name,
            email=address, invite_code=invite, code_sent=True, status_code=400,
        )

    # 服务端校验验证码：通过即消费（同一枚码不能完成第二次注册）。
    verified, code_error = crud.verify_email_code(db, address, code)
    if not verified:
        return _register_page(
            request, next_url=target, error=code_error, username=name,
            email=address, invite_code=invite, code_sent=True, status_code=400,
        )

    try:
        # 邮箱已由验证码证明可用，建号即已验证（require_verification=False）。
        # role 来自邀请码记录：普通邀请码 / 无码 -> 普通用户；管理员邀请码 -> 管理员。
        account = crud.create_user(
            db,
            username=name,
            password=password,
            role=role,
            email=address,
            require_verification=False,
        )
    except IntegrityError:
        # 并发注册同一用户名/邮箱时由唯一索引拦截；回滚后区分提示，保持会话可用。
        db.rollback()
        if crud.get_user_by_email(db, address) is not None:
            message = f"邮箱已被注册：{address}"
        else:
            message = f"用户名已被注册：{name}"
        return _register_page(
            request, next_url=target, error=message, username=name,
            email=address, invite_code=invite, code_sent=True, status_code=400,
        )
    except ValueError as exc:  # 数据层校验（空用户名 / 非法角色 / 重名 / 邮箱重名）
        return _register_page(
            request, next_url=target, error=str(exc), username=name,
            email=address, invite_code=invite, code_sent=True, status_code=400,
        )
    except Exception as exc:
        # 与项目其它管理操作一致：意外错误也只回显原因，不抛 500、不影响其它功能。
        return _register_page(
            request, next_url=target, error=f"注册失败：{exc}", username=name,
            email=address, invite_code=invite, code_sent=True, status_code=400,
        )

    # 邮箱已经过验证码证明，补上验证时间（建号时不知道「何时验证」，这里补齐）。
    crud.mark_email_verified(db, account)
    consume_error = _consume_optional_invite(db, invite, account)
    if consume_error:
        crud.delete_user(db, account)
        return _register_page(
            request, next_url=target, error=consume_error, username=name,
            email=address, invite_code=invite, code_sent=True, status_code=400,
        )
    return _session_redirect(target, account)


def _register_without_verification(
    request: Request,
    db: Session,
    *,
    username: str,
    password: str,
    confirm: str,
    email: str,
    invite: str,
    next_url: str,
) -> HTMLResponse:
    """邮箱验证总开关关闭时的旧行为：填表即建号并登录（不发验证码）。"""
    address = crud.normalize_email(email)
    name, error = validate_registration(username, password, confirm)
    if error:
        return _register_page(
            request, next_url=next_url, error=error, username=name,
            email=email, invite_code=invite, status_code=400,
        )
    role, invite_error = resolve_registration_role(db, invite)
    if invite_error:
        return _register_page(
            request, next_url=next_url, error=invite_error, username=name,
            email=email, invite_code=invite, status_code=400,
        )
    try:
        account = crud.create_user(
            db, username=name, password=password,
            role=role, email=address,
        )
    except (IntegrityError, ValueError) as exc:
        db.rollback()
        message = str(exc) if isinstance(exc, ValueError) else f"用户名已被注册：{name}"
        return _register_page(
            request, next_url=next_url, error=message, username=name,
            email=email, invite_code=invite, status_code=400,
        )
    consume_error = _consume_optional_invite(db, invite, account)
    if consume_error:
        crud.delete_user(db, account)
        return _register_page(
            request, next_url=next_url, error=consume_error, username=name,
            email=email, invite_code=invite, status_code=400,
        )
    return _session_redirect(next_url, account)


@router.get("/verify/pending", response_class=HTMLResponse)
def verify_pending_page(
    request: Request,
    db: DbSession,
    sent: int = Query(default=0),
    resent: int = Query(default=0),
    next_url: str = Query(default="/", alias="next"),
):
    """待验证页：显示待验证邮箱、发送结果与重发入口。

    未配置 SMTP 时页面上可直接生成验证链接（站内兜底），
    配置 SMTP 后只提示查收邮件，避免在页面上泄露验证链接。
    """
    target = safe_next_url(next_url)
    if not settings.email_verification_enabled:
        return RedirectResponse("/", status_code=303)
    account = _pending_user(request, db)
    return _render_pending(
        request, db, account, next_url=target, sent=bool(sent), resent=bool(resent)
    )


@router.get("/verify/resend", response_class=HTMLResponse)
def verify_resend_page(
    request: Request,
    db: DbSession,
    next_url: str = Query(default="/", alias="next"),
):
    """重发验证邮件页：Cookie 失效（换浏览器/清缓存）时按邮箱找回待验证账号。"""
    target = safe_next_url(next_url)
    if not settings.email_verification_enabled:
        return RedirectResponse("/", status_code=303)
    return _render_pending(request, db, _pending_user(request, db), next_url=target)


@router.post("/verify/resend", response_class=HTMLResponse)
def verify_resend(
    request: Request,
    db: DbSession,
    email: str = Form(default=""),
    next_url: str = Form(default="/", alias="next"),
):
    """重新发送验证邮件（带冷却，防邮箱轰炸）。

    优先使用待验证 Cookie 里的账号；没有 Cookie 时按提交的邮箱查找待验证账号。
    找不到时给出统一提示，不泄露该邮箱是否已注册。
    """
    target = safe_next_url(next_url)
    if not settings.email_verification_enabled:
        return RedirectResponse("/", status_code=303)

    account = _pending_user(request, db)
    if account is None:
        address = crud.normalize_email(email)
        candidate = crud.get_user_by_email(db, address) if address else None
        if candidate is None or not candidate.email or candidate.email_verified:
            return _render_pending(
                request,
                db,
                None,
                next_url=target,
                resent=True,
                error="未找到待验证的账号，请确认邮箱是否正确，或重新注册。",
                status_code=400,
            )
        account = candidate

    remaining = _resend_remaining_seconds(db, account)
    if remaining > 0:
        return _render_pending(
            request,
            db,
            account,
            next_url=target,
            resent=True,
            error=f"发送过于频繁，请 {remaining} 秒后再试。",
            status_code=400,
        )

    delivered, _detail = _send_verification(request, db, account)
    response = RedirectResponse(
        f"/verify/pending?sent={1 if delivered else 0}"
        f"&resent=1&next={quote(target, safe='/')}",
        status_code=303,
    )
    _set_pending_cookie(response, account.id)
    return response


@router.post("/verify/link", response_class=HTMLResponse)
def verify_link(
    request: Request,
    db: DbSession,
    next_url: str = Form(default="/", alias="next"),
):
    """在未配置 SMTP 时生成一次性的站内验证链接（本地/无邮件服务的兜底）。

    配置 SMTP 后本路由不可用——链接只通过邮件发送，避免站内泄露。
    每次生成都会让该账号此前的链接失效。
    """
    target = safe_next_url(next_url)
    if not settings.email_verification_enabled or mailer.smtp_enabled():
        return RedirectResponse(
            f"/verify/pending?next={quote(target, safe='/')}", status_code=303
    )
    account = _pending_user(request, db)
    if account is None:
        return RedirectResponse(
            f"/verify/resend?next={quote(target, safe='/')}", status_code=303
        )
    token = crud.issue_email_token(db, account)
    return _render_pending(
        request,
        db,
        account,
        next_url=target,
        verify_url=_action_url(request, token),
    )


@router.get("/verify/email", response_class=HTMLResponse)
def verify_email(
    request: Request,
    db: DbSession,
    token: str = Query(default=""),
    next_url: str = Query(default="/", alias="next"),
):
    """验证邮箱：令牌有效则标记账号已验证并清除待验证 Cookie。"""
    target = safe_next_url(next_url)
    if not settings.email_verification_enabled:
        return RedirectResponse("/", status_code=303)
    account = crud.consume_email_token(db, token)
    response = templates.TemplateResponse(
        request=request,
        name="verify_result.html",
        context={
            "next": target,
            "success": account is not None,
            "username": account.username if account is not None else "",
        },
        status_code=200 if account is not None else 400,
    )
    if account is not None:
        response.delete_cookie(PENDING_COOKIE, path="/")
    return response


# ---------- 找回密码 ----------
# 流程与邮箱验证同构：签发一次性令牌 -> 发信（或站内兜底）-> 点链接改密。
# 差异在于：找回密码不检查「是否已验证邮箱」，因为忘密码的用户正卡在这一步；
# 改密成功反而顺带把邮箱标记为已验证（能收到邮件即证明邮箱可控）。


def _forgot_page(request: Request, *, error: str = "", email: str = "",
                 status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={"error": error, "email": email},
        status_code=status_code,
    )


def _reset_form_page(request: Request, *, token: str = "", error: str = "",
                     username: str = "", status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "token": token,
            "error": error,
            "username": username,
            "min_password_length": MIN_PASSWORD_LENGTH,
            "max_password_length": MAX_PASSWORD_LENGTH,
        },
        status_code=status_code,
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(
    request: Request,
    db: DbSession,
    next_url: str = Query(default="/", alias="next"),
):
    """找回密码申请页；已登录用户直接回到目标页面。"""
    target = safe_next_url(next_url)
    if get_current_user(request, db) is not None:
        return RedirectResponse(target, status_code=303)
    return _forgot_page(request)


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    db: DbSession,
    email: str = Form(""),
    next_url: str = Form("/", alias="next"),
):
    """按邮箱申请重置链接。

    - **未注册的邮箱返回与成功完全一致的提示**，避免被用来枚举已注册账号；
    - 无邮箱的历史账号（脚本创建的 admin）同样走「统一提示」：没有投递目标，
      签发令牌没有意义，这类账号只能由管理员在库里改密；
    - 只有真正命中且带邮箱的账号才签发令牌；未配置 SMTP 时链接在提示页直接给出。
    """
    target = safe_next_url(next_url)
    _address, error = validate_email(email)
    if error:
        return _forgot_page(request, error=error, email=email, status_code=400)

    account = crud.get_user_by_email(db, _address)
    if account is None or not account.email:
        return _render_pending(request, db, None, next_url=target, kind="reset", sent=True)

    delivered, _detail = _send_reset(request, db, account)
    response = RedirectResponse(
        f"/reset/pending?sent={1 if delivered else 0}&next={quote(target, safe='/')}",
        status_code=303,
    )
    _set_pending_cookie(response, account.id)
    return response


@router.get("/reset/pending", response_class=HTMLResponse)
def reset_pending_page(
    request: Request,
    db: DbSession,
    sent: int = Query(default=0),
    resent: int = Query(default=0),
    next_url: str = Query(default="/", alias="next"),
):
    """「重置链接已发出」提示页；未配置 SMTP 时可在此生成站内链接。"""
    return _render_pending(
        request,
        db,
        _pending_user(request, db, kind="reset"),
        next_url=safe_next_url(next_url),
        kind="reset",
        sent=bool(sent),
        resent=bool(resent),
    )


@router.post("/reset/resend", response_class=HTMLResponse)
def reset_resend(
    request: Request,
    db: DbSession,
    email: str = Form(default=""),
    next_url: str = Form(default="/", alias="next"),
):
    """重新发送重置邮件（带冷却）；未命中邮箱时提示与成功一致，不泄露注册状态。"""
    target = safe_next_url(next_url)
    account = _pending_user(request, db, kind="reset")
    if account is None:
        address = crud.normalize_email(email)
        candidate = crud.get_user_by_email(db, address) if address else None
        if candidate is None or not candidate.email:
            return _render_pending(
                request, db, None, next_url=target, kind="reset", resent=True, sent=True
            )
        account = candidate

    remaining = _resend_remaining_seconds(db, account, "reset")
    if remaining > 0:
        return _render_pending(
            request,
            db,
            account,
            next_url=target,
            kind="reset",
            resent=True,
            sent=True,
            error=f"发送过于频繁，请 {remaining} 秒后再试。",
            status_code=400,
        )

    delivered, _detail = _send_reset(request, db, account)
    response = RedirectResponse(
        f"/reset/pending?sent={1 if delivered else 0}"
        f"&resent=1&next={quote(target, safe='/')}",
        status_code=303,
    )
    _set_pending_cookie(response, account.id)
    return response


@router.post("/reset/link", response_class=HTMLResponse)
def reset_link(
    request: Request,
    db: DbSession,
    next_url: str = Form(default="/", alias="next"),
):
    """未配置 SMTP 时生成一次性的站内重置链接（本地/无邮件服务的兜底）。

    配置 SMTP 后本路由不可用——链接只通过邮件发送。
    每次生成都会让该账号此前的链接失效。
    """
    target = safe_next_url(next_url)
    if mailer.smtp_enabled():
        return RedirectResponse(
            f"/reset/pending?next={quote(target, safe='/')}", status_code=303
    )
    account = _pending_user(request, db, kind="reset")
    if account is None:
        return RedirectResponse(
            f"/forgot-password?next={quote(target, safe='/')}", status_code=303
    )
    token = crud.issue_password_reset_token(db, account)
    return _render_pending(
        request,
        db,
        account,
        next_url=target,
        kind="reset",
        verify_url=_action_url(request, token, "/reset-password"),
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request,
    db: DbSession,
    token: str = Query(default=""),
    next_url: str = Query(default="/", alias="next"),
):
    """重置密码表单：先校验令牌，无效时给出重新申请入口。"""
    account = crud.verify_password_reset_token(db, token)
    if account is None:
        return _reset_form_page(
            request,
            error="重置链接无效或已过期，请重新申请。",
            status_code=400,
        )
    return _reset_form_page(request, token=token, username=account.username)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    db: DbSession,
    token: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    next_url: str = Form("/", alias="next"),
):
    """校验令牌并改密：成功后旧会话全部失效，跳登录页提示用新密码登录。"""
    target = safe_next_url(next_url)
    account = crud.verify_password_reset_token(db, token)
    if account is None:
        return _reset_form_page(
            request,
            error="重置链接无效或已过期，请重新申请。",
            status_code=400,
        )
    error = validate_password(password, confirm)
    if error:
        return _reset_form_page(
            request, token=token, error=error, status_code=400
    )
    if not crud.set_user_password(db, account, password, token):
        return _reset_form_page(
            request,
            error="重置链接无效或已过期，请重新申请。",
            status_code=400,
        )
    if account.email and not account.email_verified:
        crud.mark_email_verified(db, account)
    response = RedirectResponse(
        f"/login?reset=1&next={quote(target, safe='/')}", status_code=303
    )
    response.delete_cookie(PENDING_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def login_required_handler(request: Request, exc: LoginRequired) -> RedirectResponse:
    """未登录访问受保护页面 -> 跳转登录页并记录返回地址。"""
    return RedirectResponse(
        f"/login?next={quote(exc.next_url, safe='/')}", status_code=303
    )


def permission_denied_handler(request: Request, exc: PermissionDenied) -> HTMLResponse:
    """已登录但无权访问管理区域 -> 渲染 403 页面。

    文案按所属界面域区分：测试平台页面说「测试管理平台」，内容管理 / 站点设置
    说「内容管理」——否则内容管理的越权提示会把人指向另一个产品域。
    """
    path = request.url.path
    test_area = path.startswith(("/testcases", "/runs", "/batches"))
    return templates.TemplateResponse(
        request=request,
        name="403.html",
        context={"test_area": test_area},
        status_code=403,
    )
