"""第三方应用单点登录（OAuth 风格，站点侧授权、用户无感知）。

设计要点（与协作方原始规格的差异，详见给合作方的回复）：
- 授权由站点单方面决定：登录后自动签发一次性票据并回跳，**不做用户同意页**，
  用户全程无感知；
- 令牌端点必须带 ``client_secret``（仅平台后端持有），补齐原始规格只靠公开
  ``client_id`` 鉴权的漏洞；
- 撤销有两路：① 站点管理员应急吊销（``POST /api/auth/revoke``，需管理员会话，
  按 token 或 external_user_id 全量吊销）；② **自助吊销（self-revoke）**：持有效
  Bearer 令牌的一方（即 App 后端，在用户点「断开与网站的连接」时触发）可撤销「自己的」
  令牌——仍不暴露任何终端用户可见的站内 UI，端点由站点掌控；

- 票据一次性 + 120 秒、redirect_uri 白名单、state 防 CSRF、令牌只存哈希、
  scope 仅 identity（不给任何业务写权限）。

流程：
1. App 打开 GET /oauth/authorize?client_id&redirect_uri&state（浏览器 / WebView）；
2. 未登录先走站点登录，登录后自动回到本端点；
3. 已登录即签发一次性票据并 302 跳回 ``redirect_uri?ticket&state``；
4. 平台后端 POST /api/auth/token{ticket,client_id,client_secret} 换 access_token；
5. 平台后端 GET /api/me（Bearer）取身份；持令牌方可 POST /api/auth/revoke 自助吊销，
   站点管理员亦可 POST /api/auth/revoke 应急吊销。
"""
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote

import hmac
import time

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import AppAccessToken, OAuthTicket, User
from app.security import generate_access_token, generate_oauth_ticket, hash_token

router = APIRouter(tags=["oauth"])

DbSession = Annotated[Session, Depends(get_db)]

# ---------- 轻量限流：令牌端点按 IP 窗口计数，防票据暴力枚举 ----------
_TOKEN_LIMIT_WINDOW = 60
_TOKEN_LIMIT_MAX = 100
_token_attempts: dict[str, list[float]] = {}


def _token_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = _token_attempts.get(ip, [])
    hits = [t for t in hits if now - t < _TOKEN_LIMIT_WINDOW]
    if len(hits) >= _TOKEN_LIMIT_MAX:
        _token_attempts[ip] = hits
        return False
    hits.append(now)
    _token_attempts[ip] = hits
    return True


def _allowed_redirect_uri(redirect_uri: str) -> bool:
    if not redirect_uri:
        return False
    raw = settings.oauth_redirect_uris or ""
    allowed = [u.strip() for u in raw.replace(";", ",").split(",") if u.strip()]
    return redirect_uri in allowed


# ---------- 1. 授权页：登录门 + 白名单 + state + 一次性票据 + 回跳 ----------


@router.get("/oauth/authorize")
def oauth_authorize(
    request: Request,
    db: DbSession,
    client_id: str = Query(""),
    redirect_uri: str = Query(""),
    state: str = Query(""),
):
    if not settings.oauth_enabled or not settings.oauth_client_id:
        raise HTTPException(status_code=400, detail="oauth_disabled")
    if not client_id or client_id != settings.oauth_client_id:
        raise HTTPException(status_code=400, detail="invalid_client")
    if not _allowed_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    if not state:
        raise HTTPException(status_code=400, detail="state_required")

    user = get_current_user(request, db)
    if user is None:
        # 带齐 oauth 参数穿过登录；登录成功后自动回到本端点开票，用户无感知。
        inner = (
            f"/oauth/authorize?client_id={quote(client_id)}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&state={quote(state)}"
        )
        return RedirectResponse(f"/login?next={quote(inner, safe='')}", status_code=303)

    ticket = generate_oauth_ticket()
    now = datetime.now()
    expires = now + timedelta(seconds=settings.oauth_ticket_ttl_seconds)
    db.add(
        OAuthTicket(
            ticket_hash=hash_token(ticket),
            user_id=user.id,
            client_id=client_id,
            issued_at=now,
            expires_at=expires,
        )
    )
    db.commit()

    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}ticket={quote(ticket)}&state={quote(state)}",
        status_code=302,
    )


# ---------- 2. 换令牌：client_secret 校验 + 一次性 + 限流 ----------


@router.post("/api/auth/token")
def token_exchange(
    request: Request,
    db: DbSession,
    client_id: str = Form(""),
    client_secret: str = Form(""),
    ticket: str = Form(""),
):
    client_ip = request.client.host if request.client else "unknown"
    if not settings.oauth_enabled:
        raise HTTPException(status_code=400, detail="oauth_disabled")
    if not _token_rate_ok(client_ip):
        raise HTTPException(status_code=429, detail="too_many_requests")
    if not client_id or client_id != settings.oauth_client_id:
        raise HTTPException(status_code=401, detail="invalid_client")
    if not settings.oauth_client_secret or not hmac.compare_digest(
        client_secret, settings.oauth_client_secret
    ):
        # 密钥未配置或比对失败一律 401；恒定时间比对避免计时侧信道。
        raise HTTPException(status_code=401, detail="invalid_client")
    if not ticket:
        raise HTTPException(status_code=400, detail="invalid_grant")

    now = datetime.now()
    row = (
        db.query(OAuthTicket)
        .filter(OAuthTicket.ticket_hash == hash_token(ticket), OAuthTicket.used == False)  # noqa: E712
        .first()
    )
    if row is None or row.expires_at < now or row.client_id != client_id:
        raise HTTPException(status_code=400, detail="invalid_grant")

    # 一次性：立即标记已用，重复兑换直接失败。
    row.used = True
    row.used_at = now

    token = generate_access_token()
    ttl = settings.oauth_token_ttl_seconds
    expires = now + timedelta(seconds=ttl)
    db.add(
        AppAccessToken(
            token_hash=hash_token(token),
            user_id=row.user_id,
            client_id=client_id,
            scope="identity",
            issued_at=now,
            expires_at=expires,
        )
    )
    db.commit()

    user = db.get(User, row.user_id)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "user": {
            "external_user_id": str(user.id),
            "nickname": user.username,
            "avatar_url": user.avatar_url or "",
        },
    }


# ---------- 3. 身份读取：Bearer 校验 ----------


def _require_bearer(db: Session, authorization: str | None) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    raw = authorization.split(" ", 1)[1].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing_token")
    now = datetime.now()
    row = (
        db.query(AppAccessToken)
        .filter(
            AppAccessToken.token_hash == hash_token(raw),
            AppAccessToken.revoked == False,  # noqa: E712
        )
        .first()
    )
    if row is None or row.expires_at < now:
        raise HTTPException(status_code=401, detail="invalid_token")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid_token")
    return user


@router.get("/api/me")
def oauth_me(db: DbSession, authorization: str = Header(None)):
    if not settings.oauth_enabled:
        raise HTTPException(status_code=400, detail="oauth_disabled")
    user = _require_bearer(db, authorization)
    return {
        "external_user_id": str(user.id),
        "nickname": user.username,
        "avatar_url": user.avatar_url or "",
        "scope": "identity",
    }


# ---------- 4. 撤销：仅站点管理员（应急吊销，无终端用户入口） ----------


@router.post("/api/auth/revoke")
def oauth_revoke(
    request: Request,
    db: DbSession,
    access_token: str = Form(""),
    external_user_id: str = Form(""),
    authorization: str = Header(None),
):
    if not settings.oauth_enabled:
        raise HTTPException(status_code=400, detail="oauth_disabled")

    # 路径 A：自助吊销（self-revoke）。
    # 持有效 Bearer 令牌的一方撤销「自己的」令牌——典型场景是 App 后端在用户于 App 内
    # 点「断开与网站的连接」时带当前令牌调用。不暴露任何终端用户可见的站内 UI，
    # 端点仍由站点掌控。吊销该持有者名下、同一 client 的全部未吊销令牌（彻底断开）。
    # 令牌无效（过期 / 伪造 / 已吊销）则 401，无法自助吊销——此时走站点管理员路径。
    if authorization and authorization.startswith("Bearer "):
        holder = _require_bearer(db, authorization)
        now = datetime.now()
        n = (
            db.query(AppAccessToken)
            .filter(
                AppAccessToken.user_id == holder.id,
                AppAccessToken.client_id == settings.oauth_client_id,
                AppAccessToken.revoked == False,  # noqa: E712
            )
            .update({"revoked": True, "revoked_at": now}, synchronize_session=False)
        )
        db.commit()
        return {"revoked": True, "self": True, "count": n}

    # 路径 B：站点管理员应急吊销（按 token 或 external_user_id 全量吊销）。
    admin = get_current_user(request, db)
    if admin is None or not admin.is_staff:
        raise HTTPException(status_code=403, detail="forbidden")
    now = datetime.now()
    if access_token:
        row = (
            db.query(AppAccessToken)
            .filter(AppAccessToken.token_hash == hash_token(access_token))
            .first()
        )
        if row is not None:
            row.revoked = True
            row.revoked_at = now
    elif external_user_id:
        try:
            uid = int(external_user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_user")
        db.query(AppAccessToken).filter(
            AppAccessToken.user_id == uid, AppAccessToken.revoked == False  # noqa: E712
        ).update({"revoked": True, "revoked_at": now}, synchronize_session=False)
    else:
        raise HTTPException(status_code=400, detail="missing_target")
    db.commit()
    return {"revoked": True}
