"""短信发送（可插拔）：只依赖标准库 urllib，不引入 SDK。

设计要点与 app/mailer.py 完全一致：
- **未配置网关时不真发短信**，只把验证码写进日志并返回 ``(False, 提示)``，
  由设置页的兜底面板直接展示验证码，本地 / 测试环境照样能走完绑定解绑；
- 配置好 ``SMS_API_URL`` / ``SMS_API_KEY`` 后自动切换为真实发信，
  接入具体服务商时只改 ``_request_body`` 一处，业务代码无需改动；
- 发送失败只记录日志并返回原因，**不抛 500**。

之所以没有绑定某家服务商的 SDK：各家（腾讯云 / 阿里云 / 云片 / 容联…）的
签名算法与请求体差异很大，把「通用 HTTP 网关」这层留在这里，接入时改一处
比现在猜一个用不上的协议更可靠。``_request_body`` 目前给的是最常见的
``{phone, content, sign, code}`` 结构，接阿里云 / 云片时按文档调整即可。
"""

import json
import logging
import urllib.error
import urllib.request

from app.config import settings

logger = logging.getLogger(__name__)

SMS_TIMEOUT_SECONDS = 8

# 验证码文案模板：多数服务商要求验证码与签名之外的内容与其报备模板一致。
CODE_TEXT = "【{sign}】你的验证码是 {code}，{minutes} 分钟内有效。请勿泄露给他人。"


class SmsDeliveryError(RuntimeError):
    """短信发送失败（已配置网关但调用出错）。"""


def sms_enabled() -> bool:
    """是否配置了可用的短信网关（缺地址或缺 Key 都视为未配置）。"""
    return bool(settings.sms_api_url and settings.sms_api_key)


def code_text(code: str) -> str:
    """拼装短信正文（服务商的模板变量多数就取这一句）。"""
    minutes = max(1, settings.sms_code_ttl_seconds // 60)
    return CODE_TEXT.format(
        sign=settings.sms_sign_name, code=code, minutes=minutes
    )


def _request_body(phone: str, code: str) -> bytes:
    """构造网关请求体。

    **接入真实服务商时只改这个函数**（例如阿里云要签名串、云片要 tpl_id）。
    默认给的是最通用的 JSON：号码 + 正文 + 签名 + 单独的验证码字段。
    """
    payload = {
        "phone": phone,
        "content": code_text(code),
        "sign": settings.sms_sign_name,
        "code": code,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _send_via_gateway(phone: str, code: str) -> tuple[bool, str]:
    """调用 HTTP 网关；HTTP 2xx 视为成功，其余一律按失败返回（不抛异常）。"""
    request = urllib.request.Request(
        settings.sms_api_url,
        data=_request_body(phone, code),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {settings.sms_api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=SMS_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if 200 <= status < 300:
                return True, ""
            return False, f"短信网关返回 HTTP {status}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.exception("短信发送失败：%s", exc)
        return False, f"短信发送失败：{exc}"


def send_verification_code(phone: str, code: str, purpose: str = "bind") -> tuple[bool, str]:
    """发送验证码，返回 ``(是否真实发出, 说明)``。

    - 未配置网关：写日志（含验证码与服务商要求的正文）并返回降级提示，
      由设置页的兜底面板展示验证码；
    - 已配置但调用失败：返回失败原因；
    - 发送成功：返回 ``(True, "验证码已发送至 138****8000")``。

    ``purpose`` 只用于日志区分「绑定 / 解绑」，不影响正文。
    """
    if not sms_enabled():
        logger.info(
            "未配置短信网关，跳过真实发送（purpose=%s, phone=%s）：%s",
            purpose,
            phone,
            code_text(code),
        )
        return False, "当前未配置短信服务，验证码在页面下方直接显示"

    ok, error = _send_via_gateway(phone, code)
    if ok:
        return True, "验证码已发送"
    return False, error
