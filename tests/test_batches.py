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
from auth_helpers import create_accounts, login_as_admin


@pytest.fixture
def batch_env(tmp_path):
    """独立临时数据库 + get_db 覆盖，并以管理员身份登录（测试管理平台需管理员）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'batches.db'}",
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
    yield SimpleNamespace(client=client, session_factory=TestingSession)
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


def test_regression_added_and_disabled(batch_env):
    """停用的用例记 not_run（不算缺失、不进六类），新用例记为新增（方案 5.3）。"""
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
    assert reg["set_incomplete"] is False
    changes = {i["case_id"]: i for i in reg["items"]}
    assert changes[old_case]["change"] == "not_run"
    assert changes[old_case]["current_passed"] is None
    assert "不参与六类对比" in changes[old_case]["detail"]
    assert reg["summary"]["missing"] == 0
    assert changes[2]["change"] == "added"
    assert changes[2]["previous_passed"] is None


def test_regression_summary_counts_and_pass_rate(batch_env):
    """四类核心分类计数、通过率变化与失败数变化均与实际执行记录一致。"""
    client = batch_env.client
    _create(client, name="始终通过", expected_status=200)          # id=1
    fail_to_pass = _create(client, name="由通过转失败", expected_status=200).json()["id"]  # id=2
    pass_to_pass = _create(client, name="由失败转通过", expected_status=500).json()["id"]  # id=3
    _create(client, name="始终失败", expected_status=500)          # id=4
    also_fixed = _create(client, name="本次修复", expected_status=500).json()["id"]        # id=5

    first = client.post("/api/batches").json()
    assert first["total"] == 5 and first["passed"] == 2 and first["failed"] == 3
    assert first["pass_rate"] == 40.0

    client.put(f"/api/testcases/{fail_to_pass}", json={
        "name": "由通过转失败", "method": "GET", "path": "/api/health",
        "expected_status": 500, "enabled": True})
    client.put(f"/api/testcases/{pass_to_pass}", json={
        "name": "由失败转通过", "method": "GET", "path": "/api/health",
        "expected_status": 200, "enabled": True})
    client.put(f"/api/testcases/{also_fixed}", json={
        "name": "本次修复", "method": "GET", "path": "/api/health",
        "expected_status": 200, "enabled": True})

    second = client.post("/api/batches").json()
    assert second["total"] == 5 and second["passed"] == 3 and second["failed"] == 2
    assert second["pass_rate"] == 60.0

    reg = client.get("/api/batches/2/regression").json()
    assert reg["comparable"] is True
    assert reg["previous_batch_id"] == 1
    assert reg["current_batch_id"] == 2
    assert reg["summary"] == {
        "regression": 1, "fixed": 2, "unchanged_fail": 1,
        "unchanged_pass": 1, "added": 0, "missing": 0,
    }
    assert reg["pass_rate"] == {"current": 60.0, "previous": 40.0, "delta": 20.0}
    assert reg["failure_change"] == {"current": 2, "previous": 3, "delta": -1}

    changes = {i["case_id"]: i["change"] for i in reg["items"]}
    assert changes == {
        fail_to_pass: "regression",
        pass_to_pass: "fixed",
        also_fixed: "fixed",
        1: "unchanged_pass",
        4: "unchanged_fail",
    }


def test_regression_case_set_inconsistent_summary(batch_env):
    """用例集合不一致：停用不算缺失、新用例算新增，两者都不进四类核心分类。"""
    client = batch_env.client
    old_case = _create(client, name="旧用例", expected_status=200).json()["id"]
    client.post("/api/batches")

    client.put(f"/api/testcases/{old_case}", json={
        "name": "旧用例", "method": "GET", "path": "/api/health",
        "expected_status": 200, "enabled": False})
    new_case = _create(client, name="新增用例", path="/api/contents").json()["id"]
    client.post("/api/batches")

    reg = client.get("/api/batches/2/regression").json()
    assert reg["comparable"] is True
    assert reg["summary"] == {
        "regression": 0, "fixed": 0, "unchanged_fail": 0,
        "unchanged_pass": 0, "added": 1, "missing": 0,
    }
    assert reg["pass_rate"] == {"current": 100.0, "previous": 100.0, "delta": 0.0}

    items = {i["case_id"]: i for i in reg["items"]}
    assert items[old_case]["change"] == "not_run"
    assert items[old_case]["current_passed"] is None
    assert items[new_case]["change"] == "added"
    assert items[new_case]["previous_passed"] is None


def test_regression_no_history_and_empty_batch_summary(batch_env):
    """无历史批次 / 当前批次为空：不可对比，items 为空且汇总为零值结构。"""
    client = batch_env.client

    empty = client.post("/api/batches").json()
    assert empty["total"] == 0
    reg_empty = client.get("/api/batches/1/regression").json()
    assert reg_empty["comparable"] is False
    assert "没有用例" in reg_empty["reason"]
    assert reg_empty["items"] == []
    assert reg_empty["summary"] == {
        "regression": 0, "fixed": 0, "unchanged_fail": 0,
        "unchanged_pass": 0, "added": 0, "missing": 0,
    }

    _create(client)
    client.post("/api/batches")
    reg_first = client.get("/api/batches/2/regression").json()
    assert reg_first["comparable"] is False
    assert "没有更早的有效批次" in reg_first["reason"]
    assert reg_first["items"] == []
    assert all(v == 0 for v in reg_first["summary"].values())


def test_batch_detail_page_shows_regression_summary(batch_env):
    """批次详情页展示通过率变化、失败数变化与四类分类计数。"""
    client = batch_env.client
    _create(client, name="通过用例", expected_status=200)
    regression_case = _create(client, name="回归用例", expected_status=200).json()["id"]
    client.post("/api/batches")

    client.put(f"/api/testcases/{regression_case}", json={
        "name": "回归用例", "method": "GET", "path": "/api/health",
        "expected_status": 500, "enabled": True})
    client.post("/api/batches")

    page = client.get("/batches/2")
    assert page.status_code == 200
    for text in ("通过率变化", "失败用例数变化", "新增失败", "已修复失败",
                 "持续失败", "持续通过", "新增用例", "缺失用例"):
        assert text in page.text
    assert "与批次 #1 对比" in page.text


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


# ---------- 二期第 1 步：四态判定与批次快照 ----------


def _case_set_of(session_factory, batch_id: int) -> dict:
    import json
    from app.models import ExecutionBatch
    db = session_factory()
    try:
        batch = db.get(ExecutionBatch, batch_id)
        return json.loads(batch.case_set) if batch.case_set else None
    finally:
        db.close()


def test_case_set_snapshot_and_invariants(batch_env):
    """批次写 case_set/meta/env；八计数两条恒等式成立，判定落 outcome。"""
    client = batch_env.client
    _create(client)  # /api/health 200
    _create(client, name="失败用例", expected_status=500)  # 精确匹配不符

    client.post("/api/batches")
    cs = _case_set_of(batch_env.session_factory, 1)
    assert cs is not None and cs["schema"] == 1
    counts = cs["counts"]
    assert counts == {
        "case_count": 2, "eligible_count": 2, "executed_count": 2,
        "redirect_count": 0, "not_run_count": 0, "disabled_count": 0,
        "pending_count": 0, "rejected_count": 0,
    }
    assert counts["case_count"] == (
        counts["eligible_count"] + counts["disabled_count"]
        + counts["pending_count"] + counts["rejected_count"]
    )
    assert counts["eligible_count"] == counts["executed_count"] + counts["not_run_count"]
    outcomes = sorted(c["outcome"] for c in cs["cases"])
    assert outcomes == ["fail", "pass"]
    assert all(c["state"] == "executed" and c["run_id"] for c in cs["cases"])

    from app.models import ExecutionBatch, ExecutionRecord
    import json
    db = batch_env.session_factory()
    try:
        b = db.get(ExecutionBatch, 1)
        meta, env = json.loads(b.meta), json.loads(b.env)
        assert meta["result_schema"] == 1 and meta["runtime"] == "real"
        assert meta["selection"] == "all" and "commit" in meta
        assert set(env) == {"python", "platform", "db_path"}
        # 第 4 步启用复跑：仅失败记录写 retries（2 次复跑），通过记录仍 NULL。
        # 复跑不覆盖首次结果——失败记录仍是 passed=False / actual_status=200 等原值。
        runs = db.query(ExecutionRecord).all()
        failed_runs = [r for r in runs if not r.passed]
        passed_runs = [r for r in runs if r.passed]
        assert failed_runs and all(r.retries for r in failed_runs)
        assert all(r.retries is None for r in passed_runs)
        for r in failed_runs:
            attempts = json.loads(r.retries)
            assert len(attempts) == 2
            assert all(set(a) == {"attempt", "passed", "actual_status", "error",
                                  "duration_ms"} for a in attempts)
            assert [a["attempt"] for a in attempts] == [1, 2]
    finally:
        db.close()


def test_disabled_case_goes_to_case_set_not_eligible(batch_env):
    """停用用例进快照标 disabled，不进 eligible、不进分母，恒等式仍成立。"""
    client = batch_env.client
    _create(client)
    disabled = _create(client, name="停用", enabled=False).json()["id"]
    assert client.put(f"/api/testcases/{disabled}", json={
        "name": "停用", "method": "GET", "path": "/api/health",
        "expected_status": 200, "enabled": False,
    }).status_code == 200

    body = client.post("/api/batches").json()
    assert body["total"] == 1  # 与二期前一致：只算启用用例
    cs = _case_set_of(batch_env.session_factory, 1)
    assert cs["counts"]["case_count"] == 2
    assert cs["counts"]["eligible_count"] == 1
    assert cs["counts"]["disabled_count"] == 1
    by_id = {c["case_id"]: c for c in cs["cases"]}
    assert by_id[disabled]["state"] == "not_run"
    assert by_id[disabled]["reason"] == "disabled"
    assert "not_run" not in by_id[disabled]  # 没有 outcome
    assert cs["counts"]["case_count"] == (
        cs["counts"]["eligible_count"] + cs["counts"]["disabled_count"]
        + cs["counts"]["pending_count"] + cs["counts"]["rejected_count"]
    )


def test_legacy_batch_page_shows_old_caliber(batch_env):
    """无 case_set 的旧批次：页面标历史口径、不显示跳转/未执行卡片、数字不变。"""
    from app.models import ExecutionBatch
    from datetime import datetime
    db = batch_env.session_factory()
    try:
        db.add(ExecutionBatch(started_at=datetime.now(), finished_at=datetime.now(),
                              total=5, passed=4, failed=1))  # case_set 留 NULL
        db.commit()
    finally:
        db.close()
    page = batch_env.client.get("/batches/1")
    assert page.status_code == 200
    assert "历史批次" in page.text and "旧口径" in page.text
    assert "单独记录" not in page.text  # 不套新统计（跳转卡片）
    assert "80.0%" in page.text  # 旧口径通过率 4/5 照常显示


def test_regression_deleted_case_is_missing(batch_env):
    """只有真删除才算 missing（方案 5.3）；停用走 not_run，不算缺失。"""
    client = batch_env.client
    old_case = _create(client, name="将被删除", expected_status=200).json()["id"]
    _create(client, name="保留用例", path="/api/contents")
    client.post("/api/batches")
    assert client.delete(f"/api/testcases/{old_case}").status_code == 204
    client.post("/api/batches")

    reg = client.get("/api/batches/2/regression").json()
    items = {i["case_id"]: i for i in reg["items"]}
    assert items[old_case]["change"] == "missing"
    assert items[old_case]["current_passed"] is None
    assert "已删除" in items[old_case]["detail"]
    assert reg["summary"]["missing"] == 1
    assert reg["set_incomplete"] is False


def test_legacy_regression_marks_set_incomplete(batch_env):
    """相邻批次有 legacy 时标 set_incomplete，added/missing 明示不可信（方案 5.6）。"""
    from app.models import ExecutionBatch
    from datetime import datetime
    client = batch_env.client
    _create(client, expected_status=200)
    client.post("/api/batches")  # id=1，新口径有快照
    db = batch_env.session_factory()
    try:
        db.add(ExecutionBatch(started_at=datetime.now(), finished_at=datetime.now(),
                              total=1, passed=1, failed=0))  # id=2，legacy
        db.commit()
    finally:
        db.close()

    reg = client.get("/api/batches/2/regression").json()
    assert reg["comparable"] is True
    assert reg["set_incomplete"] is True
    page = client.get("/batches/2")
    assert "用例集合不完整" in page.text


def test_pass_rate_uses_case_set_caliber(batch_env):
    """方案 6.2：分母排除跳转与未执行；legacy 回退旧公式。"""
    from app.models import ExecutionBatch
    from app.runner import batch_pass_rate, CASE_SET_SCHEMA
    from datetime import datetime
    import json
    batch = ExecutionBatch(started_at=datetime.now(), total=5, passed=3, failed=1)
    # passed=3 含 1 条跳转（test_runs.passed 占位 True），不能当分子。
    batch.case_set = json.dumps({
        "schema": CASE_SET_SCHEMA,
        "counts": {"eligible_count": 5, "redirect_count": 1, "not_run_count": 1},
        "cases": [
            {"case_id": 1, "state": "executed", "outcome": "pass"},
            {"case_id": 2, "state": "executed", "outcome": "pass"},
            {"case_id": 3, "state": "executed", "outcome": "fail"},
            {"case_id": 4, "state": "executed", "outcome": "redirect"},
            {"case_id": 5, "state": "not_run", "reason": "missing_sample"},
        ],
    })
    assert batch_pass_rate(batch) == 66.7  # 2 / (5 - 1 - 1)
    batch.case_set = None
    assert batch_pass_rate(batch) == 60.0  # legacy 旧口径 3 / 5


# ---- 第 4 步：复跑写入 / flaky 扣除 / mixed 单列（方案 9.4 / 11.2） ----

def test_retry_written_without_overwriting_first_result(batch_env, monkeypatch):
    """首次失败后复跑 2 次写 retries；首次 passed / 状态 / 批次计数不变。"""
    from app import runner

    # 首次返回失败（400 不符预期 200），两次复跑返回通过。
    answers = [
        (400, False, "状态码不符: 实际 400 与预期 200 不一致", "fail", 10.0),
        (200, True, None, "pass", 5.0),
        (200, True, None, "pass", 6.0),
    ]
    calls = {"n": 0}

    def fake_execute(case, request_path, identity, token):
        result = answers[calls["n"] % len(answers)]
        calls["n"] += 1
        return result

    monkeypatch.setattr(runner, "_execute_once", fake_execute)
    client = batch_env.client
    case_id = _create(client, expected_status=200).json()["id"]
    body = client.post("/api/batches").json()
    assert body["total"] == 1 and body["failed"] == 1  # 计数按首次失败

    db = batch_env.session_factory()
    try:
        run = db.query(__import__("app.models", fromlist=["ExecutionRecord"]).ExecutionRecord).first()
        assert run.passed is False and run.actual_status == 400  # 首次未改写
        import json
        attempts = json.loads(run.retries)
        assert len(attempts) == 2
        assert [a["passed"] for a in attempts] == [True, True]
        assert runner._retry_kind(run) == "flaky"
    finally:
        db.close()


def test_flaky_excluded_from_regression_count(batch_env, monkeypatch):
    """A 通过 / B 首次失败+复跑两次通过：regression 不增加，明细单列 flaky，通过率仍按失败。"""
    from app import runner

    case_id = _create(batch_env.client, expected_status=200).json()["id"]
    batch_env.client.post("/api/batches")  # 批 1：通过
    client = batch_env.client
    client.put(f"/api/testcases/{case_id}", json={
        "name": "用例", "method": "GET", "path": "/api/health",
        "expected_status": 404, "enabled": True})
    # 批 2：首次失败 + 两次复跑通过
    answers = [
        (200, False, "状态码不符: 实际 200 与预期 404 不一致", "fail", 10.0),
        (404, True, None, "pass", 5.0),
        (404, True, None, "pass", 6.0),
    ]
    state = {"n": 0}

    def fake_execute(case, request_path, identity, token):
        ret = answers[state["n"] % len(answers)]
        state["n"] += 1
        return ret

    monkeypatch.setattr(runner, "_execute_once", fake_execute)
    client.post("/api/batches")
    reg = client.get("/api/batches/2/regression").json()
    assert reg["summary"]["regression"] == 0  # 已扣除
    flaky = [i for i in reg["items"] if i["change"] == "flaky"]
    assert len(flaky) == 1 and flaky[0]["case_id"] == case_id
    assert "不计入回归" in flaky[0]["detail"]
    # 通过率仍按首次失败：case_set.outcome 未被复跑覆盖。
    detail = client.get("/api/batches/2").json()
    assert detail["failed"] == 1
    page = client.get("/batches/2")
    assert "疑似不稳定 1" in page.text
    assert "疑似不稳定（不计入回归）" in page.text


def test_mixed_retry_listed_separately(batch_env, monkeypatch):
    """复跑结果不一致（失败→通过→失败）单列 mixed，不计入 regression。"""
    from app import runner

    case_id = _create(batch_env.client, expected_status=200).json()["id"]
    batch_env.client.post("/api/batches")
    client = batch_env.client
    client.put(f"/api/testcases/{case_id}", json={
        "name": "用例", "method": "GET", "path": "/api/health",
        "expected_status": 404, "enabled": True})
    answers = [
        (200, False, "状态码不符: 首次", "fail", 10.0),
        (404, True, None, "pass", 5.0),
        (200, False, "状态码不符: 三次", "fail", 7.0),
    ]
    state = {"n": 0}

    def fake_execute(case, request_path, identity, token):
        ret = answers[state["n"] % len(answers)]
        state["n"] += 1
        return ret

    monkeypatch.setattr(runner, "_execute_once", fake_execute)
    client.post("/api/batches")
    reg = client.get("/api/batches/2/regression").json()
    assert reg["summary"]["regression"] == 0
    mixed = [i for i in reg["items"] if i["change"] == "mixed"]
    assert len(mixed) == 1
    assert "尚不能确认稳定性" in mixed[0]["detail"]


def test_single_execute_does_not_retry(batch_env, monkeypatch):
    """单条执行不触发复跑（retry_on_fail 默认 False），retries 保持 NULL。"""
    from app import runner
    from app.models import ExecutionRecord

    calls = {"n": 0}
    real = runner._execute_once

    def count_calls(case, request_path, identity, token):
        calls["n"] += 1
        return real(case, request_path, identity, token)

    monkeypatch.setattr(runner, "_execute_once", count_calls)
    client = batch_env.client
    case_id = _create(client, expected_status=404).json()["id"]  # 访问 200 期望 404 → 失败
    client.post(f"/api/testcases/{case_id}/execute")
    assert calls["n"] == 1  # 只发 1 次，无复跑
    db = batch_env.session_factory()
    try:
        run = db.query(ExecutionRecord).first()
        assert run.retries is None and run.passed is False
    finally:
        db.close()


def test_retry_kind_pure_function():
    """_retry_kind 纯函数：flaky / mixed / None（首次通过、不足2次、损坏）。"""
    from app import runner
    import json
    from types import SimpleNamespace

    def rec(passed, retries):
        return SimpleNamespace(passed=passed, retries=retries)

    both_pass = json.dumps([{"passed": True}, {"passed": True}])
    mixed = json.dumps([{"passed": False}, {"passed": True}])
    both_fail = json.dumps([{"passed": False}, {"passed": False}])
    assert runner._retry_kind(rec(False, both_pass)) == "flaky"
    assert runner._retry_kind(rec(False, mixed)) == "mixed"
    assert runner._retry_kind(rec(False, both_fail)) is None
    assert runner._retry_kind(rec(True, both_pass)) is None  # 首次通过不标
    assert runner._retry_kind(rec(False, None)) is None
    assert runner._retry_kind(rec(False, "[broken")) is None
    assert runner._retry_kind(rec(False, json.dumps([{"passed": True}]))) is None

