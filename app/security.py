"""密码哈希与会话令牌：仅使用标准库实现，避免为认证引入新的第三方依赖。

- 密码：PBKDF2-HMAC-SHA256 + 每用户随机盐；存储格式
  ``pbkdf2_sha256$<迭代次数>$<盐 hex>$<摘要 hex>``，明文密码不落库。
- 会话：把 ``{"uid": <用户 id>, "iat": <签发时间>}`` 序列化后用
  HMAC-SHA256 签名（密钥来自配置），格式 ``<base64url(payload)>.<签名>``。
  Cookie 只保存用户 id，角色每次请求从数据库读取，便于后续变更即时生效。
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

PBKDF2_ITERATIONS = 200_000
_HASH_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
# 允许的客户端与服务器时钟偏差（秒）：签发时间晚于当前时间超过该值视为异常令牌。
_CLOCK_SKEW_SECONDS = 60


def hash_password(password: str) -> str:
    """生成可存储的密码哈希；空密码视为非法输入。"""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_hex(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return f"{_HASH_ALGORITHM}${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """校验明文密码与存储哈希是否匹配；格式异常或不匹配一律返回 False。"""
    try:
        algorithm, iterations, salt, expected = stored_hash.split("$")
        if algorithm != _HASH_ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


def hash_token(token: str) -> str:
    """一次性令牌的 SHA-256 十六进制摘要。

    邮箱验证等令牌只在邮件/页面链接里出现明文，库中只存本摘要，
    数据库泄露也无法直接拼出可用链接。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes, secret_key: str) -> str:
    return _b64encode(hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).digest())


def create_signed_payload(
    payload: dict, secret_key: str, *, now: float | None = None
) -> str:
    """把任意 JSON 载荷签成 ``<base64url(payload)>.<签名>``。

    与会话令牌同一套 HMAC-SHA256 格式，供设备令牌等「轻量可信状态」复用；
    ``iat`` 由调用方自带（这里不强行注入，签名载荷本身不解释语义）。
    """
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{_b64encode(body)}.{_sign(body, secret_key)}"


def _read_payload(token: str, secret_key: str) -> dict | None:
    """校验签名并返回载荷字典；签名不符 / 格式错误一律返回 None。"""
    try:
        encoded_payload, signature = token.split(".")
    except (AttributeError, ValueError):
        return None
    try:
        payload = _b64decode(encoded_payload)
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(_sign(payload, secret_key), signature):
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def create_device_token(secret_key: str, *, now: float | None = None) -> str:
    """签发游客试算的「设备令牌」：随机设备 id + 签发时间。

    只用于按设备限制免登录试算次数（见 persona_pages 的限流）；不含任何
    用户信息，签名防伪造，HttpOnly Cookie 保存，前端读不到。
    """
    payload = {
        "dev": secrets.token_hex(8),
        "iat": int(time.time() if now is None else now),
    }
    return create_signed_payload(payload, secret_key)


def read_device_token(token: str, secret_key: str) -> str | None:
    """校验设备令牌并返回设备 id；无效 / 缺字段返回 None（有效期由调用方按窗口判）。"""
    data = _read_payload(token, secret_key)
    if not data:
        return None
    device = str(data.get("dev") or "").strip()
    return device or None


def create_session_token(
    user_id: int,
    secret_key: str,
    *,
    now: float | None = None,
) -> str:
    """为指定用户签发会话令牌（不含角色，角色在请求时查库）。"""
    issued_at = int(time.time() if now is None else now)
    return create_signed_payload(
        {"uid": user_id, "iat": issued_at}, secret_key, now=now
    )


def decode_session(
    token: str,
    secret_key: str,
    max_age_seconds: int,
    *,
    now: float | None = None,
) -> dict | None:
    """校验会话令牌并返回载荷 ``{"uid": ..., "iat": ...}``。

    签名不符、格式错误或已过期一律返回 None。``iat`` 一并返回，供调用方
    判断「签发时间是否早于最近一次改密时间」——会话 Cookie 无法在服务端
    逐条吊销，只能靠这个时间戳把旧会话整体判死。
    """
    data = _read_payload(token, secret_key)
    if not data:
        return None
    try:
        claims = {"uid": int(data["uid"]), "iat": int(data["iat"])}
    except (KeyError, TypeError, ValueError):
        return None
    current = int(time.time() if now is None else now)
    if claims["iat"] > current + _CLOCK_SKEW_SECONDS:
        return None
    if max_age_seconds and current - claims["iat"] > max_age_seconds:
        return None
    return claims


def read_session_token(
    token: str,
    secret_key: str,
    max_age_seconds: int,
    *,
    now: float | None = None,
) -> int | None:
    """校验会话令牌并返回用户 id；签名不符、格式错误或已过期返回 None。"""
    claims = decode_session(
        token, secret_key, max_age_seconds, now=now
    )
    return None if claims is None else claims["uid"]
