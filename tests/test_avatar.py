"""用户头像：上传 / 替换 / 恢复默认 / 校验 / 页面展示。

覆盖：
- 游客上传跳登录；
- 合法图片：落盘为 256×256 WebP、写 users.avatar_url、页面与导航渲染 <img>；
- 替换头像：旧文件被删除，只保留新文件；
- 校验：非图片、超大文件都拒绝且不落库；
- 恢复默认：清空字段并删除文件；
- 老库补列：ensure_user_profile_columns 补 avatar_url。

图片全部用 Pillow 现场生成，不依赖外部素材与网络。
"""

import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import crud, media
from app.config import settings
from app.database import Base, get_db
from app.main import app
from auth_helpers import USER_USERNAME, create_accounts, login_as_user


def _png(size=(300, 200), color=(200, 40, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def avatar_env(tmp_path, monkeypatch):
    """独立临时库 + 临时头像目录（不碰 app/static/avatars）。"""
    monkeypatch.setattr(settings, "avatar_storage_dir", str(tmp_path / "avatars"))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'avatar.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        crud.seed_if_empty(db)
    finally:
        db.close()
    create_accounts(factory)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, follow_redirects=False)
    login_as_user(client)
    try:
        yield SimpleNamespace(
            client=client, factory=factory, root=tmp_path / "avatars"
        )
    finally:
        app.dependency_overrides.pop(get_db, None)


def _account(factory):
    db = factory()
    try:
        return crud.get_user_by_username(db, USER_USERNAME)
    finally:
        db.close()


def _upload(env, data: bytes, filename: str = "me.png"):
    return env.client.post(
        "/me/avatar",
        files={"file": (filename, data, "image/png")},
        follow_redirects=False,
    )


class TestUpload:
    def test_guest_redirected(self, avatar_env):
        client = TestClient(app, follow_redirects=False)
        resp = client.post(
            "/me/avatar",
            files={"file": ("me.png", _png(), "image/png")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?next=")

    def test_upload_saves_square_webp(self, avatar_env):
        resp = _upload(avatar_env, _png())
        assert resp.status_code == 303
        assert "avatar=ok" in resp.headers["location"]

        account = _account(avatar_env.factory)
        assert account.avatar_url.startswith("/avatars/avatar_u")
        assert account.avatar_url.endswith(".webp")

        path = avatar_env.root / account.avatar_url.rpartition("/")[2]
        assert path.is_file()
        with Image.open(path) as image:
            assert image.size == (256, 256)
            assert image.format == "WEBP"

    def test_page_and_nav_render_image(self, avatar_env):
        _upload(avatar_env, _png())
        page = avatar_env.client.get("/me")
        assert 'class="space-avatar space-avatar-img"' in page.text
        assert 'class="navbar-avatar"' in page.text
        assert "恢复默认" in page.text

    def test_replace_removes_old_file(self, avatar_env):
        _upload(avatar_env, _png(color=(10, 20, 30)))
        first = _account(avatar_env.factory).avatar_url
        _upload(avatar_env, _png(color=(200, 200, 10)))
        second = _account(avatar_env.factory).avatar_url

        assert first != second
        assert not (avatar_env.root / first.rpartition("/")[2]).exists()
        assert (avatar_env.root / second.rpartition("/")[2]).is_file()

    def test_rejects_non_image(self, avatar_env):
        resp = _upload(avatar_env, b"not an image at all")
        assert "avatar_err=not_image" in resp.headers["location"]
        assert _account(avatar_env.factory).avatar_url is None

    def test_rejects_oversize(self, avatar_env, monkeypatch):
        monkeypatch.setattr(settings, "avatar_upload_max_bytes", 100)
        resp = _upload(avatar_env, _png(size=(400, 400)))
        assert "avatar_err=too_big" in resp.headers["location"]
        assert _account(avatar_env.factory).avatar_url is None


class TestDelete:
    def test_reset_removes_file(self, avatar_env):
        _upload(avatar_env, _png())
        url = _account(avatar_env.factory).avatar_url
        path = avatar_env.root / url.rpartition("/")[2]
        assert path.is_file()

        resp = avatar_env.client.post("/me/avatar/delete", follow_redirects=False)
        assert "avatar=removed" in resp.headers["location"]
        assert _account(avatar_env.factory).avatar_url is None
        assert not path.exists()

    def test_reset_without_avatar_is_fine(self, avatar_env):
        resp = avatar_env.client.post("/me/avatar/delete", follow_redirects=False)
        assert "avatar=removed" in resp.headers["location"]


class TestColumns:
    def test_legacy_users_table_gets_avatar_column(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy_users.db'}")
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE TABLE users (id INTEGER PRIMARY KEY,"
                        " username VARCHAR(50) NOT NULL)"
                    )
                )
            added = crud.ensure_user_profile_columns(engine)
            assert "avatar_url" in added
            assert crud.ensure_user_profile_columns(engine) == []
        finally:
            engine.dispose()


def test_remove_avatar_file_ignores_foreign_urls():
    assert media.remove_avatar_file(None) is False
    assert media.remove_avatar_file("https://example.com/x.webp") is False
    assert media.remove_avatar_file("/covers/cover_1.jpg") is False
    assert media.remove_avatar_file("/avatars/../secret.webp") is False
