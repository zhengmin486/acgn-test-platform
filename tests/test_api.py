"""接口与页面的功能测试（默认库为播种后的本地 SQLite，不依赖 MySQL）。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.crud import seed_if_empty
from app.database import Base, get_db
from app.fake_data import FAKE_CONTENTS
from app.main import app

client = TestClient(app)

REQUIRED_FIELDS = {"id", "title", "type", "summary", "rating"}


def test_health():
    """健康检查：状态码 + 精确响应体。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "acgn-test-platform"}


def test_list_contents():
    """内容列表：total 与 items 一致，且关键业务字段类型正确。"""
    resp = client.get("/api/contents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["items"]) > 0
    for item in body["items"]:
        assert REQUIRED_FIELDS <= set(item)
        assert isinstance(item["id"], int)
        assert isinstance(item["title"], str) and item["title"]
        assert isinstance(item["type"], str) and item["type"]
        assert isinstance(item["summary"], str) and item["summary"]
        assert isinstance(item["rating"], (int, float))


@pytest.mark.parametrize(
    ("content_type", "expected_ids"),
    [
        ("anime", [1, 6]),
        ("comic", [2]),
        ("game", [3, 5]),
        ("novel", [4]),
        ("ANIME", [1, 6]),
    ],
)
def test_list_contents_by_type(content_type, expected_ids, tmp_path):
    """按 ACGN 类型筛选；类型标识不区分大小写。

    开发库中的内容会随真实导入增长（如 Bangumi 导入的作品），
    因此改用只含 6 条种子数据的受控临时库验证筛选逻辑，不依赖库内真实数据。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'type_filter.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    with TestingSession() as db:
        seed_if_empty(db)

    app.dependency_overrides[get_db] = override_get_db
    try:
        resp = client.get("/api/contents", params={"type": content_type})

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == len(expected_ids)
        assert [item["id"] for item in body["items"]] == expected_ids
        assert all(item["type"] == content_type.lower() for item in body["items"])
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("content_type", ["unknown", ""])
def test_list_contents_invalid_type(content_type):
    """未知类型和空类型均返回 400。"""
    resp = client.get("/api/contents", params={"type": content_type})

    assert resp.status_code == 400
    assert "类型不合法" in resp.json()["detail"]


def test_get_content_detail():
    """详情：完整字段与种子数据条目逐一相等，而不只是 id 相同。"""
    resp = client.get("/api/contents/1")
    assert resp.status_code == 200
    assert resp.json() == FAKE_CONTENTS[0]


def test_get_content_not_found():
    """不存在的 id -> 404，并返回明确的中文错误信息。"""
    resp = client.get("/api/contents/99999")
    assert resp.status_code == 404
    assert "内容不存在" in resp.json()["detail"]


def test_get_content_invalid_id_format():
    """路径参数不是整数 -> 422 参数校验失败，而不是 500 或 404。"""
    resp = client.get("/api/contents/not-a-number")
    assert resp.status_code == 422


@pytest.mark.parametrize("content_id", [0, -1])
def test_get_content_boundary_ids(content_id):
    """格式合法但数据中不存在的边界 id（0、负数）-> 404。"""
    resp = client.get(f"/api/contents/{content_id}")
    assert resp.status_code == 404


def test_search_hit():
    """标题关键词命中：状态码、条数一致，且每条结果都包含关键词。"""
    resp = client.get("/api/contents/search", params={"q": "星海"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == len(body["items"]) >= 1
    assert all("星海" in (i["title"] + i["summary"]) for i in body["items"])


def test_search_case_insensitive():
    """搜索契约：关键词不区分大小写（小写 cyber 命中 id=5 的 Cyber Shrine）。"""
    resp = client.get("/api/contents/search", params={"q": "cyber"})
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == [5]


def test_search_matches_summary_only_keyword():
    """仅出现在简介中的关键词也能命中（"乐队"只在 id=6 的简介里）。"""
    resp = client.get("/api/contents/search", params={"q": "乐队"})
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()["items"]] == [6]


def test_search_no_result():
    """关键词无匹配 -> 200 + 空列表，而非 404。"""
    resp = client.get("/api/contents/search", params={"q": "绝不存在的关键词xyz"})
    assert resp.status_code == 200
    assert resp.json() == {
        "total": 0, "page": 1, "page_size": 20, "total_pages": 0,
        "type": None, "items": [],
    }


def test_search_empty_q_rejected():
    """缺参、空串、纯空白 -> 统一返回 400。"""
    for params in ({}, {"q": ""}, {"q": "   "}):
        resp = client.get("/api/contents/search", params=params)
        assert resp.status_code == 400


def test_empty_database_api_behaviour(tmp_path):
    """可控数据：把 get_db 覆盖为独立临时 SQLite，在空库上验证 API 契约。

    与默认开发库完全隔离，结束时还原依赖，不影响其他用例。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api_empty.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        assert client.get("/api/contents").json() == {"total": 0, "items": []}
        assert client.get("/api/contents/1").status_code == 404
        search = client.get("/api/contents/search", params={"q": "星海"})
        assert search.status_code == 200
        assert search.json() == {
            "total": 0, "page": 1, "page_size": 20, "total_pages": 0,
            "type": None, "items": [],
        }
        assert client.get("/").status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_index_page():
    """首页（门户）：渲染分类/推荐区块与条目链接，不再有全量列表表格。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "分类浏览" in resp.text and "推荐作品" in resp.text
    assert "内容列表" not in resp.text  # 全量列表已移到「全部作品」页（/contents）
    assert "/contents/" in resp.text  # 推荐位链到详情


def test_detail_page():
    """详情页：渲染标题与简介。"""
    resp = client.get("/contents/1")
    assert resp.status_code == 200
    assert "星海航线" in resp.text


def test_detail_page_not_found():
    """详情页 404：状态码正确且渲染了 404 模板的引导内容。"""
    resp = client.get("/contents/99999")
    assert resp.status_code == 404
    assert "内容不存在" in resp.text
    assert "返回列表" in resp.text


def test_static_css_served():
    """静态样式挂载可用（本地资源，不访问外部网站）。"""
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")


def test_openapi_schema_lists_api_paths():
    """接口文档数据源 openapi.json 可用且包含全部核心路径。"""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert {"/api/health", "/api/contents", "/api/contents/search"} <= set(paths)
