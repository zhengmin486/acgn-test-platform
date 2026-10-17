"""封面图片管线测试：识别 / 文件名净化 / 缩略图 / 压缩全图 / 按需端点 / 上传 / 路由。

零外部网络：测试图片全部用 Pillow 现场生成；封面目录用临时目录
（monkeypatch settings.cover_storage_dir），不碰项目内真实 covers 目录；
上传路由在独立临时库上跑，复用 auth_helpers 的账号与登录。
"""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import media
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Content
from auth_helpers import create_accounts, login_as_admin

# 一张最小的合法 JPEG 头 + 填充（解码会失败，用于「魔术字节对但文件损坏」场景）。
JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"


def make_image_bytes(size=(1600, 2400), color=(91, 60, 160), fmt="JPEG") -> bytes:
    """现场生成一张纯色图（平滑、可压缩性好，用于验证尺寸与体积预算）。"""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


def make_noisy_jpeg(size=(2000, 3000)) -> bytes:
    """现场生成一张噪声图（难压缩，用于验证「先缩宽度再降画质」的兜底）。"""
    noise = Image.effect_noise(size, 64).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def make_rgba_png(size=(900, 1350)) -> bytes:
    """带透明通道的 PNG：验证派生前会贴白底，不会出黑块。"""
    image = Image.new("RGBA", size, (255, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture
def cover_dir(tmp_path, monkeypatch):
    """把封面根目录指到临时目录，隔离项目内真实 covers。"""
    root = tmp_path / "covers"
    monkeypatch.setattr(settings, "cover_storage_dir", str(root))
    return root


def put_cover(cover_dir: Path, name: str, data: bytes) -> Path:
    cover_dir.mkdir(parents=True, exist_ok=True)
    path = cover_dir / name
    path.write_bytes(data)
    return path


# ---------- 魔术字节识别 ----------


class TestSniffImage:
    def test_recognizes_supported_formats(self):
        assert media.sniff_image(JPEG_HEADER) == "jpeg"
        assert media.sniff_image(make_image_bytes(fmt="PNG")) == "png"
        assert media.sniff_image(make_image_bytes(fmt="WEBP")) == "webp"
        assert media.sniff_image(make_rgba_png()) == "png"
        assert media.sniff_image(b"GIF89a" + b"\x00" * 20) == "gif"

    def test_rejects_non_images(self):
        assert media.sniff_image(b"") is None
        assert media.sniff_image(b"<html>429 Too Many Requests</html>") is None
        assert media.sniff_image(b"RIFF____AVI " + b"\x00" * 20) is None  # RIFF 但非 WEBP
        assert media.sniff_image(b"\x89PNG\r\n\x1a\n"[:5]) is None  # 头不完整


# ---------- 文件名净化（防路径穿越） ----------


class TestSafeCoverName:
    def test_accepts_plain_basenames(self):
        assert media.safe_cover_name("cover_265.jpg") == "cover_265.jpg"
        assert media.safe_cover_name("cover_u3_1699999999999.png") == "cover_u3_1699999999999.png"
        assert media.safe_cover_name("cover-9.WEBP") == "cover-9.WEBP"

    def test_rejects_traversal_and_weird_names(self):
        assert media.safe_cover_name("../secret.jpg") is None
        assert media.safe_cover_name("..%2Fsecret.jpg") is None
        assert media.safe_cover_name("a/b.jpg") is None
        assert media.safe_cover_name("a\\b.jpg") is None
        assert media.safe_cover_name("/etc/passwd") is None
        assert media.safe_cover_name(".env") is None
        assert media.safe_cover_name("x.tar.gz") is None  # 去扩展名后仍含点
        assert media.safe_cover_name("noext") is None
        assert media.safe_cover_name("") is None
        assert media.safe_cover_name("x" * 200 + ".jpg") is None


# ---------- 派生图生成 ----------


class TestGenerateVariants:
    def test_thumb_is_400px_webp(self, cover_dir):
        source = put_cover(cover_dir, "cover_265.jpg", make_image_bytes())
        thumb = media.generate_variant("thumb", source)
        assert thumb == cover_dir / "thumbs" / "cover_265.webp"
        image = decode(thumb.read_bytes())
        assert image.format == "WEBP"
        assert image.width == 400
        assert image.height == 600  # 2:3 等比

    def test_thumb_never_upscales(self, cover_dir):
        source = put_cover(cover_dir, "cover_small.jpg", make_image_bytes(size=(300, 450)))
        thumb = media.generate_variant("thumb", source)
        assert decode(thumb.read_bytes()).size == (300, 450)

    def test_full_respects_width_and_budget(self, cover_dir):
        source = put_cover(cover_dir, "cover_big.jpg", make_image_bytes(size=(3000, 4500)))
        full = media.generate_variant("full", source)
        image = decode(full.read_bytes())
        assert image.format == "WEBP"
        assert image.width == 1200
        assert len(full.read_bytes()) <= settings.cover_full_max_bytes

    def test_full_shrinks_noisy_image_within_width(self, cover_dir):
        """难压缩的大图：宽度必须仍被压到上限内（画质保底接受超限）。"""
        source = put_cover(cover_dir, "cover_noise.jpg", make_noisy_jpeg())
        full = media.generate_variant("full", source)
        image = decode(full.read_bytes())
        assert image.width <= settings.cover_full_max_width
        assert len(full.read_bytes()) < 2 * 1024 * 1024  # 至少比原图小一个量级

    def test_rgba_png_flattened_over_white(self, cover_dir):
        source = put_cover(cover_dir, "cover_alpha.png", make_rgba_png())
        thumb = media.generate_variant("thumb", source)
        image = decode(thumb.read_bytes())
        assert image.mode == "RGB"
        assert image.getpixel((10, 10)) == (255, 255, 255)  # 透明处贴白，不是黑块

    def test_corrupt_source_raises_media_error(self, cover_dir):
        source = put_cover(cover_dir, "cover_broken.jpg", JPEG_HEADER + b"\x00" * 64)
        with pytest.raises(media.MediaError):
            media.generate_variant("thumb", source)

    def test_atomic_write_leaves_no_temp_files(self, cover_dir):
        source = put_cover(cover_dir, "cover_265.jpg", make_image_bytes())
        media.generate_variant("thumb", source)
        media.generate_variant("full", source)
        assert list((cover_dir / "thumbs").glob(".tmp-*")) == []
        assert list((cover_dir / "full").glob(".tmp-*")) == []


class TestEnsureVariant:
    def test_cache_hit_is_idempotent(self, cover_dir):
        put_cover(cover_dir, "cover_265.jpg", make_image_bytes())
        first = media.ensure_variant("thumb", "cover_265.jpg")
        assert first is not None
        stamp = first.stat().st_mtime_ns
        second = media.ensure_variant("thumb", "cover_265.jpg")
        assert second == first
        assert second.stat().st_mtime_ns == stamp  # 命中缓存不重新生成

    def test_missing_source_returns_none(self, cover_dir):
        assert media.ensure_variant("thumb", "cover_absent.jpg") is None

    def test_illegal_name_returns_none(self, cover_dir):
        assert media.ensure_variant("thumb", "../x.jpg") is None

    def test_corrupt_source_returns_none(self, cover_dir):
        put_cover(cover_dir, "cover_broken.jpg", JPEG_HEADER + b"\x00" * 64)
        assert media.ensure_variant("thumb", "cover_broken.jpg") is None


# ---------- 按需端点 ----------


class TestMediaEndpoints:
    def test_thumb_endpoint_generates_and_caches(self, cover_dir):
        put_cover(cover_dir, "cover_265.jpg", make_image_bytes())
        with TestClient(app) as client:
            first = client.get("/media/thumb/cover_265.jpg")
            assert first.status_code == 200
            assert first.headers["content-type"] == "image/webp"
            assert first.headers["cache-control"] == "public, max-age=2592000"
            assert decode(first.content).width == 400
            second = client.get("/media/thumb/cover_265.jpg")
            assert second.status_code == 200
            assert second.content == first.content  # 二次访问走缓存

    def test_full_endpoint_serves_compressed(self, cover_dir):
        put_cover(cover_dir, "cover_265.jpg", make_image_bytes(size=(3000, 4500)))
        with TestClient(app) as client:
            resp = client.get("/media/full/cover_265.jpg")
        assert resp.status_code == 200
        image = decode(resp.content)
        assert image.format == "WEBP"
        assert image.width == 1200
        assert len(resp.content) <= settings.cover_full_max_bytes

    def test_falls_back_to_original_when_generation_fails(self, cover_dir):
        """源图损坏时服务端降级返回原图：封面可见性优先于体积优化。"""
        raw = JPEG_HEADER + b"\x00" * 64
        put_cover(cover_dir, "cover_broken.jpg", raw)
        with TestClient(app) as client:
            resp = client.get("/media/full/cover_broken.jpg")
        assert resp.status_code == 200
        assert resp.content == raw  # 原图字节，前端 onerror 再兜底

    def test_missing_cover_returns_404(self, cover_dir):
        with TestClient(app) as client:
            assert client.get("/media/thumb/nope.jpg").status_code == 404
            assert client.get("/media/full/nope.jpg").status_code == 404

    def test_traversal_and_illegal_names_return_404(self, cover_dir):
        with TestClient(app) as client:
            assert client.get("/media/thumb/..%2F..%2Fsecret.jpg").status_code == 404
            assert client.get("/media/thumb/.env").status_code == 404
            assert client.get("/media/full/x.tar.gz").status_code == 404


# ---------- 上传校验与落盘 ----------


class TestSaveUpload:
    def test_valid_upload_writes_original_and_variants(self, cover_dir):
        url = media.save_upload(make_image_bytes(), content_id=7)
        assert url.startswith("/covers/cover_u7_")
        assert url.endswith(".jpg")
        name = url.rpartition("/")[2]
        original = cover_dir / name
        assert original.is_file()
        stem = name.rpartition(".")[0]
        thumb = cover_dir / "thumbs" / f"{stem}.webp"
        full = cover_dir / "full" / f"{stem}.webp"
        assert thumb.is_file() and decode(thumb.read_bytes()).width == 400
        assert full.is_file()

    def test_rgba_png_upload_ok(self, cover_dir):
        url = media.save_upload(make_rgba_png(), content_id=8)
        assert url.endswith(".png")
        stem = url.rpartition("/")[2].rpartition(".")[0]
        assert (cover_dir / "thumbs" / f"{stem}.webp").is_file()

    def test_rejects_empty(self, cover_dir):
        with pytest.raises(media.UploadError) as exc:
            media.save_upload(b"", content_id=1)
        assert exc.value.code == "empty"

    def test_rejects_oversize(self, cover_dir):
        data = b"\xff\xd8\xff" + b"\x00" * (6 * 1024 * 1024)
        with pytest.raises(media.UploadError) as exc:
            media.save_upload(data, content_id=1)
        assert exc.value.code == "too_big"

    def test_rejects_non_image(self, cover_dir):
        with pytest.raises(media.UploadError) as exc:
            media.save_upload(b"<?php echo 'x'; ?>", content_id=1)
        assert exc.value.code == "not_image"

    def test_rejects_undecodable(self, cover_dir):
        """魔术字节是 JPEG 但内容损坏：第三道校验（实际解码）拦住。"""
        with pytest.raises(media.UploadError) as exc:
            media.save_upload(JPEG_HEADER + b"\x00" * 64, content_id=1)
        assert exc.value.code == "corrupt"


class TestRemoveCoverFile:
    def test_removes_original_and_variants(self, cover_dir):
        source = put_cover(cover_dir, "cover_old.jpg", make_image_bytes())
        media.generate_variant("thumb", source)
        media.generate_variant("full", source)
        assert media.remove_cover_file("/covers/cover_old.jpg") is True
        assert not source.exists()
        assert not (cover_dir / "thumbs" / "cover_old.webp").exists()
        assert not (cover_dir / "full" / "cover_old.webp").exists()

    def test_ignores_remote_and_foreign_paths(self, cover_dir):
        assert media.remove_cover_file("https://lain.bgm.tv/pic/cover/l/a/b/265.jpg") is False
        assert media.remove_cover_file("/static/other.png") is False
        assert media.remove_cover_file(None) is False
        assert media.remove_cover_file("/covers/../outside.jpg") is False

    def test_missing_file_returns_false(self, cover_dir):
        cover_dir.mkdir(parents=True, exist_ok=True)
        assert media.remove_cover_file("/covers/cover_gone.jpg") is False


# ---------- 模板助手 ----------


class TestTemplateHelpers:
    def test_local_cover_goes_through_media_endpoints(self):
        assert media.thumb_src("/covers/cover_265.jpg") == "/media/thumb/cover_265.jpg"
        assert media.full_src("/covers/cover_265.jpg") == "/media/full/cover_265.jpg"

    def test_remote_url_passes_through(self):
        remote = "https://lain.bgm.tv/pic/cover/lg/a/b/265.jpg"
        assert media.thumb_src(remote) == remote
        assert media.full_src(remote) == remote

    def test_empty_and_weird_values(self):
        assert media.thumb_src(None) == ""
        assert media.full_src("") == ""
        # 名字不合法的本地地址原样返回（端点会 404，前端 onerror 兜底）
        assert media.thumb_src("/covers/a b.jpg") == "/covers/a b.jpg"

    def test_media_base_url_prefix(self, monkeypatch):
        monkeypatch.setattr(settings, "media_base_url", "https://img.example.com/")
        assert media.thumb_src("/covers/cover_265.jpg") == "https://img.example.com/media/thumb/cover_265.jpg"
        # 远端地址不拼基址（它本来就在别的域名上）
        assert media.full_src("https://x/y.jpg") == "https://x/y.jpg"


# ---------- 上传路由与页面 ----------


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    """独立临时库 + 临时封面目录；提供游客 / 管理员两个客户端。"""
    cover_root = tmp_path / "covers"
    monkeypatch.setattr(settings, "cover_storage_dir", str(cover_root))

    engine = create_engine(
        f"sqlite:///{tmp_path / 'media_upload.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(session_factory, admin=True, user=False)
    db = session_factory()
    try:
        db.add(Content(title="待传封面的作品", type="anime", summary="简介", rating=8.0,
                       image_url="/covers/cover_old.jpg"))
        db.commit()
        content_id = db.scalars(select(Content)).one().id
    finally:
        db.close()

    # 预置一张「旧封面」，验证上传成功后会被清理
    put_cover(cover_root, "cover_old.jpg", make_image_bytes(size=(500, 750)))
    media.generate_variant("thumb", cover_root / "cover_old.jpg")

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    guest = TestClient(app)
    admin = TestClient(app)
    login_as_admin(admin)
    try:
        yield SimpleNamespace(guest=guest, admin=admin, content_id=content_id,
                              cover_root=cover_root, session_factory=session_factory)
    finally:
        app.dependency_overrides.pop(get_db, None)


class TestCoverUploadRoute:
    def _upload(self, client, content_id, *, filename="cover.jpg", data=None, next_url=None):
        payload = make_image_bytes() if data is None else data
        form = {"next": next_url} if next_url else {}
        return client.post(
            f"/manage/contents/{content_id}/cover",
            files={"cover": (filename, payload, "image/jpeg")},
            data=form,
            follow_redirects=False,
        )

    def test_admin_upload_updates_image_url_and_cleans_old(self, upload_env):
        resp = self._upload(upload_env.admin, upload_env.content_id)
        assert resp.status_code == 303
        assert "cover=ok" in resp.headers["location"]

        db = upload_env.session_factory()
        try:
            content = db.get(Content, upload_env.content_id)
            assert content.image_url.startswith("/covers/cover_u")
        finally:
            db.close()
        # 旧封面及其缩略图被清理，新封面双尺寸就位
        assert not (upload_env.cover_root / "cover_old.jpg").exists()
        assert not (upload_env.cover_root / "thumbs" / "cover_old.webp").exists()
        new_name = content.image_url.rpartition("/")[2]
        assert (upload_env.cover_root / new_name).is_file()
        stem = new_name.rpartition(".")[0]
        assert (upload_env.cover_root / "thumbs" / f"{stem}.webp").is_file()
        assert (upload_env.cover_root / "full" / f"{stem}.webp").is_file()

    def test_uploaded_cover_served_by_media_endpoint(self, upload_env):
        self._upload(upload_env.admin, upload_env.content_id)
        db = upload_env.session_factory()
        try:
            name = db.get(Content, upload_env.content_id).image_url.rpartition("/")[2]
        finally:
            db.close()
        with TestClient(app) as client:
            resp = client.get(f"/media/thumb/{name}")
        assert resp.status_code == 200
        assert decode(resp.content).width == 400

    def test_guest_is_redirected_to_login(self, upload_env):
        resp = self._upload(upload_env.guest, upload_env.content_id)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    def test_non_image_rejected_with_reason(self, upload_env):
        resp = self._upload(
            upload_env.admin, upload_env.content_id,
            filename="evil.php", data=b"<?php echo 'x'; ?>",
        )
        assert resp.status_code == 303
        assert "cover=error" in resp.headers["location"]
        assert "why=not_image" in resp.headers["location"]
        # 库里的封面不被写脏
        db = upload_env.session_factory()
        try:
            assert db.get(Content, upload_env.content_id).image_url == "/covers/cover_old.jpg"
        finally:
            db.close()

    def test_oversize_rejected_with_reason(self, upload_env):
        resp = self._upload(
            upload_env.admin, upload_env.content_id,
            data=b"\xff\xd8\xff" + b"\x00" * (6 * 1024 * 1024),
        )
        assert resp.status_code == 303
        assert "why=too_big" in resp.headers["location"]

    def test_missing_content_returns_404(self, upload_env):
        resp = self._upload(upload_env.admin, 999999)
        assert resp.status_code == 404

    def test_detail_page_shows_upload_form_only_for_staff(self, upload_env):
        guest_html = upload_env.guest.get(f"/contents/{upload_env.content_id}").text
        assert "上传封面" not in guest_html
        assert f'/manage/contents/{upload_env.content_id}/cover' not in guest_html
        admin_html = upload_env.admin.get(f"/contents/{upload_env.content_id}").text
        assert f'action="/manage/contents/{upload_env.content_id}/cover"' in admin_html
        assert 'enctype="multipart/form-data"' in admin_html
