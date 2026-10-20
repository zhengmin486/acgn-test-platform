"""账号设置页路由：个人资料（/settings/profile）+ 账号与安全（/settings/security）。

为什么单独成页，而不塞进个人空间（/me）：
- 个人空间回答「我收藏了什么、看了什么」，是可浏览的内容页；资料与绑定属于
  「设置」，频率低、字段少、且一半是敏感操作（解绑 = 拆掉找回通道）；
- 主流站点（B站 / 抖音 / 微博）都是「头像菜单 → 账号设置」，设置页内部再分
  「个人资料 / 账号与安全」两栏，本模块照这个结构落，入口在 base_content.html
  的头像下拉菜单里。

边界与约定：
- 全模块只要求登录（``require_login_page``），普通用户与管理员一视同仁；
  所有写操作都只作用于 ``_current_account`` 解析出的本人账号，URL 里没有 user_id，
  从结构上就不存在改别人资料的入口；
- 写操作统一 PRG：303 回设置页并用 ``?msg=<键>`` 回显结果，文案集中在
  ``PROFILE_MESSAGES`` / ``SECURITY_MESSAGES``，模板只负责渲染对应级别的 alert；
- **邮箱脱敏**：页面只显示 ``a***@domain``，完整地址不外露（用户明确要求邮箱
  属于隐私信息）；手机同理，只显示 ``138****8000``。

绑定 / 解绑的安全模型（两条通道一致：「证明新凭据归你」才绑，「证明旧凭据归你」才解）：
- 绑 / 换邮箱：把确认链接发到**新地址**，点开才算绑定；发起时要求输入当前密码，
  防止「拿到一个未锁屏的会话就能把邮箱换成自己的、再走找回密码接管账号」。
- 解绑邮箱：同样要求当前密码。解绑后仍可用用户名 + 密码登录，只是不能再用邮箱找回密码。
- 绑 / 换手机：给**新号码**发 6 位验证码，输对才算绑定。
- 解绑手机：给**当前绑定号码**发验证码，输对才解绑（证明手机仍在你手上）。
- 两条通道都不改密码、不动会话；数据库层的唯一索引保证「一个邮箱 / 手机只挂一个账号」。
"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import crud, mailer, sms
from app.auth import LoginRequired, require_login_page, validate_email
from app.config import settings
from app.page_common import DbSession, templates
from app.security import verify_password

# 路由级守卫：只要求登录（与个人空间同一条守卫）。
router = APIRouter(tags=["pages"], dependencies=[Depends(require_login_page)])

# 账号与安全页可展开的操作面板（白名单）：换邮箱 / 解绑邮箱 / 换手机 / 解绑手机。
# 用 ``?panel=`` 而不是 ``<details>``，是为了 PRG 之后能回到同一个面板——
# 校验失败重定向回列表页时，用户不用再点一次「更换」才看到表单。
SECURITY_PANELS = ("email-change", "email-unbind", "phone-bind", "phone-unbind")

# 文案表：键写进 URL（``?msg=email_bound``），值决定 alert 级别与提示语。
# 集中在这里而不是散在模板 if/elif 里，既方便对齐措辞，也保证「每个跳转都带得出话」。
PROFILE_MESSAGES: dict[str, tuple[str, str]] = {
    "saved": ("success", "个人资料已保存。"),
    "invalid": (
        "danger",
        "保存失败：请检查生日（不能晚于今天、不能早于 1900 年）与所在地长度。",
    ),
}

SECURITY_MESSAGES: dict[str, tuple[str, str]] = {
    # 邮箱
    "email_sent": ("success", "确认邮件已发送，请到新邮箱点击链接完成绑定。"),
    "email_sent_dev": ("info", "当前未配置邮件服务，可用下方「直接确认」按钮完成绑定。"),
    "email_bound": ("success", "邮箱已更新。"),
    "email_unbound": ("info", "邮箱已解绑。"),
    "email_invalid": ("danger", "邮箱格式不正确或长度超限。"),
    "email_taken": ("danger", "该邮箱已被其他账号使用。"),
    "email_same": ("info", "这个邮箱已经是当前绑定邮箱，无需更换。"),
    "email_cooldown": ("warning", "发送太频繁了，请稍后再试。"),
    "email_pwd": ("danger", "当前密码不正确，邮箱未改动。"),
    "email_link_invalid": ("danger", "确认链接无效或已过期，请重新发起换绑。"),
    "email_none_pending": ("warning", "没有待确认的邮箱绑定。"),
    "email_cancelled": ("info", "已取消本次邮箱换绑。"),
    # 手机
    "phone_code_sent": ("success", "验证码已发送，请查收短信。"),
    "phone_invalid": ("danger", "手机号格式不正确：请填写 11 位大陆手机号。"),
    "phone_taken": ("danger", "该手机号已绑定其他账号。"),
    "phone_bound": ("success", "手机号已绑定。"),
    "phone_unbound": ("info", "手机已解绑。"),
    "phone_code_bad": ("danger", "验证码不正确或已过期，请重新获取。"),
    "phone_cooldown": ("warning", "发送太频繁，请稍后再试。"),
    "phone_mismatch": ("danger", "号码与当前绑定手机不一致。"),
    "phone_none_pending": ("warning", "没有待验证的号码，请重新发起。"),
    "phone_cancelled": ("info", "已取消本次验证。"),
}


def _current_account(request: Request, db: Session):
    """取当前登录账号的 ORM 对象；会话有效但账号已删除时按未登录处理。"""
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise LoginRequired(next_url=request.url.path)
    account = crud.get_user(db, current_user.id)
    if account is None:
        raise LoginRequired(next_url=request.url.path)
    return account


def _flash(messages: dict[str, tuple[str, str]], key: str | None):
    """把 ``?msg=`` 映射成模板用的 ``(级别, 文案)``；未知键忽略（不长篇报错）。"""
    if not key:
        return None
    found = messages.get(key)
    if found is None:
        return None
    return {"level": found[0], "text": found[1]}


def _confirm_url(request: Request, token: str) -> str:
    """换绑确认链接：优先用配置的对外基址，否则取当前请求的 host。"""
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/settings/security/email/confirm?token={token}"


def _render_profile(request: Request, db: Session, account, *, status_code: int = 200):
    """渲染「个人资料」分区：简介 + 性别 / 生日 / 所在地（一个表单一次提交）。"""
    return templates.TemplateResponse(
        request=request,
        name="settings_profile.html",
        context={
            "account": account,
            "active_tab": "profile",
            "flash": _flash(PROFILE_MESSAGES, request.query_params.get("msg")),
            "bio_max_length": crud.MAX_BIO_LENGTH,
            "gender_labels": crud.GENDER_LABELS,
            "location_max_length": crud.MAX_LOCATION_LENGTH,
            "zodiac": crud.zodiac_sign(account.birthday),
            "age": crud.age_from_birthday(account.birthday),
            "today_iso": date.today().isoformat(),
        },
        status_code=status_code,
    )


def _render_security(
    request: Request,
    db: Session,
    account,
    *,
    panel: str = "",
    dev_code: str = "",
    dev_phone: str = "",
    status_code: int = 200,
):
    """渲染「账号与安全」分区：邮箱 / 手机两张卡 + 待验证状态 + 展开中的操作面板。

    ``panel`` 决定哪个操作面板是展开的（换绑 / 解绑），只接受白名单取值，
    避免被塞进任意字符串到模板里。
    ``dev_code`` 只在**未配置短信网关**时由「发送验证码」的 POST 直接渲染回填：
    验证码明文只存在于内存与那一次响应里，不落库、不进 URL。
    """
    pending_email = crud.latest_pending_email_change(db, account)
    pending_phone = crud.pending_phone_code(db, account)
    return templates.TemplateResponse(
        request=request,
        name="settings_security.html",
        context={
            "account": account,
            "active_tab": "security",
            "flash": _flash(SECURITY_MESSAGES, request.query_params.get("msg")),
            "panel": panel if panel in SECURITY_PANELS else "",
            "email_masked": crud.mask_email(account.email),
            "phone_masked": crud.mask_phone(account.phone),
            # 待确认的邮箱换绑（有记录就显示「等待确认 / 取消」）
            "pending_email_masked": (
                crud.mask_email(pending_email.new_email) if pending_email else ""
            ),
            # 待验证的手机号（有记录就显示验证码输入框）
            "pending_phone_masked": (
                crud.mask_phone(pending_phone.phone) if pending_phone else ""
            ),
            "pending_phone_purpose": pending_phone.purpose if pending_phone else "",
            # 「重新发送」按钮的冷却（秒）；0 表示可以立即重发
            "phone_cooldown": crud.phone_code_cooldown_remaining(db, account),
            # 未配置 SMTP / 短信网关时的站内兜底开关（与 auth 的验证链接兜底同思路）
            "mail_configured": mailer.smtp_enabled(),
            "sms_configured": sms.sms_enabled(),
            "dev_code": dev_code,
            "dev_phone": dev_phone,
            "code_ttl_minutes": max(1, settings.sms_code_ttl_seconds // 60),
            "phone_max_length": crud.MAX_PHONE_LENGTH,
        },
        status_code=status_code,
    )


def _security_url(msg: str, panel: str = "") -> str:
    """账号与安全页的 PRG 回跳地址：``msg`` 决定提示语，``panel`` 决定展开哪个面板。"""
    target = f"/settings/security?msg={msg}"
    if panel in SECURITY_PANELS:
        target += f"&panel={panel}"
    return target


@router.get("/settings")
def settings_root():
    """``/settings`` 只是入口：跳到默认分区，避免出现「空壳页」。"""
    return RedirectResponse("/settings/profile", status_code=303)


# ---------- 个人资料 ----------


@router.get("/settings/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: DbSession):
    """个人资料：只读展示 + 表单编辑（性别 / 生日 / 所在地 / 简介）。"""
    account = _current_account(request, db)
    return _render_profile(request, db, account)


@router.post("/settings/profile")
def update_profile(
    request: Request,
    db: DbSession,
    bio: str = Form(default=""),
    gender: str = Form(default=""),
    birthday: str = Form(default=""),
    location: str = Form(default=""),
):
    """保存个人资料：四项一次提交、一次落库，任一非法则整表不写。"""
    account = _current_account(request, db)
    try:
        crud.update_user_profile(
            db, account, bio=bio, gender=gender, birthday=birthday, location=location
        )
    except ValueError:
        return RedirectResponse("/settings/profile?msg=invalid", status_code=303)
    return RedirectResponse("/settings/profile?msg=saved", status_code=303)


# ---------- 账号与安全：邮箱 ----------


@router.get("/settings/security", response_class=HTMLResponse)
def security_page(
    request: Request,
    db: DbSession,
    panel: str = Query(default="", max_length=20),
):
    """账号与安全：邮箱 / 手机的绑定状态与操作入口；``?panel=`` 展开某个操作面板。"""
    account = _current_account(request, db)
    return _render_security(request, db, account, panel=panel)


@router.post("/settings/security/email")
def request_email_bind(
    request: Request,
    db: DbSession,
    email: str = Form(default=""),
    password: str = Form(default=""),
):
    """发起绑定 / 换绑邮箱：先验当前密码，再向**新地址**发确认链接。

    链接点开才真正改库（``/settings/security/email/confirm``），所以这一步对
    账号的唯一副作用是把旧待确认记录作废。
    """
    account = _current_account(request, db)
    address, email_error = validate_email(email)
    if email_error:
        return RedirectResponse(_security_url("email_invalid", "email-change"), status_code=303)
    if not verify_password(password, account.password_hash):
        return RedirectResponse(_security_url("email_pwd", "email-change"), status_code=303)
    if account.email and address == account.email:
        return RedirectResponse(_security_url("email_same", "email-change"), status_code=303)
    occupied = crud.get_user_by_email(db, address)
    if occupied is not None and occupied.id != account.id:
        return RedirectResponse(_security_url("email_taken", "email-change"), status_code=303)
    # 冷却：否则这个端点能拿来反复给任意地址发确认邮件
    if crud.email_change_cooldown_remaining(db, account) > 0:
        return RedirectResponse(_security_url("email_cooldown", "email-change"), status_code=303)

    token = crud.issue_email_change_token(db, account, address)
    delivered, _hint = mailer.send_email_change_email(
        address, account.username, _confirm_url(request, token)
    )
    key = "email_sent" if delivered else "email_sent_dev"
    return RedirectResponse(_security_url(key), status_code=303)


@router.get("/settings/security/email/confirm", response_class=HTMLResponse)
def confirm_email_bind(request: Request, db: DbSession, token: str = Query(default="")):
    """点击邮件里的确认链接：消费令牌并把邮箱改成新地址。"""
    account = _current_account(request, db)
    confirmed = crud.consume_email_change_token(db, token)
    # 令牌可能属于别的账号（用户复制了别人的链接）：只认「确认后还是我自己」。
    if confirmed is None or confirmed.id != account.id:
        return RedirectResponse(_security_url("email_link_invalid"), status_code=303)
    return RedirectResponse(_security_url("email_bound"), status_code=303)


@router.post("/settings/security/email/confirm")
def confirm_email_bind_fallback(request: Request, db: DbSession):
    """站内兜底确认：仅在**未配置 SMTP** 时可用（否则必须走邮件里的链接）。

    与 ``auth.verify_link`` 同一条思路——本地 / 测试环境没有邮件服务，
    否则绑定流程走不完；线上配置了 SMTP 后这条路径自动关闭。
    """
    account = _current_account(request, db)
    if mailer.smtp_enabled():
        return RedirectResponse(_security_url("email_link_invalid"), status_code=303)
    confirmed = crud.confirm_pending_email_change(db, account)
    if confirmed is None:
        return RedirectResponse(_security_url("email_none_pending"), status_code=303)
    return RedirectResponse(_security_url("email_bound"), status_code=303)


@router.post("/settings/security/email/cancel")
def cancel_email_bind(request: Request, db: DbSession):
    """取消待确认的邮箱换绑（作废令牌，邮箱保持原样）。"""
    account = _current_account(request, db)
    record = crud.latest_pending_email_change(db, account)
    if record is not None:
        record.consumed_at = datetime.now()
        db.commit()
    return RedirectResponse(_security_url("email_cancelled"), status_code=303)


@router.post("/settings/security/email/unbind")
def unbind_email(
    request: Request,
    db: DbSession,
    password: str = Form(default=""),
):
    """解绑邮箱：需要当前密码（证明是本人，而不是捡到会话的人）。

    解绑后账号仍可用用户名 + 密码登录，只是失去「邮箱找回密码」这条通道，
    页面上的提示语会明说这一点。
    """
    account = _current_account(request, db)
    if not verify_password(password, account.password_hash):
        return RedirectResponse(_security_url("email_pwd", "email-unbind"), status_code=303)
    if not account.email:
        return RedirectResponse(_security_url("email_none_pending"), status_code=303)
    crud.clear_user_email(db, account)
    return RedirectResponse(_security_url("email_unbound"), status_code=303)


# ---------- 账号与安全：手机 ----------


@router.post("/settings/security/phone/code")
def send_phone_code(
    request: Request,
    db: DbSession,
    phone: str = Form(default=""),
    purpose: str = Form(default=""),
):
    """发送短信验证码：绑定发到新号码，解绑发到当前绑定号码。

    两种调用方式：
    - 带 ``phone``（面板里的「发送验证码」）：校验号码后发码；
    - 不带 ``phone``（待验证面板里的「重新发送」）：沿用当前那条待验证记录的
      号码与用途，不接受前端另填，避免拿这个端点给任意号码发短信。

    未配置短信网关时**直接渲染页面并把验证码显示出来**（仅本地 / 测试环境：
    否则没有手机收码的开发者一步也走不下去）。这条分支不重定向，验证码因此
    不会进入 URL、浏览历史或 Referer。
    """
    account = _current_account(request, db)
    pending = crud.pending_phone_code(db, account)

    if not (phone or "").strip():
        if pending is None:
            return RedirectResponse(_security_url("phone_none_pending"), status_code=303)
        value, purpose = pending.phone, pending.purpose
    else:
        if purpose not in crud.PHONE_PURPOSES:
            purpose = crud.PHONE_PURPOSE_BIND
        value, phone_error = crud.validate_phone(phone)
        if phone_error:
            return RedirectResponse(_security_url("phone_invalid", "phone-bind"), status_code=303)
        if purpose == crud.PHONE_PURPOSE_UNBIND:
            # 解绑只能对「当前绑定号码」发码
            if value != account.phone:
                return RedirectResponse(_security_url("phone_mismatch", "phone-unbind"), status_code=303)
        else:
            occupied = crud.get_user_by_phone(db, value)
            if occupied is not None and occupied.id != account.id:
                return RedirectResponse(_security_url("phone_taken", "phone-bind"), status_code=303)

    if crud.phone_code_cooldown_remaining(db, account) > 0:
        return RedirectResponse(_security_url("phone_cooldown"), status_code=303)

    code = crud.issue_phone_code(db, account, value, purpose)
    delivered, _hint = sms.send_verification_code(value, code, purpose=purpose)
    if delivered:
        return RedirectResponse(_security_url("phone_code_sent"), status_code=303)
    return _render_security(request, db, account, dev_code=code, dev_phone=crud.mask_phone(value))


@router.post("/settings/security/phone/confirm")
def confirm_phone_code(
    request: Request,
    db: DbSession,
    code: str = Form(default=""),
):
    """校验验证码并完成绑定 / 解绑；码错、过期、超次一律回同一句提示。

    号码与用途都取自服务端的待验证记录，**不读表单**：前端能改的字段就不能
    当成事实，否则「绑定」可能被改成「解绑」。
    """
    account = _current_account(request, db)
    pending = crud.pending_phone_code(db, account)
    if pending is None:
        return RedirectResponse(_security_url("phone_code_bad"), status_code=303)

    value, purpose = pending.phone, pending.purpose
    if not crud.consume_phone_code(db, account, value, code, purpose=purpose):
        return RedirectResponse(_security_url("phone_code_bad"), status_code=303)

    if purpose == crud.PHONE_PURPOSE_UNBIND:
        if value != account.phone:
            return RedirectResponse(_security_url("phone_mismatch"), status_code=303)
        crud.clear_user_phone(db, account)
        return RedirectResponse(_security_url("phone_unbound"), status_code=303)

    # 并发保护：两个账号同时验证同一号码时，后到的那个在这里被拦下
    occupied = crud.get_user_by_phone(db, value)
    if occupied is not None and occupied.id != account.id:
        return RedirectResponse(_security_url("phone_taken"), status_code=303)
    crud.set_user_phone(db, account, value)
    return RedirectResponse(_security_url("phone_bound"), status_code=303)


@router.post("/settings/security/phone/cancel")
def cancel_phone_code(request: Request, db: DbSession):
    """取消本次验证（作废验证码，号码不变）。"""
    account = _current_account(request, db)
    crud.discard_phone_code(db, account)
    return RedirectResponse(_security_url("phone_cancelled"), status_code=303)
