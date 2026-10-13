"""执行记录与执行器测试：通过/失败判定、方法支持、历史留存与页面流程。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离；
执行器只向进程内应用发请求，无真实网络访问。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import TestCase

VALID_PAYLOAD = {
    "name": "健康检查用例",
    "method": "GET",
    "path": "/api/health",
    "expected_status": 200,
    "enabled": True,
}


@pytest.fixture
def run_env(tmp_path):
    """独立临时数据库 + get_db 覆盖；暴露会话工厂供绕过校验的用例构造。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runs.db'}",
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
    yield SimpleNamespace(client=TestClient(app), session_factory=TestingSession)
    app.dependency_overrides.pop(get_db, None)


def _create(client, **overrides):
    return client.post("/api/testcases", json={**VALID_PAYLOAD, **overrides})


def _execute(client, case_id):
    return client.post(f"/api/testcases/{case_id}/execute")


def test_execute_pass_and_detail(run_env):
    """命中预期状态码 -> 通过；记录字段完整，详情接口与执行结果一致。"""
    case_id = _create(run_env.client).json()["id"]
    resp = _execute(run_env.client, case_id)
    assert resp.status_code == 201
    body = resp.json()
    assert body["case_id"] == case_id
    assert body["case_name"] == "健康检查用例"
    assert body["method"] == "GET"
    assert body["path"] == "/api/health"
    assert body["expected_status"] == 200
    assert body["actual_status"] == 200
    assert body["passed"] is True
    assert body["error"] is None
    assert isinstance(body["duration_ms"], (int, float)) and body["duration_ms"] >= 0
    assert body["created_at"]

    got = run_env.client.get(f"/api/runs/{body['id']}")
    assert got.status_code == 200
    assert got.json() == body


def test_execute_status_mismatch_fails(run_env):
    """实际与预期不一致 -> 失败，错误摘要写明差异，绝不误判为通过。"""
    case_id = _create(run_env.client, expected_status=404).json()["id"]
    body = _execute(run_env.client, case_id).json()
    assert body["passed"] is False
    assert body["actual_status"] == 200
    assert "不一致" in body["error"]


def test_execute_uses_configured_method(run_env):
    """按用例配置的方法发请求：POST 打到只有 GET 的接口 -> 405，与预期 405 一致则通过。"""
    case_id = _create(run_env.client, method="POST", path="/api/contents",
                      expected_status=405).json()["id"]
    body = _execute(run_env.client, case_id).json()
    assert body["actual_status"] == 405
    assert body["passed"] is True


def test_execute_records_target_404(run_env):
    """目标资源不存在的路径按预期 404 断言为通过（这是接口行为断言，不是执行错误）。"""
    case_id = _create(run_env.client, path="/api/contents/99999",
                      expected_status=404).json()["id"]
    body = _execute(run_env.client, case_id).json()
    assert body["actual_status"] == 404
    assert body["passed"] is True


def test_execute_missing_case_404(run_env):
    assert _execute(run_env.client, 999).status_code == 404


def test_non_local_path_recorded_as_failure(run_env):
    """脏数据（绕过校验写入的外部 URL）执行时被兜底拦截：记为失败、不发送请求。"""
    session = run_env.session_factory()
    session.add(TestCase(name="脏数据用例", method="GET",
                         path="https://evil.example/x",
                         expected_status=200, enabled=True))
    session.commit()
    bad_id = session.scalars(select(TestCase.id)).one()

    body = _execute(run_env.client, bad_id).json()
    assert body["passed"] is False
    assert body["actual_status"] is None
    assert "路径不合法" in body["error"]


def test_list_runs_order_and_filter(run_env):
    """记录按最新在前排序；支持按用例过滤。"""
    first = _create(run_env.client).json()["id"]
    _execute(run_env.client, first)
    second = _create(run_env.client, name="第二条", path="/api/contents").json()["id"]
    _execute(run_env.client, second)

    all_runs = run_env.client.get("/api/runs").json()
    assert all_runs["total"] == 2
    assert [r["id"] for r in all_runs["items"]] == [2, 1]

    filtered = run_env.client.get(f"/api/runs?case_id={first}").json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["case_id"] == first


def test_history_survives_case_deletion(run_env):
    """删除用例后执行历史保留，名称/方法/路径快照仍可完整解读。"""
    client = run_env.client
    case_id = _create(client).json()["id"]
    record = _execute(client, case_id).json()

    assert client.delete(f"/api/testcases/{case_id}").status_code == 204

    history = client.get(f"/api/runs?case_id={case_id}").json()
    assert history["total"] == 1
    item = history["items"][0]
    assert item["id"] == record["id"]
    assert item["case_id"] == case_id
    assert item["case_name"] == "健康检查用例"
    assert item["path"] == "/api/health"
    assert item["passed"] is True

    detail = client.get(f"/api/runs/{record['id']}")
    assert detail.status_code == 200


def test_get_run_missing_404(run_env):
    assert run_env.client.get("/api/runs/999").status_code == 404


def test_execute_page_flow(run_env):
    """页面执行：点击执行 -> 303 到本次详情页；历史页可见记录。"""
    client = run_env.client
    case_id = _create(client).json()["id"]

    resp = client.post(f"/testcases/{case_id}/execute", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/runs/1"

    detail = client.get("/runs/1")
    assert detail.status_code == 200
    assert "通过" in detail.text
    assert "健康检查用例" in detail.text

    history = client.get(f"/runs?case_id={case_id}")
    assert history.status_code == 200
    assert "执行历史" in history.text


def test_runs_page_missing_404(run_env):
    assert run_env.client.get("/runs/999").status_code == 404
