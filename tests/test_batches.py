"""批次执行与回归对比测试：批量、空用例集、部分失败、统计与历史查询。

依赖覆盖 + 独立临时 SQLite，与开发库 local_dev.db 完全隔离；
执行器仅面向进程内应用，无真实网络访问。
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app


@pytest.fixture
def batch_env(tmp_path):
    """独立临时数据库 + get_db 覆盖，用例结束还原依赖。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'batches.db'}",
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
    payload = {"name": "用例", "method": "GET", "path": "/api/health",
               "expected_status": 200, "enabled": True}
    payload.update(overrides)
    return client.post("/api/testcases", json=payload)


def test_run_batch_all_pass_stats(batch_env):
    """全部通过：批次统计正确，记录都归属本批次。"""
    client = batch_env.client
    _create(client)
    _create(client, name="内容列表", path="/api/contents")

    resp = client.post("/api/batches")
    assert resp.status_code == 201
    body = resp.json()
    assert body["total"] == 2
    assert body["passed"] == 2
    assert body["failed"] == 0
    assert body["pass_rate"] == 100.0
    assert body["started_at"] and body["finished_at"]

    runs = client.get("/api/runs?batch_id=1").json()
    assert runs["total"] == 2
    assert all(r["batch_id"] == 1 and r["passed"] for r in runs["items"])


def test_batch_only_enabled_cases(batch_env):
    """仅执行启用用例：停用用例不产生记录。"""
    client = batch_env.client
    enabled = _create(client).json()["id"]
    disabled = _create(client, name="停用用例", enabled=False).json()["id"]

    body = client.post("/api/batches").json()
    assert body["total"] == 1

    runs = client.get("/api/runs?batch_id=1").json()["items"]
    assert [r["case_id"] for r in runs] == [enabled]
    assert disabled not in [r["case_id"] for r in runs]


def test_batch_partial_failure(batch_env):
    """部分失败：失败用例被记录，批次统计正确，其余用例不受影响。"""
    client = batch_env.client
    _create(client)
    _create(client, name="预期错误用例", expected_status=500)

    body = client.post("/api/batches").json()
    assert body["total"] == 2
    assert body["passed"] == 1
    assert body["failed"] == 1
    assert body["pass_rate"] == 50.0

    runs = client.get("/api/runs?batch_id=1").json()["items"]
    failed_runs = [r for r in runs if not r["passed"]]
    assert len(failed_runs) == 1
    assert "不一致" in failed_runs[0]["error"]


def test_single_execution_not_counted_in_batch(batch_env):
    """单条执行记录 batch_id 为空，不计入批次统计，也不与批次记录混淆。"""
    client = batch_env.client
    case_id = _create(client).json()["id"]

    assert client.post(f"/api/testcases/{case_id}/execute").status_code == 201
    batch = client.post("/api/batches").json()
    assert batch["total"] == 1
    assert batch["passed"] == 1

    all_runs = client.get("/api/runs").json()
    assert all_runs["total"] == 2
    assert [r["batch_id"] for r in all_runs["items"]] == [1, None]
    batch_runs = client.get("/api/runs?batch_id=1").json()
    assert batch_runs["total"] == 1
    assert batch_runs["items"][0]["batch_id"] == 1

    client.post(f"/api/testcases/{case_id}/execute")
    assert client.get("/api/batches/1").json()["total"] == 1
    assert client.get("/api/runs?batch_id=1").json()["total"] == 1


def test_empty_batch_and_no_baseline(batch_env):
    """空用例集：仍是有效批次（total=0、通过率为空），不作为对比基线。"""
    client = batch_env.client
    body = client.post("/api/batches").json()
    assert body["total"] == 0
    assert body["passed"] == 0 and body["failed"] == 0
    assert body["pass_rate"] is None
    assert body["started_at"] and body["finished_at"]

    reg = client.get("/api/batches/1/regression").json()
    assert reg["comparable"] is False
    assert "没有用例" in reg["reason"]

    _create(client)
    client.post("/api/batches")
    reg2 = client.get("/api/batches/2/regression").json()
    assert reg2["comparable"] is False
    assert "没有更早的有效批次" in reg2["reason"]


def test_regression_pass_to_fail(batch_env):
    """上批通过、本批失败 -> 识别为回归。"""
    client = batch_env.client
    case_id = _create(client, expected_status=200).json()["id"]
    client.post("/api/batches")

    client.put(f"/api/testcases/{case_id}", json={
        "name": "健康检查用例", "method": "GET", "path": "/api/health",
        "expected_status": 404, "enabled": True})
    client.post("/api/batches")

    reg = client.get("/api/batches/2/regression").json()
    assert reg["comparable"] is True
    assert reg["previous_batch_id"] == 1
    assert len(reg["items"]) == 1
    item = reg["items"][0]
    assert item["case_id"] == case_id
    assert item["change"] == "regression"
    assert item["previous_passed"] is True
    assert item["current_passed"] is False


def test_regression_fixed_and_unchanged(batch_env):
    """失败变通过（fixed）与状态不变（unchanged_pass）识别正确；首批不可比。"""
    client = batch_env.client
    _create(client)
    _create(client, name="失败用例", expected_status=500)
    client.post("/api/batches")

    first_reg = client.get("/api/batches/1/regression").json()
    assert first_reg["comparable"] is False
    assert "没有更早的有效批次" in first_reg["reason"]

    cases = client.get("/api/testcases").json()["items"]
    fixed_target = next(c for c in cases if c["name"] == "失败用例")
    client.put(f"/api/testcases/{fixed_target['id']}", json={
        "name": "失败用例", "method": "GET", "path": "/api/health",
        "expected_status": 200, "enabled": True})
    client.post("/api/batches")

    reg = client.get("/api/batches/2/regression").json()
    assert reg["comparable"] is True
    changes = {i["case_id"]: i["change"] for i in reg["items"]}
    assert changes[1] == "unchanged_pass"
    assert changes[2] == "fixed"


def test_regression_added_and_missing(batch_env):
    """停用后的用例记为缺失（不计通过），新用例记为新增。"""
    client = batch_env.client
    old_case = _create(client).json()["id"]
    client.post("/api/batches")

    client.put(f"/api/testcases/{old_case}", json={
        "name": "用例", "method": "GET", "path": "/api/health",
        "expected_status": 200, "enabled": False})
    _create(client, name="新增用例", path="/api/contents")
    client.post("/api/batches")

    reg = client.get("/api/batches/2/regression").json()
    assert reg["comparable"] is True
    changes = {i["case_id"]: i for i in reg["items"]}
    assert changes[old_case]["change"] == "missing"
    assert changes[old_case]["current_passed"] is None
    assert "不计为通过" in changes[old_case]["detail"]
    assert changes[2]["change"] == "added"
    assert changes[2]["previous_passed"] is None


def test_batches_list_and_missing_404(batch_env):
    client = batch_env.client
    _create(client)
    client.post("/api/batches")

    listed = client.get("/api/batches").json()
    assert listed["total"] == 1

    assert client.get("/api/batches/999").status_code == 404
    assert client.get("/api/batches/999/regression").status_code == 404
    assert client.get("/batches/999").status_code == 404


def test_batch_page_flow(batch_env):
    """页面流程：执行全部 -> 303 到批次结果页；统计与回归区块可见。"""
    client = batch_env.client
    _create(client)

    resp = client.post("/testcases/execute-all", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/batches/1"

    detail = client.get("/batches/1")
    assert detail.status_code == 200
    assert "批次 #1" in detail.text
    assert "通过率" in detail.text
    assert "回归对比" in detail.text
    assert "没有更早的有效批次" in detail.text
    assert "通过" in detail.text

    listing = client.get("/batches")
    assert listing.status_code == 200
    assert "批次执行历史" in listing.text
