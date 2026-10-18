"""封面按需下载（app/media.py 的 fetch_cover_on_demand）的行为测试。

存在的理由：批量导入只给高热作品下封面，低热作品把地址存进
``contents.remote_image_url``，等详情页第一次被打开时才补齐。
这条链路的约定：

- 只允许 Bangumi 自家 CDN（库里被塞进任意地址时不能当下载器用）；
- 下载失败一律静默返回 None——封面不是关键路径，不能让详情页报错或变慢；
- 成功后回填 image_url 并清掉 remote_image_url（不重复下载）。

全部用桩替换 urlopen，不触网。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import crud, media  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Base  # noqa: E402

# 一张 1x1 的合法 JPEG（不是真封面，只用于通过魔术字节校验）。
TINY_JPEG = io.BytesIO()
Image.new("RGB", (1, 1), (10, 20, 30)).save(TINY_JPEG, format="JPEG")
JPEG_BYTES = TINY_JPEG.getvalue()


class FakeContent:
    """鸭子类型的 Content 替身，只带 fetch_cover_on_demand 用到的字段。"""

    def __init__(self, remote=None, image=None, bangumi_id=None, cid=1):
        self.remote_image_url = remote
        self.image_url = image
        self.bangumi_id = bangumi_id
        self.id = cid


@pytest.fixture
def cover_root(tmp_path, monkeypatch):
    """把封面目录指到临时目录，不碰真实 app/static/covers。"""
    root = tmp_path / "covers"
    root.mkdir()
    monkeypatch.setattr(media, "cover_root", lambda: root)
    monkeypatch.setattr(media.settings, "cover_thumb_width", 40)
    monkeypatch.setattr(media.settings, "cover_full_max_bytes", 50_000)
    return root


class _StubOpener:
    def __init__(self, body=None, exc=None):
        self.body = body
        self.exc = exc
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        if self.exc is not None:
            raise self.exc
        return _Resp(self.body or b"")


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, opener):
    monkeypatch.setattr(media.urllib.request, "urlopen", opener.open)


class TestHappyPath:
    def test_downloads_and_backfills(self, cover_root, monkeypatch):
        """下载成功：落盘原图、回填 image_url、清掉 remote_image_url。"""
        opener = _StubOpener(body=JPEG_BYTES)
        _patch_urlopen(monkeypatch, opener)

        item = FakeContent(remote="https://lain.bgm.tv/pic/cover/l/ab/cd/265_X.jpg",
                           bangumi_id=265)
        result = media.fetch_cover_on_demand(item)

        assert result == "/covers/cover_265.jpg"
        assert item.image_url == "/covers/cover_265.jpg"
        assert item.remote_image_url is None
        assert (cover_root / "cover_265.jpg").is_file()
        # 派生图一并生成，列表页与详情页都能直接用
        assert (cover_root / "variants" / "cover_265.webp").is_file() or any(
            p.name.endswith(".webp") for p in cover_root.rglob("*.webp")
        )

    def test_skips_when_local_cover_exists(self, cover_root, monkeypatch):
        """已经有本地封面就不再下载，也不该清掉 remote_image_url。"""
        opener = _StubOpener(body=JPEG_BYTES)
        _patch_urlopen(monkeypatch, opener)

        item = FakeContent(
            remote="https://lain.bgm.tv/x.jpg", image="/covers/cover_1.jpg", bangumi_id=1
        )
        result = media.fetch_cover_on_demand(item)

        assert result == "/covers/cover_1.jpg"
        assert opener.calls == []
        assert item.remote_image_url == "https://lain.bgm.tv/x.jpg"

    def test_noop_without_remote_url(self, cover_root, monkeypatch):
        """没记远端地址就直接返回，不发起任何请求。"""
        opener = _StubOpener(body=JPEG_BYTES)
        _patch_urlopen(monkeypatch, opener)

        assert media.fetch_cover_on_demand(FakeContent(remote=None)) is None
        assert opener.calls == []


class TestSafety:
    def test_rejects_non_bangumi_host(self, cover_root, monkeypatch):
        """非 Bangumi 域名一律拒绝：不能把库里存的地址当任意下载器。"""
        opener = _StubOpener(body=JPEG_BYTES)
        _patch_urlopen(monkeypatch, opener)

        item = FakeContent(remote="https://evil.example.com/x.jpg", bangumi_id=1)
        assert media.fetch_cover_on_demand(item) is None
        assert opener.calls == []

    def test_rejects_oversize_body(self, cover_root, monkeypatch):
        """超过大小上限的响应丢弃，防止异常响应撑爆磁盘。"""
        opener = _StubOpener(body=b"x" * (media._ON_DEMAND_MAX_BYTES + 1))
        _patch_urlopen(monkeypatch, opener)

        item = FakeContent(remote="https://lain.bgm.tv/pic/cover/l/a/b/1_x.jpg", bangumi_id=1)
        assert media.fetch_cover_on_demand(item) is None
        assert not (cover_root / "cover_1.jpg").exists()

    def test_rejects_non_image_body(self, cover_root, monkeypatch):
        """返回的不是图片就丢弃，不落盘。"""
        opener = _StubOpener(body=b"<html>404</html>")
        _patch_urlopen(monkeypatch, opener)

        item = FakeContent(remote="https://lain.bgm.tv/pic/cover/l/a/b/1_x.jpg", bangumi_id=1)
        assert media.fetch_cover_on_demand(item) is None
        assert not (cover_root / "cover_1.jpg").exists()


class TestFailureIsSilent:
    @pytest.mark.parametrize(
        "exc",
        [OSError("远程主机强迫关闭"), TimeoutError("timed out")],
    )
    def test_network_failure_returns_none(self, cover_root, monkeypatch, exc):
        """网络失败静默降级——详情页不能因为封面报错。"""
        opener = _StubOpener(exc=exc)
        _patch_urlopen(monkeypatch, opener)

        item = FakeContent(remote="https://lain.bgm.tv/pic/cover/l/a/b/1_x.jpg", bangumi_id=1)
        assert media.fetch_cover_on_demand(item) is None
        assert item.image_url is None
        # 远端地址保留，下次访问还能再试
        assert item.remote_image_url == "https://lain.bgm.tv/pic/cover/l/a/b/1_x.jpg"


class TestSchemaMigration:
    def test_remote_image_column_is_added(self, tmp_path):
        """老库要能幂等补出 remote_image_url 列（项目没有 Alembic，只靠 CREATE_ALL + ALTER）。

        不用 DROP COLUMN 造老库——部分 SQLite 版本不支持；直接用原生 SQL 建一张
        只含必要列的 contents 表，更接近真实的历史库形态。
        """
        engine = build_engine(f"sqlite:///{tmp_path / 'mig.db'}")
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE contents ("
                " id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,"
                " type VARCHAR(20), summary TEXT, rating FLOAT)"
            )

        insp = __import__("sqlalchemy").inspect(engine)
        assert "remote_image_url" not in {c["name"] for c in insp.get_columns("contents")}

        added = crud.ensure_content_remote_image_column(engine)
        assert added == ["remote_image_url"]

        insp = __import__("sqlalchemy").inspect(engine)
        assert "remote_image_url" in {c["name"] for c in insp.get_columns("contents")}

        # 幂等：再跑一次不重复加
        assert crud.ensure_content_remote_image_column(engine) == []

    def test_missing_contents_table_is_noop(self, tmp_path):
        """contents 表还不存在时安全返回，不报错。"""
        engine = build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        assert crud.ensure_content_remote_image_column(engine) == []