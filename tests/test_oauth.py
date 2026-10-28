"""第三方单点登录测试：覆盖授权回跳 / 换令牌 / 身份读取 / 撤销。

仅跑本文件（不跑全量）；独立临时 SQLite，与开发库完全隔离。
"""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, get_db
from app.main import app
from auth_helpers import (
    ADMIN_USERNAME,
    USER_USERNAME,
    create_accounts,
    login_as_admin,
    login_as_user,
)

CLIENT_ID = "test_client"
CLIENT_SECRET = "test_secret"
REDIRECT = "https://app.example.com/cb"


@pytest.fixture
def oauth_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'oauth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    db = TestingSession()
    try:
        crud.seed_if_empty(db)
    finally:
        db.close()
    create_accounts(TestingSession)

    monkeypatch.setattr(settings, "oauth_enabled", True)
    monkeypatch.setattr(settings, "oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oauth_client_secret", CLIENT_SECRET)
    monkeypatch.setattr(settings, "oauth_redirect_uris", f"{REDIRECT},{REDIRECT}2")

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield SimpleNamespace(
            client=TestClient(app, follow_redirects=False),
            session_factory=TestingSession,
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _user_id(session_factory, username: str) -> int:
    db = session_factory()
    try:
        return crud.get_user_by_username(db, username).id
    finally:
        db.close()


def _authorize(oauth_env, *, logged_in=True, **params):
    client = oauth_env.client
    if logged_in:
        login_as_user(client)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/oauth/authorize?{qs}")


def _exchange(oauth_env, *, ticket, client_id=CLIENT_ID, client_secret=CLIENT_SECRET):
    return oauth_env.client.post(
        "/api/auth/token",
        data={"ticket": ticket, "client_id": client_id, "client_secret": client_secret},
    )


# ---------- 阀门：未开启 / 非法 client / 非法 redirect / 缺 state ----------


def test_disabled_rejects(oauth_env, monkeypatch):
    monkeypatch.setattr(settings, "oauth_enabled", False)
    assert _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s").status_code == 400
    assert _exchange(oauth_env, ticket="x").status_code == 400
    assert oauth_env.client.get("/api/me", headers={"Authorization": "Bearer z"}).status_code == 400


def test_invalid_client(oauth_env):
    assert _authorize(oauth_env, client_id="wrong", redirect_uri=REDIRECT, state="s").status_code == 400
    assert _exchange(oauth_env, ticket="x", client_id="wrong").status_code == 401


def test_redirect_not_whitelisted(oauth_env):
    assert (
        _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri="https://evil.example.com", state="s").status_code
        == 400
    )


def test_state_required(oauth_env):
    assert _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="").status_code == 400


# ---------- 全链路：登录 -> 授权 -> 换令牌 -> me ----------


def test_full_flow(oauth_env):
    uid = _user_id(oauth_env.session_factory, USER_USERNAME)
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="xyz")
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith(REDIRECT + "?")
    qs = parse_qs(urlparse(loc).query)
    assert qs["state"] == ["xyz"]
    ticket = qs["ticket"][0]
    assert ticket

    ex = _exchange(oauth_env, ticket=ticket)
    assert ex.status_code == 200
    body = ex.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == settings.oauth_token_ttl_seconds
    assert body["user"]["external_user_id"] == str(uid)
    assert body["user"]["nickname"] == USER_USERNAME
    token = body["access_token"]
    assert token

    me = oauth_env.client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["external_user_id"] == str(uid)


def test_unauthenticated_redirected_to_login(oauth_env):
    resp = oauth_env.client.get(
        f"/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT}&state=xyz"
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login?next=")


# ---------- 换令牌的失败分支 ----------


def test_wrong_secret(oauth_env):
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    assert _exchange(oauth_env, ticket=ticket, client_secret="bad").status_code == 401


def test_missing_secret_config(oauth_env, monkeypatch):
    monkeypatch.setattr(settings, "oauth_client_secret", "")
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    assert _exchange(oauth_env, ticket=ticket).status_code == 401


def test_used_ticket_rejected(oauth_env):
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    assert _exchange(oauth_env, ticket=ticket).status_code == 200
    # 同一票据二次兑换必须失败（一次性）
    assert _exchange(oauth_env, ticket=ticket).status_code == 400


def test_expired_ticket_rejected(oauth_env, monkeypatch):
    # 把票据有效期压成负数，签发即过期，验证过期分支（同属 invalid_grant）。
    monkeypatch.setattr(settings, "oauth_ticket_ttl_seconds", -10)
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    assert _exchange(oauth_env, ticket=ticket).status_code == 400


def test_missing_ticket(oauth_env):
    assert _exchange(oauth_env, ticket="").status_code == 400


# ---------- 身份读取失败分支 ----------


def test_me_without_token(oauth_env):
    assert oauth_env.client.get("/api/me").status_code == 401


def test_me_with_bad_token(oauth_env):
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": "Bearer garbage"}).status_code
        == 401
    )


def test_me_wrong_scheme(oauth_env):
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": "Basic abc"}).status_code
        == 401
    )


# ---------- 撤销：仅管理员，且立即失效 ----------


def test_revoke_requires_admin(oauth_env):
    login_as_user(oauth_env.client)
    assert oauth_env.client.post("/api/auth/revoke", data={"external_user_id": "1"}).status_code == 403


def test_admin_revoke_by_user_id_invalidates_token(oauth_env):
    uid = _user_id(oauth_env.session_factory, USER_USERNAME)
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    token = _exchange(oauth_env, ticket=ticket).json()["access_token"]

    login_as_admin(oauth_env.client)
    rev = oauth_env.client.post("/api/auth/revoke", data={"external_user_id": str(uid)})
    assert rev.status_code == 200
    assert rev.json()["revoked"] is True

    # 被吊销的令牌立即失效
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_admin_revoke_by_token(oauth_env):
    resp = _authorize(oauth_env, client_id=CLIENT_ID, redirect_uri=REDIRECT, state="s")
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    ex = _exchange(oauth_env, ticket=ticket).json()
    token = ex["access_token"]

    login_as_admin(oauth_env.client)
    rev = oauth_env.client.post("/api/auth/revoke", data={"access_token": token})
    assert rev.status_code == 200
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_revoke_by_unknown_user_is_safe(oauth_env):
    login_as_admin(oauth_env.client)
    # 不存在的 user_id 不应报错（无行可撤）
    assert oauth_env.client.post("/api/auth/revoke", data={"external_user_id": "999999"}).status_code == 200


# ---------- 自助吊销（self-revoke）：持有效 Bearer 撤销自己的令牌 ----------


def _get_token(oauth_env, *, as_admin: bool = False) -> str:
    if as_admin:
        login_as_admin(oauth_env.client)
    else:
        login_as_user(oauth_env.client)
    resp = oauth_env.client.get(
        f"/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT}&state=s"
    )
    ticket = parse_qs(urlparse(resp.headers["location"]).query)["ticket"][0]
    return _exchange(oauth_env, ticket=ticket).json()["access_token"]


def test_self_revoke_by_bearer_invalidates_token(oauth_env):
    token = _get_token(oauth_env)
    # 不登录管理员，直接带 Bearer 自助吊销
    rev = oauth_env.client.post(
        "/api/auth/revoke", headers={"Authorization": f"Bearer {token}"}
    )
    assert rev.status_code == 200
    body = rev.json()
    assert body["revoked"] is True and body.get("self") is True and body["count"] >= 1
    # 自己的令牌立即失效
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


def test_self_revoke_requires_valid_bearer(oauth_env):
    # 无任何鉴权：落回管理员路径，普通访客 403（不能开放式吊销）
    assert oauth_env.client.post("/api/auth/revoke", data={"external_user_id": "1"}).status_code == 403
    # 伪造 Bearer：直接 401，不会误当管理员
    assert (
        oauth_env.client.post(
            "/api/auth/revoke", headers={"Authorization": "Bearer garbage"}
        ).status_code
        == 401
    )


def test_self_revoke_isolated_to_own_user(oauth_env):
    user_token = _get_token(oauth_env, as_admin=False)
    admin_token = _get_token(oauth_env, as_admin=True)
    # 用户自助吊销自己的令牌，不应波及其它用户的令牌
    rev = oauth_env.client.post(
        "/api/auth/revoke", headers={"Authorization": f"Bearer {user_token}"}
    )
    assert rev.status_code == 200
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": f"Bearer {admin_token}"}).status_code
        == 200
    )
    assert (
        oauth_env.client.get("/api/me", headers={"Authorization": f"Bearer {user_token}"}).status_code
        == 401
    )
