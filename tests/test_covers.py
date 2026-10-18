"""封面本地化测试：提取 / 下载 / 导入回填 / 回填 CLI / 补列 / 页面渲染。

覆盖：
- extract_cover_url：尺寸偏好（large > common > medium）、异常结构返回 None；
- cover_filename：扩展名白名单、查询串剥离、默认 .jpg；
- download_cover：正常落图并返回站内地址、超限 / 非图片 / HTTP 错误 /
  可重试状态码重试后成功；
- 导入流程：封面随作品落地、下载失败不阻塞入库、--no-cover 与
  COVER_DOWNLOAD_ENABLED=false 时跳过；
- 回填 CLI scripts/fetch_covers.py：只补缺封面的条目、--apply 才写库、
  --force 重下、单条失败不中断；
- 历史库补列：init_db 给旧 contents 表补 image_url；
- 页面渲染：详情页 / 列表页有封面时经 /media 派生图端点渲染 <img>
  （/media/full/... 与 /media/thumb/...），无封面时常驻渐变占位；
  /covers 静态挂载可用。

外部请求全部用假 fetch / 假 opener / 假下载器模拟，不访问真实网络；
数据库用独立临时 SQLite（tmp_path），与 local_dev.db 隔离。
"""

import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app import crud
from app.config import settings
from app.database import Base, build_engine, get_db
from app.main import app
from app.models import Content, ContentFavorite

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# scripts/ 不是包，按文件路径加载回填脚本；它内部会加载 import_bangumi，
# 测试统一复用这同一个实例，保证异常类标识一致（stub 抛出的错误才能被捕获）。
_spec2 = importlib.util.spec_from_file_location(
    "fetch_covers_for_tests", PROJECT_ROOT / "scripts" / "fetch_covers.py"
)
fetch_covers = importlib.util.module_from_spec(_spec2)
sys.modules[_spec2.name] = fetch_covers
_spec2.loader.exec_module(fetch_covers)

import_bangumi = fetch_covers.import_bangumi

# 一张最小的合法 JPEG 头（下载校验只认文件头，不解析整图）。
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32


# ---------- 假网络 ----------


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    """按脚本依次返回预设响应或抛异常，并记录请求。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        if not self._script:
            raise AssertionError("脚本已耗尽，收到了多余的请求")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeResponse(step)


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example/cover.jpg", code, "err", {}, None)


def make_payload(bangumi_id: int, bgm_type: int = 2, images: dict | None = None) -> dict:
    payload = {
        "id": bangumi_id,
        "type": bgm_type,
        "name": f"entry-{bangumi_id}",
        "name_cn": f"条目{bangumi_id}",
        "summary": f"条目 {bangumi_id} 的简介。",
        "rating": {"score": 7.5},
    }
    if images is not None:
        payload["images"] = images
    return payload


def make_stub_fetch(payloads: dict[int, dict]):
    calls: list[int] = []

    def fetch(subject_id: int) -> dict:
        calls.append(subject_id)
        if subject_id not in payloads:
            raise import_bangumi.BangumiAPIError(f"获取条目 {subject_id} 失败：模拟网络故障")
        return payloads[subject_id]

    fetch.calls = calls
    return fetch


def content_by_bangumi(db, bangumi_id: int) -> Content:
    """按 bangumi_id 取唯一条目（init_db 会播种假数据，不能裸 .one()）。"""
    return db.scalars(select(Content).where(Content.bangumi_id == bangumi_id)).one()


def make_recording_downloader(tmp_path: Path, *, local_url: str = "/covers/cover_probe.jpg"):
    """假下载器：写一个标记文件并返回站内地址，记录调用参数。"""
    calls: list[tuple[str, int]] = []

    def download(remote_url: str, bangumi_id: int) -> str:
        calls.append((remote_url, bangumi_id))
        (tmp_path / "downloaded.marker").write_text(remote_url, encoding="utf-8")
        return local_url

    download.calls = calls
    return download


def make_db(tmp_path, name: str = "covers.db"):
    engine = build_engine(f"sqlite:///{tmp_path / name}")
    crud.init_db(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ---------- 提取与文件名 ----------


class TestExtractCoverUrl:
    def test_prefers_medium_for_bandwidth(self):
        """优先 medium(r/800) 而非 large 原图：实测小 12–30%，最终都要压到 ≤400KB WebP。"""
        url = import_bangumi.extract_cover_url(
            {"images": {"large": "https://x/l.jpg", "common": "https://x/c.jpg",
                        "medium": "https://x/m.jpg"}}
        )
        assert url == "https://x/m.jpg"

    def test_falls_back_to_common_then_large(self):
        """medium 缺失时依次退到 common、large。"""
        assert import_bangumi.extract_cover_url(
            {"images": {"large": "https://x/l.jpg", "common": "https://x/c.jpg"}}
        ) == "https://x/c.jpg"
        assert import_bangumi.extract_cover_url(
            {"images": {"large": "https://x/l.jpg"}}
        ) == "https://x/l.jpg"

    def test_falls_back_to_medium(self):
        url = import_bangumi.extract_cover_url({"images": {"medium": "https://x/m.jpg"}})
        assert url == "https://x/m.jpg"

    def test_missing_or_invalid(self):
        assert import_bangumi.extract_cover_url({}) is None
        assert import_bangumi.extract_cover_url({"images": None}) is None
        assert import_bangumi.extract_cover_url({"images": []}) is None
        assert import_bangumi.extract_cover_url({"images": {"large": "not-a-url"}}) is None
        assert import_bangumi.extract_cover_url({"images": {"large": 123}}) is None


class TestCoverFilename:
    def test_keeps_known_extension(self):
        assert import_bangumi.cover_filename("https://lain.bgm.tv/pic/cover/lg/a/b/265.jpg", 265) \
            == "cover_265.jpg"

    def test_strips_query_string(self):
        assert import_bangumi.cover_filename("https://x/y/265.png?token=1", 265) == "cover_265.png"

    def test_unknown_extension_defaults_to_jpg(self):
        assert import_bangumi.cover_filename("https://x/y/265.bin", 265) == "cover_265.jpg"
        assert import_bangumi.cover_filename("https://x/y/noext", 265) == "cover_265.jpg"

    def test_webp_kept(self):
        assert import_bangumi.cover_filename("https://x/y/265.webp", 265) == "cover_265.webp"


# ---------- 下载 ----------


class TestDownloadCover:
    def test_writes_file_and_returns_site_path(self, tmp_path):
        opener = FakeOpener([JPEG_BYTES])
        local = import_bangumi.download_cover(
            "https://lain.bgm.tv/pic/cover/lg/a/b/265.jpg",
            bangumi_id=265,
            storage_dir=tmp_path / "covers",
            opener=opener,
            sleep=lambda _s: None,
        )
        assert local == "/covers/cover_265.jpg"
        written = (tmp_path / "covers" / "cover_265.jpg").read_bytes()
        assert written == JPEG_BYTES
        assert opener.calls == ["https://lain.bgm.tv/pic/cover/lg/a/b/265.jpg"]

    def test_rejects_oversize(self, tmp_path):
        big = b"\xff\xd8\xff" + b"\x00" * 64
        opener = FakeOpener([big])
        with pytest.raises(import_bangumi.CoverDownloadError, match="上限"):
            import_bangumi.download_cover(
                "https://x/265.jpg",
                bangumi_id=265,
                storage_dir=tmp_path,
                opener=opener,
                max_bytes=8,
                sleep=lambda _s: None,
            )
        assert list(tmp_path.glob("*.jpg")) == []

    def test_rejects_non_image(self, tmp_path):
        opener = FakeOpener([b"<html>429 Too Many Requests</html>"])
        with pytest.raises(import_bangumi.CoverDownloadError, match="不是图片"):
            import_bangumi.download_cover(
                "https://x/265.jpg",
                bangumi_id=265,
                storage_dir=tmp_path,
                opener=opener,
                sleep=lambda _s: None,
            )
        assert list(tmp_path.glob("*.jpg")) == []

    def test_http_error_fails_without_retry(self, tmp_path):
        opener = FakeOpener([http_error(404)])
        with pytest.raises(import_bangumi.CoverDownloadError, match="HTTP 404"):
            import_bangumi.download_cover(
                "https://x/265.jpg",
                bangumi_id=265,
                storage_dir=tmp_path,
                opener=opener,
                sleep=lambda _s: None,
            )

    def test_retries_on_503(self, tmp_path):
        opener = FakeOpener([http_error(503), JPEG_BYTES])
        local = import_bangumi.download_cover(
            "https://x/265.jpg",
            bangumi_id=265,
            storage_dir=tmp_path,
            opener=opener,
            retries=1,
            sleep=lambda _s: None,
        )
        assert local == "/covers/cover_265.jpg"
        assert len(opener.calls) == 2

    def test_rejects_bad_url(self, tmp_path):
        opener = FakeOpener([])
        with pytest.raises(import_bangumi.CoverDownloadError, match="不合法"):
            import_bangumi.download_cover(
                "ftp://x/265.jpg",
                bangumi_id=265,
                storage_dir=tmp_path,
                opener=opener,
            )


# ---------- 导入回填 ----------

COVER_IMAGES = {
    "large": "https://lain.bgm.tv/pic/cover/lg/a/b/265.jpg",
    "common": "https://lain.bgm.tv/pic/cover/md/a/b/265.jpg",
}


class TestImportWithCover:
    def test_cover_lands_with_content(self, tmp_path):
        engine, session_factory = make_db(tmp_path)
        try:
            downloader = make_recording_downloader(tmp_path)
            summary = import_bangumi.run_import(
                [265],
                session_factory=session_factory,
                apply=True,
                fetch=make_stub_fetch({265: make_payload(265, images=COVER_IMAGES)}),
                cover_downloader=downloader,
            )
            assert summary.imported == 1
            outcome = summary.outcomes[0]
            assert "封面已本地化" in outcome.reason
            assert "/covers/cover_probe.jpg" in outcome.reason

            db = session_factory()
            try:
                content = content_by_bangumi(db, 265)
                assert content.image_url == "/covers/cover_probe.jpg"
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_download_failure_does_not_block_import(self, tmp_path):
        engine, session_factory = make_db(tmp_path)
        try:
            def failing(remote_url, bangumi_id):
                raise import_bangumi.CoverDownloadError("模拟下载故障")

            summary = import_bangumi.run_import(
                [265],
                session_factory=session_factory,
                apply=True,
                fetch=make_stub_fetch({265: make_payload(265, images=COVER_IMAGES)}),
                cover_downloader=failing,
            )
            assert summary.imported == 1  # 作品照常入库
            assert "封面下载失败" in summary.outcomes[0].reason

            db = session_factory()
            try:
                assert content_by_bangumi(db, 265).image_url is None
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_no_cover_flag_skips_download(self, tmp_path):
        engine, session_factory = make_db(tmp_path)
        try:
            summary = import_bangumi.run_import(
                [265],
                session_factory=session_factory,
                apply=True,
                fetch=make_stub_fetch({265: make_payload(265, images=COVER_IMAGES)}),
                cover_downloader=False,
            )
            assert summary.imported == 1
            assert "关闭了封面下载" in summary.outcomes[0].reason
            db = session_factory()
            try:
                assert content_by_bangumi(db, 265).image_url is None
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_payload_without_images_keeps_image_url_empty(self, tmp_path):
        engine, session_factory = make_db(tmp_path)
        try:
            summary = import_bangumi.run_import(
                [265],
                session_factory=session_factory,
                apply=True,
                fetch=make_stub_fetch({265: make_payload(265)}),
                cover_downloader=make_recording_downloader(tmp_path),
            )
            assert summary.imported == 1
            assert "封面" not in summary.outcomes[0].reason
            db = session_factory()
            try:
                assert content_by_bangumi(db, 265).image_url is None
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_auto_downloader_respects_settings_switch(self, tmp_path, monkeypatch):
        """COVER_DOWNLOAD_ENABLED=false 时 run_import 自动不构造下载器。"""
        engine, session_factory = make_db(tmp_path)
        try:
            monkeypatch.setattr(settings, "cover_download_enabled", False)
            summary = import_bangumi.run_import(
                [265],
                session_factory=session_factory,
                apply=True,
                fetch=make_stub_fetch({265: make_payload(265, images=COVER_IMAGES)}),
            )
            assert summary.imported == 1
            db = session_factory()
            try:
                assert content_by_bangumi(db, 265).image_url is None
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_auto_downloader_downloads_when_enabled(self, tmp_path, monkeypatch):
        """开关打开时自动构造的下载器真的会把图落到配置目录并回写地址。"""
        engine, session_factory = make_db(tmp_path)
        cover_dir = tmp_path / "auto-covers"
        try:
            monkeypatch.setattr(settings, "cover_download_enabled", True)
            monkeypatch.setattr(settings, "cover_storage_dir", str(cover_dir))
            monkeypatch.setattr(import_bangumi, "build_opener", lambda proxy: FakeOpener([JPEG_BYTES]))

            summary = import_bangumi.run_import(
                [265],
                session_factory=session_factory,
                apply=True,
                fetch=make_stub_fetch({265: make_payload(265, images=COVER_IMAGES)}),
                sleep=lambda _s: None,
            )
            assert summary.imported == 1
            assert (cover_dir / "cover_265.jpg").read_bytes() == JPEG_BYTES
            db = session_factory()
            try:
                assert content_by_bangumi(db, 265).image_url == "/covers/cover_265.jpg"
            finally:
                db.close()
        finally:
            engine.dispose()


# ---------- 回填 CLI ----------


class TestFetchCoversCli:
    def _seed_contents(self, session_factory):
        db = session_factory()
        try:
            db.add(Content(title="有封面", type="anime", summary="s", rating=7.0,
                           bangumi_id=265, image_url="/covers/cover_265.jpg"))
            db.add(Content(title="缺封面", type="anime", summary="s", rating=7.0,
                           bangumi_id=975))
            db.add(Content(title="无BID", type="anime", summary="s", rating=7.0))
            db.commit()
        finally:
            db.close()

    def test_backfill_writes_only_missing(self, tmp_path):
        engine, session_factory = make_db(tmp_path, "backfill.db")
        try:
            self._seed_contents(session_factory)
            payloads = {265: make_payload(265, images=COVER_IMAGES),
                        975: make_payload(975, 4, images={"common": "https://x/975.png"})}
            downloader = make_recording_downloader(tmp_path, local_url="/covers/cover_975.png")

            code = fetch_covers.main(
                ["--apply", "--scope", "all", "--database-url", f"sqlite:///{tmp_path / 'backfill.db'}"],
                fetch=make_stub_fetch(payloads),
                cover_downloader=downloader,
            )
            assert code == 0
            assert downloader.calls == [("https://x/975.png", 975)]

            db = session_factory()
            try:
                rows = {row.title: row.image_url for row in db.scalars(select(Content))}
                assert rows["有封面"] == "/covers/cover_265.jpg"  # 未被动过
                assert rows["缺封面"] == "/covers/cover_975.png"
                assert rows["无BID"] is None
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_preview_does_not_write_db(self, tmp_path):
        engine, session_factory = make_db(tmp_path, "preview.db")
        try:
            self._seed_contents(session_factory)
            code = fetch_covers.main(
                ["--scope", "all", "--database-url", f"sqlite:///{tmp_path / 'preview.db'}"],
                fetch=make_stub_fetch({975: make_payload(975, 4, images={"common": "https://x/975.png"})}),
                cover_downloader=make_recording_downloader(tmp_path),
            )
            assert code == 0
            db = session_factory()
            try:
                rows = {row.title: row.image_url for row in db.scalars(select(Content))}
                assert rows["缺封面"] is None  # 下图了但没写库
            finally:
                db.close()
        finally:
            engine.dispose()

    def test_force_redownloads_existing(self, tmp_path):
        engine, session_factory = make_db(tmp_path, "force.db")
        try:
            self._seed_contents(session_factory)
            downloader = make_recording_downloader(tmp_path, local_url="/covers/cover_265.jpg")
            code = fetch_covers.main(
                ["--apply", "--force", "--scope", "all", "--database-url", f"sqlite:///{tmp_path / 'force.db'}"],
                fetch=make_stub_fetch({265: make_payload(265, images=COVER_IMAGES),
                                       975: make_payload(975, 4, images={"common": "https://x/975.png"})}),
                cover_downloader=downloader,
            )
            assert code == 0
            assert [bid for _, bid in downloader.calls] == [265, 975]
        finally:
            engine.dispose()

    def test_failure_returns_nonzero(self, tmp_path, capsys):
        engine, session_factory = make_db(tmp_path, "fail.db")
        try:
            self._seed_contents(session_factory)
            code = fetch_covers.main(
                ["--apply", "--scope", "all", "--database-url", f"sqlite:///{tmp_path / 'fail.db'}"],
                fetch=make_stub_fetch({}),  # 全部取不到 -> BangumiAPIError
                cover_downloader=make_recording_downloader(tmp_path),
            )
            assert code == 1
            assert "失败" in capsys.readouterr().out
        finally:
            engine.dispose()


# ---------- 历史库补列 ----------


class TestLegacySchemaUpgrade:
    def test_init_db_adds_image_url_column(self, tmp_path):
        """封面上线前创建的 contents 表（无 image_url）启动时自动补列。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE contents ("
                        "id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL,"
                        " type VARCHAR(20) NOT NULL, summary TEXT NOT NULL,"
                        " rating FLOAT NOT NULL, bangumi_id INTEGER,"
                        " source_url VARCHAR(500))"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO contents (id, title, type, summary, rating)"
                        " VALUES (1, '老数据', 'anime', '简介', 8.0)"
                    )
                )
            added = crud.ensure_content_image_column(engine)
            assert added == ["image_url"]
            # 年份 / 客观信息列同样走幂等补列（老库没有这些列）
            assert crud.ensure_content_year_column(engine) == ["year"]
            assert crud.ensure_content_year_column(engine) == []
            assert set(crud.ensure_content_meta_columns(engine)) == {
                "media", "source_material", "episodes", "meta_fetched_at",
            }
            assert crud.ensure_content_meta_columns(engine) == []
            # 封面按需下载列（批量导入只给高热作品下封面，低热作品记远端地址）
            assert crud.ensure_content_remote_image_column(engine) == ["remote_image_url"]
            assert crud.ensure_content_remote_image_column(engine) == []

            # 其余历史列（series_id / book_kind / …）不逐个点名：这里直接跑
            # init_db —— 就是应用启动时真正走的那条路径。手写补列清单的话，
            # contents 每加一列都得回来改一次，漏一个就让下面 db.get(Content, 1)
            # 因缺列报错（并行会话加列时尤其容易踩）。
            crud.init_db(engine)
            columns = {col["name"] for col in inspect(engine).get_columns("contents")}
            assert {"image_url", "year", "remote_image_url", "series_id", "book_kind"} <= columns

            db = sessionmaker(bind=engine, expire_on_commit=False)()
            try:
                content = db.get(Content, 1)
                assert content.image_url is None  # 旧行回退渐变占位，不被写脏值
                assert content.remote_image_url is None  # 没记远端地址，按需下载会跳过
            finally:
                db.close()
        finally:
            engine.dispose()


# ---------- 页面渲染与静态挂载 ----------


class TestCoverPages:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        engine, session_factory = make_db(tmp_path, "pages.db")
        db = session_factory()
        try:
            db.add(Content(title="有封面的作品", type="anime", summary="简介内容", rating=8.0,
                           bangumi_id=265, image_url="/covers/cover_265.jpg"))
            db.add(Content(title="无封面的作品", type="anime", summary="简介内容", rating=7.0))
            db.commit()
        finally:
            db.close()

        def override_get_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        yield session_factory
        app.dependency_overrides.clear()
        engine.dispose()

    def test_detail_page_renders_cover_image(self, env):
        db = env()
        try:
            content_id = db.scalars(
                select(Content).where(Content.title == "有封面的作品")
            ).one().id
        finally:
            db.close()
        with TestClient(app) as client:
            html = client.get(f"/contents/{content_id}").text
        # 有封面：图片层走 /media/full/ 压缩端点，叠在常驻占位层之上
        assert '<img class="detail-cover" src="/media/full/cover_265.jpg"' in html
        assert 'class="detail-cover" aria-hidden="true"' in html

    def test_detail_page_without_cover_renders_placeholder_only(self, env):
        db = env()
        try:
            content_id = db.scalars(
                select(Content).where(Content.title == "无封面的作品")
            ).one().id
        finally:
            db.close()
        with TestClient(app) as client:
            html = client.get(f"/contents/{content_id}").text
        # 无封面：只有渐变占位层，不渲染图片（占位层常驻，有封面时被盖住）
        assert 'class="detail-cover" aria-hidden="true"' in html
        assert '<img class="detail-cover"' not in html

    def test_contents_page_renders_cover_image(self, env):
        with TestClient(app) as client:
            html = client.get("/contents").text
        # 列表卡片：缩略图走 /media/thumb/ 端点，加载失败 onerror 隐藏回退占位
        assert '<img class="work-cover" src="/media/thumb/cover_265.jpg"' in html
        assert 'onerror=' in html

    def test_covers_static_mount_serves_file(self):
        """/covers 挂载生效：能拿到放进去的封面文件（探测后清理）。"""
        cover_dir = Path(settings.cover_storage_dir)
        if not cover_dir.is_absolute():
            cover_dir = PROJECT_ROOT / cover_dir
        cover_dir.mkdir(parents=True, exist_ok=True)
        probe = cover_dir / "cover_mount_probe.jpg"
        probe.write_bytes(JPEG_BYTES)
        try:
            with TestClient(app) as client:
                resp = client.get("/covers/cover_mount_probe.jpg")
            assert resp.status_code == 200
            assert resp.content == JPEG_BYTES
            missing = client.get("/covers/definitely-not-here.jpg")
            assert missing.status_code == 404
        finally:
            probe.unlink(missing_ok=True)


class TestCoverScope:
    """补图范围：默认只补有互动的作品，其次高分（首页推荐），--scope all 才全量。"""

    def _factory(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'scope.db'}")
        Base.metadata.create_all(engine)
        return sessionmaker(bind=engine, expire_on_commit=False)

    def test_scope_filters_by_engagement_and_rating(self, tmp_path):
        factory = self._factory(tmp_path)
        db = factory()
        try:
            touched = Content(title="收藏过", type="anime", summary="x", rating=6.0, bangumi_id=1)
            untouched = Content(title="没动过", type="anime", summary="x", rating=7.0, bangumi_id=2)
            top_rated = Content(title="高分未动", type="anime", summary="x", rating=9.0, bangumi_id=3)
            db.add_all([touched, untouched, top_rated])
            db.commit()
            db.add(ContentFavorite(user_id=1, content_id=touched.id))
            db.commit()

            engaged = fetch_covers.list_pending_contents(db, force=False, scope="engaged")
            assert [row.id for row in engaged] == [touched.id]

            top = fetch_covers.list_pending_contents(db, force=False, scope="top", top=1)
            assert {row.id for row in top} == {touched.id, top_rated.id}

            every = fetch_covers.list_pending_contents(db, force=False, scope="all")
            assert len(every) == 3

            limited = fetch_covers.list_pending_contents(db, force=False, scope="all", limit=2)
            assert len(limited) == 2
        finally:
            db.close()

    def test_scope_home_and_content_ids(self, tmp_path):
        """scope=home 只补首页口味精选；--content-id 精确点名并忽略 scope。"""
        factory = self._factory(tmp_path)
        db = factory()
        try:
            daily = Content(title="贴贴日常", type="anime", summary="x", rating=8.0, bangumi_id=11)
            violent = Content(title="热血战斗", type="anime", summary="x", rating=9.5, bangumi_id=12)
            db.add_all([daily, violent])
            db.commit()
            crud.replace_content_tags(db, daily.id, ["日常", "百合"])
            crud.replace_content_tags(db, violent.id, ["战斗", "热血"])

            home = fetch_covers.list_pending_contents(db, force=False, scope="home")
            assert [row.id for row in home] == [daily.id]  # 无本命标签的不在首页范围

            picked = fetch_covers.list_pending_contents(
                db, force=False, scope="engaged", content_ids=[violent.id]
            )
            assert [row.id for row in picked] == [violent.id]

            daily.image_url = "/covers/x.jpg"
            db.commit()
            assert all(
                row.id != daily.id
                for row in fetch_covers.list_pending_contents(db, force=False, scope="home")
            )
            # --force 时已有封面的也重新进队列
            forced = fetch_covers.list_pending_contents(
                db, force=True, scope="home", content_ids=[daily.id]
            )
            assert [row.id for row in forced] == [daily.id]
        finally:
            db.close()
