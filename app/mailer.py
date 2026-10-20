"""邮件发送（可插拔）：只依赖标准库 smtplib / email。

设计要点：
- **未配置 SMTP 时不真发邮件**，只把验证链接写入日志并返回 ``(False, 原因)``，
  由「待验证页面」与「管理员 → 账号验证」提供站内链接兜底；
  这样在没有服务器、没有域名、没有邮件服务的本地环境里，验证流程依然能完整走通。
- 配置好 ``SMTP_HOST`` / ``SMTP_FROM_ADDRESS``（以及账号授权码）后即自动切换为真实发信，
  业务代码无需任何改动——这就是「可插拔」的含义。
- 发信失败只记录日志并返回原因，**不抛 500**，与管理端其它操作的错误处理风格保持一致。
"""

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from app.config import settings

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 10

VERIFICATION_SUBJECT = "验证你的 Otaku 账号邮箱"
RESET_SUBJECT = "重置你的 Otaku 账号密码"
EMAIL_CHANGE_SUBJECT = "确认更换 Otaku 账号邮箱"


class EmailDeliveryError(RuntimeError):
    """邮件发送失败（已配置 SMTP 但投递出错）。"""


def smtp_enabled() -> bool:
    """是否配置了可用的 SMTP（缺主机或缺发件人地址都视为未配置）。"""
    return bool(settings.smtp_host and settings.smtp_from_address)


def sender_address() -> str:
    """发件人地址；未单独配置发件人时退回 SMTP 账号。"""
    return settings.smtp_from_address or settings.smtp_username or "no-reply@localhost"


def _ttl_text(seconds: int) -> str:
    """把有效期秒数渲染成人类可读文案（整小时用「小时」，否则用「分钟」）。"""
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    return f"{max(1, seconds // 60)} 分钟"


def _build_message(
    subject: str,
    to_address: str,
    username: str,
    action_url: str,
    hint: str,
    ttl_seconds: int,
) -> EmailMessage:
    """构造「一次性链接」邮件：纯文本 + HTML 双版本，主题与正文由调用方决定。"""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.smtp_from_name, sender_address()))
    message["To"] = to_address
    ttl = _ttl_text(ttl_seconds)
    text_body = (
        f"{username} 你好：\n\n"
        f"{hint}\n\n"
        f"{action_url}\n\n"
        f"链接有效期为 {ttl}，重新发送邮件后旧链接会失效。\n"
        "如果这不是你本人的操作，请忽略本邮件。\n"
    )
    html_body = (
        "<p>{name} 你好：</p>"
        "<p>{hint}</p>"
        '<p><a href="{url}">{url}</a></p>'
        "<p>链接有效期为 {ttl}，重新发送邮件后旧链接会失效。</p>"
        "<p>如果这不是你本人的操作，请忽略本邮件。</p>"
    ).format(name=username, hint=hint, url=action_url, ttl=ttl)
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _deliver(
    message: EmailMessage,
    to_address: str,
    action_url: str,
    fallback_hint: str,
) -> tuple[bool, str]:
    """投递邮件，返回 ``(是否真实发出, 失败原因)``；成功时原因为空串。

    - 未配置 SMTP：把链接写进日志并返回降级提示，由页面兜底展示；
    - 已配置但投递失败：记录异常，返回原因（不抛 500）。
    """
    if not smtp_enabled():
        logger.info("未配置 SMTP，跳过真实发信；一次性链接：%s", action_url)
        return False, fallback_hint

    try:
        if settings.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            )
        else:
            server = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            )
        with server:
            server.ehlo()
            if settings.smtp_use_tls and not settings.smtp_use_ssl:
                server.starttls()
                server.ehlo()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except Exception as exc:  # 与项目其它管理操作一致：失败只回显，不抛 500
        logger.exception("邮件发送失败：%s", exc)
        return False, f"邮件发送失败：{exc}"
    return True, ""


def build_verification_email(
    to_address: str,
    username: str,
    verify_url: str,
) -> EmailMessage:
    """构造邮箱验证邮件；纯文本 + HTML 双版本，兼容各类邮件客户端。"""
    return _build_message(
        VERIFICATION_SUBJECT,
        to_address,
        username,
        verify_url,
        "感谢注册 Otaku（ACGN 内容平台）。"
        "请打开下面的链接完成邮箱验证，验证后即可登录：",
        settings.email_verification_token_ttl_seconds,
    )


def send_verification_email(
    to_address: str,
    username: str,
    verify_url: str,
) -> tuple[bool, str]:
    """发送验证邮件，返回 ``(是否真实发出, 说明)``。

    - 未配置 SMTP：写日志并返回 ``(False, "…未配置…")``；
    - 已配置但投递失败：记录异常并返回 ``(False, 失败原因)``；
    - 发送成功：返回 ``(True, "验证邮件已发送至 …")``。
    """
    message = build_verification_email(to_address, username, verify_url)
    ok, error = _deliver(
        message,
        to_address,
        verify_url,
        "当前未配置邮件服务，可使用页面上的验证链接完成验证",
    )
    return ok, (f"验证邮件已发送至 {to_address}" if ok else error)


def build_password_reset_email(
    to_address: str,
    username: str,
    reset_url: str,
) -> EmailMessage:
    """构造找回密码邮件；与验证邮件同构，但有效期更短。"""
    return _build_message(
        RESET_SUBJECT,
        to_address,
        username,
        reset_url,
        "我们收到了重置你 Otaku 账号密码的请求。"
        "请打开下面的链接设置新密码；如果你没有发起该请求，忽略本邮件即可。",
        settings.password_reset_token_ttl_seconds,
    )


def send_password_reset_email(
    to_address: str,
    username: str,
    reset_url: str,
) -> tuple[bool, str]:
    """发送找回密码邮件，返回 ``(是否真实发出, 说明)``；语义同 ``send_verification_email``。"""
    message = build_password_reset_email(to_address, username, reset_url)
    ok, error = _deliver(
        message,
        to_address,
        reset_url,
        "当前未配置邮件服务，可使用页面上的重置链接继续操作",
    )
    return ok, (f"重置密码邮件已发送至 {to_address}" if ok else error)


def build_email_change_email(
    to_address: str,
    username: str,
    confirm_url: str,
) -> EmailMessage:
    """构造「更换绑定邮箱」确认邮件；发给**新地址**，证明该地址确实可用。"""
    return _build_message(
        EMAIL_CHANGE_SUBJECT,
        to_address,
        username,
        confirm_url,
        "我们收到了把你的 Otaku 账号邮箱更换为当前地址的请求。"
        "请打开下面的链接完成确认；如果你没有发起该请求，忽略本邮件即可（原邮箱不受影响）。",
        settings.email_verification_token_ttl_seconds,
    )


def send_email_change_email(
    to_address: str,
    username: str,
    confirm_url: str,
) -> tuple[bool, str]:
    """发送换绑确认邮件，返回 ``(是否真实发出, 说明)``；语义同 ``send_verification_email``。"""
    message = build_email_change_email(to_address, username, confirm_url)
    ok, error = _deliver(
        message,
        to_address,
        confirm_url,
        "当前未配置邮件服务，可使用设置页上的确认按钮完成换绑",
    )
    return ok, (f"确认邮件已发送至 {to_address}" if ok else error)
