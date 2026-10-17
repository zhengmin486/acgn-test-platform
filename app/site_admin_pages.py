"""站点设置页面路由（内容平台 → 站点设置，/admin/*）。

管「人」与「准入」：账号邮箱验证的人工兜底、注册邀请码、角色调整。
既不属于内容数据运营（/manage），也不属于测试工具（/testcases 等），故单独成模块。
角色管理仅站长可用：路由级 require_owner_page 叠加在 main.py 的 require_admin_page 之上。
"""

from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import crud, mailer
from app.admin_common import actor_id as _actor_id, short_text as _short, url_with as _url
from app.auth import require_owner_page
from app.config import settings
from app.page_common import DbSession, templates

router = APIRouter(tags=["site-admin"])


# ---------- 账号验证（邮箱验证的人工兜底入口） ----------


def _render_accounts_page(
    request: Request,
    db,
    *,
    verified: str = "",
    error: str = "",
    link: str = "",
    link_user: str = "",
):
    return templates.TemplateResponse(
        request=request,
        name="admin_accounts.html",
        context={
            "pending_users": crud.list_unverified_users(db),
            "verified": verified,
            "error": error,
            "link": link,
            "link_user": link_user,
            "smtp_enabled": mailer.smtp_enabled(),
            "email_verification_enabled": settings.email_verification_enabled,
        },
    )


@router.get("/admin/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    db: DbSession,
    verified: str = Query(default=""),
    error: str = Query(default=""),
    link: str = Query(default=""),
    link_user: str = Query(default=""),
):
    """待验证账号列表：管理员可直接放行，或在未配置邮件服务时取得验证链接。"""
    return _render_accounts_page(
        request, db, verified=verified, error=error, link=link, link_user=link_user
    )


@router.post("/admin/accounts/{user_id}/verify")
def verify_account(request: Request, db: DbSession, user_id: int):
    """管理员手动标记账号为已验证（PRG：303 回列表并回显用户名）。"""
    account = crud.get_user(db, user_id)
    if account is None:
        return RedirectResponse(
            _url("/admin/accounts", error=f"账号不存在：{user_id}"), status_code=303
        )
    crud.mark_email_verified(db, account)
    return RedirectResponse(
        _url("/admin/accounts", verified=account.username), status_code=303
    )


@router.post("/admin/accounts/{user_id}/link")
def account_verification_link(request: Request, db: DbSession, user_id: int):
    """为指定账号生成一次性验证链接，供管理员转交用户。

    与面向普通用户的 /verify/link 不同，这里**不要求「未配置 SMTP」**：
    邮件服务故障（配置错误、被拒收）时，管理员仍需要一条可转交的链接兜底。
    管理员本身就能直接放行账号，因此不存在额外越权风险。
    每次生成都会让该账号此前的链接失效。
    """
    if not settings.email_verification_enabled:
        return RedirectResponse("/admin/accounts", status_code=303)
    account = crud.get_user(db, user_id)
    if account is None or not account.email:
        return RedirectResponse(
            _url("/admin/accounts", error=f"账号不存在或无邮箱：{user_id}"), status_code=303
        )
    token = crud.issue_email_token(db, account)
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    verify_url = f"{base}/verify/email?token={quote(token)}"
    return RedirectResponse(
        _url("/admin/accounts", link=verify_url, link_user=account.username),
        status_code=303,
    )


# ---------- 注册邀请码（公开注册关闭后的准入控制） ----------
#
# 公开注册关闭（REGISTRATION_MODE=invite，默认）后，/register 必须提供有效
# 邀请码。这里负责生成 / 列表 / 作废；实际消费在注册流程（crud.consume_invite_code）。
# 邀请码明文存储：管理员需要在本页看到并转交，与密码、令牌的保密语义不同。


def _invite_expires_at(days_text: str) -> tuple[datetime | None, str]:
    """解析有效期天数：空 / 0 = 永不过期；返回 ``(过期时间, 错误信息)``。"""
    raw = (days_text or "").strip()
    if not raw or raw == "0":
        return None, ""
    if not raw.isdigit():
        return None, "有效期天数必须是 0（永不过期）或正整数"
    days = int(raw)
    if days > 3650:
        return None, "有效期天数不能超过 3650（10 年）"
    return datetime.now() + timedelta(days=days), ""


def _render_invites_page(
    request: Request,
    db,
    *,
    generated: int = 0,
    revoked: str = "",
    error: str = "",
):
    """渲染邀请码管理页：生成表单 + 全量列表（含状态与使用痕迹）。"""
    codes = [(code, crud.invite_code_status(code)) for code in crud.list_invite_codes(db)]
    # 使用者 / 签发人用户名映射：账号删除后回退显示 id，记录仍可解读。
    usernames = {user.id: user.username for user in crud.list_users(db)}
    return templates.TemplateResponse(
        request=request,
        name="admin_invites.html",
        context={
            "codes": codes,
            "status_labels": crud.INVITE_STATUS_LABELS,
            "usernames": usernames,
            "invite_required": settings.registration_invite_only,
            "registration_mode": settings.registration_mode,
            "max_batch": crud.INVITE_CODE_MAX_BATCH,
            "generated": generated,
            "revoked": revoked,
            "error": error,
        },
    )


@router.get("/admin/invites", response_class=HTMLResponse)
def invites_page(
    request: Request,
    db: DbSession,
    generated: int = Query(default=0),
    revoked: str = Query(default=""),
    error: str = Query(default=""),
):
    """邀请码管理：生成 / 查看 / 作废注册邀请码。"""
    return _render_invites_page(
        request, db, generated=generated, revoked=revoked, error=_short(error) if error else ""
    )


@router.post("/admin/invites/generate")
async def generate_invites_page(request: Request, db: DbSession):
    """批量生成邀请码；PRG 回列表页，新码出现在列表最上方（明文展示）。"""
    form = await request.form()
    count_text = str(form.get("count", "1")).strip() or "1"
    if not count_text.isdigit() or not (1 <= int(count_text) <= crud.INVITE_CODE_MAX_BATCH):
        return RedirectResponse(
            _url("/admin/invites", error=f"生成数量需为 1-{crud.INVITE_CODE_MAX_BATCH} 的整数"),
            status_code=303,
        )
    expires_at, error = _invite_expires_at(str(form.get("days", "")))
    if error:
        return RedirectResponse(_url("/admin/invites", error=error), status_code=303)
    note = str(form.get("note", "")).strip()
    try:
        codes = crud.create_invite_codes(
            db,
            count=int(count_text),
            expires_at=expires_at,
            note=note,
            created_by=_actor_id(request),
        )
    except (ValueError, RuntimeError) as exc:
        return RedirectResponse(_url("/admin/invites", error=str(exc)), status_code=303)
    return RedirectResponse(
        _url("/admin/invites", generated=len(codes)), status_code=303
    )


@router.post("/admin/invites/{invite_id}/revoke")
def revoke_invite_page(request: Request, db: DbSession, invite_id: int):
    """作废邀请码（仅未使用的可作废）；结果 PRG 回显。"""
    ok, error = crud.revoke_invite_code(db, invite_id)
    return RedirectResponse(
        _url(
            "/admin/invites",
            revoked="1" if ok else "",
            error="" if ok else error,
        ),
        status_code=303,
    )


# ---------- 角色管理（仅站长） ----------


@router.get(
    "/admin/roles",
    response_class=HTMLResponse,
    dependencies=[Depends(require_owner_page)],
)
def roles_page(
    request: Request,
    db: DbSession,
    changed: str = Query(default=""),
    to: str = Query(default=""),
    error: str = Query(default=""),
):
    """角色管理（仅站长）：列出全部账号并调整其角色。"""
    current_user = getattr(request.state, "current_user", None)
    return templates.TemplateResponse(
        request=request,
        name="admin_roles.html",
        context={
            "users": crud.list_users(db),
            "role_labels": crud.ROLE_LABELS,
            "assignable_roles": (crud.ROLE_USER, crud.ROLE_ADMIN, crud.ROLE_OWNER),
            "current_user_id": current_user.id if current_user else 0,
            "changed": changed,
            "to": to,
            "error": error,
        },
    )


@router.post(
    "/admin/roles/{user_id}",
    dependencies=[Depends(require_owner_page)],
)
async def change_user_role(request: Request, db: DbSession, user_id: int):
    """调整账号角色（仅站长），PRG 回角色管理页回显结果。

    两条自我保护规则：不能改自己的角色（避免误操作导致自己降权）；
    不能把最后一个站长降级（否则站点失去最高权限账号）。
    """
    form = await request.form()
    role = str(form.get("role", "")).strip().lower()
    current_user = getattr(request.state, "current_user", None)
    account = crud.get_user(db, user_id)
    if account is None:
        return RedirectResponse(
            _url("/admin/roles", error=f"账号不存在：{user_id}"), status_code=303
        )
    if current_user is not None and account.id == current_user.id:
        return RedirectResponse(
            _url("/admin/roles", error="不能修改自己的角色"), status_code=303
        )
    if account.role == crud.ROLE_OWNER and role != crud.ROLE_OWNER:
        if crud.count_owners(db) <= 1:
            return RedirectResponse(
                _url("/admin/roles", error="至少需要保留一个站长账号"), status_code=303
            )
    try:
        crud.set_user_role(db, account, role)
    except ValueError as exc:
        return RedirectResponse(_url("/admin/roles", error=str(exc)), status_code=303)
    return RedirectResponse(
        _url("/admin/roles", changed=account.username, to=role), status_code=303
    )


