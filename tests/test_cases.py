"""测试用例管理功能测试：正常 CRUD、无效输入、不存在用例与页面流程。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from auth_helpers import create_accounts, login_as_admin

VALID_PAYLOAD = {
    "name": "健康检查用例",
    "method": "GET",
    "path": "/api/health",
    "expected_status": 200,
    "enabled": True,
}


@pytest.fixture
def case_client(tmp_path):
    """独立临时数据库 + get_db 覆盖，并以管理员身份登录（测试管理平台需管理员）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cases.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(TestingSession, admin=True, user=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    login_as_admin(client)
    yield client
    app.dependency_overrides.pop(get_db, None)


def _create(case_client, **overrides):
    return case_client.post("/api/testcases", json={**VALID_PAYLOAD, **overrides})


def test_create_and_get_case(case_client):
    """创建用例：字段完整返回、时间存在；详情与创建结果一致。"""
    resp = _create(case_client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "健康检查用例"
    assert body["method"] == "GET"
    assert body["path"] == "/api/health"
    assert body["expected_status"] == 200
    assert body["enabled"] is True
    assert body["created_at"]

    got = case_client.get(f"/api/testcases/{body['id']}")
    assert got.status_code == 200
    assert got.json() == body


def test_list_cases(case_client):
    """列表：total 与条目一致、按 id 排序。"""
    _create(case_client)
    _create(case_client, name="第二条用例", path="/api/contents")
    resp = case_client.get("/api/testcases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [i["id"] for i in body["items"]] == [1, 2]


def test_update_case(case_client):
    """编辑：字段更新、方法统一大写存储、启用状态可关闭。"""
    case_id = _create(case_client).json()["id"]
    resp = case_client.put(f"/api/testcases/{case_id}", json={
        "name": "内容列表用例",
        "method": "get",
        "path": "/api/contents",
        "expected_status": 200,
        "enabled": False,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "内容列表用例"
    assert body["method"] == "GET"
    assert body["enabled"] is False


def test_delete_case(case_client):
    """删除：204，之后详情 404、列表归零。"""
    case_id = _create(case_client).json()["id"]
    assert case_client.delete(f"/api/testcases/{case_id}").status_code == 204
    assert case_client.get(f"/api/testcases/{case_id}").status_code == 404
    assert case_client.get("/api/testcases").json()["total"] == 0


@pytest.mark.parametrize("overrides", [
    {"name": "   "},
    {"method": "FETCH"},
    {"path": "https://example.com/api"},
    {"path": "//evil.com/x"},
    {"path": "/api with space"},
    {"expected_status": 99},
    {"expected_status": 999},
])
def test_create_case_invalid_input(case_client, overrides):
    """无效输入（空白名称/未支持方法/外部 URL/协议相对路径/含空白路径/状态码越界）-> 422。"""
    assert _create(case_client, **overrides).status_code == 422


def test_missing_case_api_404(case_client):
    """不存在的用例：GET/PUT/DELETE 均返回 404。"""
    assert case_client.get("/api/testcases/999").status_code == 404
    assert case_client.put("/api/testcases/999", json=VALID_PAYLOAD).status_code == 404
    assert case_client.delete("/api/testcases/999").status_code == 404


def test_cases_page_renders(case_client):
    """用例管理页：标题与已有用例名称渲染。"""
    _create(case_client)
    resp = case_client.get("/testcases")
    assert resp.status_code == 200
    assert "测试用例管理" in resp.text
    assert "健康检查用例" in resp.text


def test_cases_page_create_form(case_client):
    """页面表单新建：成功后 303 重定向回列表，列表可见新用例。"""
    resp = case_client.post("/testcases", data={
        "name": "详情页用例", "method": "GET", "path": "/contents/1",
        "expected_status": "200", "enabled": "on",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/testcases"
    assert "详情页用例" in case_client.get("/testcases").text


def test_cases_page_create_form_rejects_bad_path(case_client):
    """页面表单拒绝外部 URL：400 + 错误提示，且不落库。"""
    resp = case_client.post("/testcases", data={
        "name": "坏用例", "method": "GET", "path": "https://example.com/x",
        "expected_status": "200", "enabled": "on",
    })
    assert resp.status_code == 400
    assert "不允许外部 URL" in resp.text
    assert case_client.get("/api/testcases").json()["total"] == 0


def test_cases_page_edit_flow(case_client):
    """页面编辑流：编辑页预填，提交后 303，字段与方法大小写归一生效。"""
    case_id = _create(case_client).json()["id"]
    page = case_client.get(f"/testcases/{case_id}/edit")
    assert page.status_code == 200
    assert "健康检查用例" in page.text

    resp = case_client.post(f"/testcases/{case_id}/edit", data={
        "name": "改名用例", "method": "POST", "path": "/api/contents",
        "expected_status": "200", "enabled": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    body = case_client.get(f"/api/testcases/{case_id}").json()
    assert body["name"] == "改名用例"
    assert body["method"] == "POST"
    assert body["enabled"] is False


def test_cases_page_delete_flow(case_client):
    """页面删除流：303 后列表归零。"""
    case_id = _create(case_client).json()["id"]
    resp = case_client.post(f"/testcases/{case_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert case_client.get("/api/testcases").json()["total"] == 0


def test_cases_page_missing_404(case_client):
    """页面对不存在用例：编辑页/编辑提交/删除均 404。"""
    assert case_client.get("/testcases/999/edit").status_code == 404
    assert case_client.post("/testcases/999/edit", data={}).status_code == 404
    assert case_client.post("/testcases/999/delete", data={}).status_code == 404
