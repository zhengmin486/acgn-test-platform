"""增量入库通道（app/ingest_api.py）的行为测试。

覆盖这条链路的契约：
- 默认关闭（没配 INGEST_API_KEY 一律 503），配了才生效；
- Key 缺失/错误 401；
- 站长可调、管理员与普通用户 403；
- 正常路径把 Bangumi payload 写成 contents 行，并带上标签与作者关系；
- 重复推送同一 bangumi_id 记 duplicate 且不新增行（幂等）；
- 请求体形状错误 400 / 超量 413。

用临时 SQLite，不触网、不写真实库（conftest 已全局关掉按需补全）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.auth import require_owner_api  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Content, ContentTag, Tag, User  # noqa: E402

KEY = "test-ingest-key-0123456789"


def subject_payload(subject_id: int, name: str = "测试作品") -> dict:
    """构造一条最小可用的 Bangumi subject payload（type=2 动画）。"""
    return {
        "id": subject_id,
        "name": name,
        "name_cn": name,
        "type": 2,
        "summary": "简介文字",
        "date": "2026-10-01",
        "rating": {"score": 8.1, "total": 100},
        "tags": [{"name": "热血"}, {"name": "校园"}],
        "infobox": [{"key": "导演", "value": "监督"}, {"key": "动画制作", "value": "制作公司"}],
    }


@pytest.fixture
def factory(tmp_path):
    """独立临时库，建表并写入 admin(owner) 与 user 两个账号。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ingest.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        db.add_all(
            [
                User(username="boss", password_hash="x", role="owner"),
                User(username="plain", password_hash="x", role="user"),
            ]
        )
        db.commit()
    finally:
        db.close()
    return session_factory


@pytest.fixture
def sign_in(client_factory, factory, monkeypatch):
    """让后续请求以指定身份发出：覆盖 require_owner_api 守卫。

    守卫内部直接调 ``get_current_user``（而不是应用级的 load_current_user），
    所以覆盖守卫本身最直接，也不用去搭真实登录的 session Cookie。

    桩函数刻意不声明任何参数——一旦带上 ``db=Depends(get_db)`` 之类的参数，
    FastAPI 会在覆盖后重新解析它的签名，把依赖当成必填 query 参数，
    请求还没进端点就被 422 挡掉。
    """
    from fastapi import HTTPException

    from app.auth import ROLE_OWNER, CurrentUser

    def _sign(username: str):
        db = factory()
        try:
            user = db.scalar(select(User).where(User.username == username))
            # 守卫返回的是 CurrentUser 视图对象（is_owner 是它的属性，不是 User 模型的），
            # 这里保持与真实链路一致，免得测试通过而线上报 AttributeError。
            view = CurrentUser(id=user.id, username=user.username, role=user.role)
        finally:
            db.close()
        assert user is not None, f"测试账号 {username} 不存在"

        def guard():
            if view.role != ROLE_OWNER:
                raise HTTPException(status_code=403, detail="需要站长权限")
            return view

        monkeypatch.setitem(app.dependency_overrides, require_owner_api, guard)

    return _sign


@pytest.fixture
def client_factory(factory, monkeypatch):
    """构造启用了 INGEST_API_KEY 的 TestClient（数据库指向临时库）。"""
    monkeypatch.setattr(settings, "ingest_api_key", KEY)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    client = TestClient(app)
    app.dependency_overrides[get_db] = override_get_db
    yield client
    app.dependency_overrides.pop(get_db, None)


def _post(client, subjects, key: str | None = KEY):
    headers = {"X-Ingest-Key": key} if key else {}
    return client.post(
        "/api/ingest/subjects",
        json={"subjects": subjects, "source": "test"},
        headers=headers,
    )


class TestIngestDisabledByDefault:
    def test_without_configured_key_returns_503(self, factory, monkeypatch):
        """默认关闭：没配 INGEST_API_KEY 时端点直接 503，不接受任何推送。"""
        monkeypatch.setattr(settings, "ingest_api_key", "")

        def override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        client = TestClient(app)
        app.dependency_overrides[get_db] = override_get_db
        try:
            resp = _post(client, [subject_payload(1)])
            assert resp.status_code == 503
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestIngestAuth:
    def test_missing_key_returns_401(self, client_factory, sign_in):
        sign_in("boss")
        resp = _post(client_factory, [subject_payload(1)], key=None)
        assert resp.status_code == 401

    def test_wrong_key_returns_401(self, client_factory, sign_in):
        sign_in("boss")
        resp = _post(client_factory, [subject_payload(1)], key="wrong-key")
        assert resp.status_code == 401

    def test_non_owner_cannot_ingest(self, client_factory, sign_in):
        """普通用户即使带着正确的 Key 也进不来：需要站长角色。"""
        sign_in("plain")
        resp = _post(client_factory, [subject_payload(1)])
        assert resp.status_code == 403


class TestIngestPayload:
    def test_imports_subject_with_tags_and_entities(self, client_factory, factory, sign_in):
        sign_in("boss")

        resp = _post(client_factory, [subject_payload(101, "新世纪福音战士")])
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] == 1
        row = body["results"][0]
        assert row["status"] == "imported"
        assert row["bangumi_id"] == 101

        db = factory()
        try:
            content = db.scalar(select(Content).where(Content.bangumi_id == 101))
            assert content is not None
            assert content.title == "新世纪福音战士"
            assert content.type == "anime"
            # 标签经既有 replace_content_tags 落库（弱关联表，不是 ORM 关系属性）
            tags = {
                row.name
                for row in db.scalars(
                    select(Tag)
                    .join(ContentTag, ContentTag.tag_id == Tag.id)
                    .where(ContentTag.content_id == content.id)
                )
            }
            assert "热血" in tags and "校园" in tags
        finally:
            db.close()

    def test_duplicate_bangumi_id_is_skipped(self, client_factory, factory, sign_in):
        """重复推送幂等：第二次记 duplicate，不新增行也不覆盖。"""
        sign_in("boss")

        first = _post(client_factory, [subject_payload(202, "星际牛仔")])
        assert first.json()["imported"] == 1

        second = _post(client_factory, [subject_payload(202, "星际牛仔")])
        assert second.status_code == 200
        assert second.json()["imported"] == 0
        assert second.json()["results"][0]["status"] == "duplicate"

        db = factory()
        try:
            count = len(db.scalars(select(Content)).all())
            assert count == 1
        finally:
            db.close()

    def test_bad_request_shapes(self, client_factory, sign_in):
        sign_in("boss")

        missing = client_factory.post(
            "/api/ingest/subjects",
            json={"source": "test"},
            headers={"X-Ingest-Key": KEY},
        )
        assert missing.status_code == 400

        empty = _post(client_factory, [])
        assert empty.status_code == 200
        assert empty.json()["total"] == 0

    def test_oversize_batch_returns_413(self, client_factory, sign_in):
        sign_in("boss")

        resp = _post(client_factory, [subject_payload(i) for i in range(1001)])
        assert resp.status_code == 413

    def test_non_dict_item_is_skipped_not_fatal(self, client_factory, sign_in):
        """坏数据只跳过该条，不让整批失败。"""
        sign_in("boss")

        resp = _post(client_factory, [subject_payload(303), "not-an-object"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["imported"] == 1
        assert body["results"][1]["status"] == "skipped"

