"""结果分析测试（方案第 3 步）：归因分母 / 前缀归一 / legacy 分口径 / 路由分组。

独立临时 SQLite + get_db 覆盖，不碰开发库；直接构造批次与执行记录，
以便精确控制 pass / fail / redirect / not_run 四类同时出现（真实跑批很难造齐）。
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import analysis
from app.database import Base, get_db
from app.main import app
from app.models import ExecutionBatch, ExecutionRecord
from auth_helpers import create_accounts, login_as_admin


@pytest.fixture
def analysis_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'analysis.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(Session, admin=True, user=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    login_as_admin(client)
    try:
        yield SimpleNamespace(client=client, session_factory=Session)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _batch(db, *, case_set=None, total=0, passed=0, failed=0):
    batch = ExecutionBatch(started_at=datetime.now(), total=total,
                           passed=passed, failed=failed, case_set=case_set)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _run(db, batch_id, *, passed, error=None, path="/api/contents",
         case_id=1, name="用例", duration_ms=1.0):
    run = ExecutionRecord(
        case_id=case_id, batch_id=batch_id, case_name=name, method="GET",
        path=path, expected_status=200, actual_status=200 if passed else 400,
        duration_ms=duration_ms, passed=passed, error=error,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _case_set(entries):
    """构造判定快照：entries 为 (case_id, state, outcome|reason, run_id|None)。"""
    import json
    cases = []
    for case_id, state, key, run_id in entries:
        item = {"case_id": case_id, "state": state}
        if state == "executed":
            item["outcome"] = key
            item["run_id"] = run_id
        else:
            item["reason"] = key
        cases.append(item)
    return json.dumps({"schema": 1, "case_ids": [c[0] for c in entries],
                       "cases": cases}, ensure_ascii=False)


def test_normalize_error_makes_legacy_and_new_failures_same_defect():
    """6.5：旧的全角冒号 ×2 与无前缀 ×1，归一后与同类新记录归因一致。"""
    pairs = (
        ("状态不符新", "状态码不符: 实际 400 与预期 200 不一致", "实际状态码 400 与预期 200 不一致"),
        ("路径新", "路径不合法: 仅允许本站本地路径", "路径不合法：仅允许本站本地路径"),
        ("请求新", "请求异常: Timeout", "请求异常：Timeout"),
    )
    for label, new_text, old_text in pairs:
        assert analysis.classify_failure(new_text) == analysis.classify_failure(old_text), label
    assert analysis.normalize_error("实际状态码 400 与预期 200 不一致").startswith(
        analysis.ERR_STATUS)
    assert analysis.normalize_error("路径不合法：x").startswith(analysis.ERR_PATH)
    # 判断不了的仍是 To Investigate，不猜根因。
    assert analysis.classify_failure("奇怪的错误") == "To Investigate"
    assert analysis.classify_failure("服务端错误: 服务端返回 500") == "To Investigate"


def test_attribution_denominator_is_failed_count_only(analysis_env):
    """通过 / 跳转 / 未执行都不进归因分母；五类占比之和为 100%。"""
    db = analysis_env.session_factory()
    batch = _batch(db, total=4, passed=2, failed=1)
    ok = _run(db, batch.id, passed=True, case_id=1)
    fail = _run(db, batch.id, passed=False, case_id=2,
                error="状态码不符: 实际 400 与预期 200 不一致")
    redirect = _run(db, batch.id, passed=True, case_id=3, path="/manage")
    batch.case_set = _case_set([
        (1, "executed", "pass", ok.id),
        (2, "executed", "fail", fail.id),
        (3, "executed", "redirect", redirect.id),
        (4, "not_run", "missing_sample", None),
        (5, "not_run", "disabled", None),
    ])
    db.commit()

    result = analysis.attribution(db, batch.id)
    assert result["total"] == 1
    assert result["counts"]["Product Bug"] == 1
    assert result["counts"]["To Investigate"] == 0
    assert [r.id for r in result["runs"]] == [fail.id]
    assert result["shares"]["Product Bug"] == 100.0
    assert result["shares"]["No Defect"] == 0.0


def test_failed_runs_use_case_set_outcome_not_passed_flag(analysis_env):
    """case_set 是判定权威：passed=False 但快照写 pass 的记录不算失败。"""
    db = analysis_env.session_factory()
    batch = _batch(db, total=1, passed=1, failed=0)
    run = _run(db, batch.id, passed=False, error="状态码不符: x")
    batch.case_set = _case_set([(1, "executed", "pass", run.id)])
    db.commit()
    assert analysis.collect_failed_runs(db, batch.id) == []


def test_legacy_batch_falls_back_to_passed_false(analysis_env):
    """legacy 批次没有快照：按 passed=False 回退，旧错误文本归一后同类。"""
    db = analysis_env.session_factory()
    batch = _batch(db, total=3, passed=1, failed=2)
    _run(db, batch.id, passed=True, case_id=1)
    _run(db, batch.id, passed=False, case_id=2,
         error="实际状态码 400 与预期 200 不一致")
    _run(db, batch.id, passed=False, case_id=3,
         error="路径不合法：仅允许本站本地路径")
    result = analysis.attribution(db, batch.id)
    assert result["total"] == 2
    assert result["counts"]["Product Bug"] == 1
    assert result["counts"]["Automation Bug"] == 1


def test_route_group_algorithm_and_override():
    """9.2：可复现算法 + 显式例外表，不靠路径前缀猜业务模块。"""
    assert analysis.route_group("/api/contents/7") == "api.contents"
    assert analysis.route_group("/manage/contents") == "manage.contents"
    assert analysis.route_group("/admin/roles") == "admin.roles"
    assert analysis.route_group("/contents/7") == "page.contents"
    assert analysis.route_group("/api") == "api"
    assert analysis.route_group("/") == "page.root"
    analysis.ROUTE_GROUP_OVERRIDES["/special"] = "api.special"
    try:
        assert analysis.route_group("/special") == "api.special"
    finally:
        del analysis.ROUTE_GROUP_OVERRIDES["/special"]


def test_route_group_matrix_counts_failures_per_batch(analysis_env):
    """矩阵按失败记录聚合，legacy 批次也按旧口径计入，不重复不漏。"""
    db = analysis_env.session_factory()
    first = _batch(db, total=1, failed=1)
    _run(db, first.id, passed=False, case_id=1, path="/api/contents/11",
         error="状态码不符: x")
    second = _batch(db, total=1, failed=1)
    _run(db, second.id, passed=False, case_id=2, path="/contents/12",
         error="服务端错误: 服务端返回 500")
    _run(db, None, passed=False, case_id=3, path="/api/tags",
         error="请求异常：Timeout")  # 单条执行单独一列

    matrix = analysis.route_group_matrix(db)
    by_group = {row["group"]: row for row in matrix["rows"]}
    assert matrix["batch_ids"] == [first.id, second.id]
    assert by_group["api.contents"]["counts"][first.id] == 1
    assert by_group["api.contents"]["counts"][second.id] == 0
    assert by_group["page.contents"]["counts"][second.id] == 1
    assert by_group["api.tags"]["single"] == 1
    assert by_group["api.tags"]["total"] == 1


def test_analysis_page_shows_distribution_and_batch_filter(analysis_env):
    """页面：五类占比、失败明细、批次筛选与空态都可见。"""
    client = analysis_env.client
    empty = client.get("/analysis")
    assert empty.status_code == 200
    assert "失败归因分布" in empty.text
    assert "没有失败记录" in empty.text

    db = analysis_env.session_factory()
    batch = _batch(db, total=2, passed=1, failed=1)
    _run(db, batch.id, passed=True, case_id=1)
    _run(db, batch.id, passed=False, case_id=2,
         error="实际状态码 400 与预期 200 不一致", path="/api/contents/9")
    page = client.get("/analysis")
    assert "Product Bug" in page.text
    assert "路由分组" in page.text
    assert "100.0%" in page.text
    assert "历史口径" in page.text  # legacy 批次被标注
    filtered = client.get(f"/analysis?batch_id={batch.id}")
    assert filtered.status_code == 200
    assert "批次失败总数：1" in filtered.text

    missing = client.get("/analysis?batch_id=999")
    assert "不存在" in missing.text


def test_batch_and_single_attribution_separate(analysis_env):
    """v0.6：单条失败不并入批次分母，两者分开统计；矩阵里单条单列。"""
    db = analysis_env.session_factory()
    batch = _batch(db, total=2, passed=1, failed=1)
    ok = _run(db, batch.id, passed=True, case_id=1)
    batch_fail = _run(db, batch.id, passed=False, case_id=2,
                      error="状态码不符: 实际 400 与预期 200 不一致")
    batch.case_set = _case_set([
        (1, "executed", "pass", ok.id),
        (2, "executed", "fail", batch_fail.id),
    ])
    single_fail = _run(db, None, passed=False, case_id=99,
                       error="路径不合法：非法路径", path="/api/bad")
    db.commit()

    batch_attr = analysis.attribution(db)
    single_attr = analysis.single_attribution(db)
    assert batch_attr["total"] == 1  # 不因单条失败从 1 变 2
    assert batch_attr["counts"]["Product Bug"] == 1
    assert batch_attr["counts"]["Automation Bug"] == 0
    assert single_attr["total"] == 1
    assert single_attr["counts"]["Automation Bug"] == 1
    assert [r.id for r in single_attr["runs"]] == [single_fail.id]
    # 指定批次时只看批次；单条不进该批。
    assert analysis.attribution(db, batch.id)["total"] == 1

    page = analysis_env.client.get("/analysis")
    assert "批次失败归因分布" in page.text
    assert "单条执行失败归因（独立统计）" in page.text
    assert "批次失败总数：1" in page.text
    assert "单条执行失败总数：1" in page.text


def test_redirect_prefix_never_enters_failure_set(analysis_env):
    """跳转前缀映射保留为防御兜底，但正常跳转不进失败归因集合。"""
    assert analysis.classify_failure("跳转: 预期 200 实际 302") == "To Investigate"
    db = analysis_env.session_factory()
    batch = _batch(db, total=1, passed=1, failed=0)
    redirect = _run(db, batch.id, passed=True, case_id=1, path="/manage")
    batch.case_set = _case_set([(1, "executed", "redirect", redirect.id)])
    db.commit()
    assert analysis.collect_failed_runs(db, batch.id) == []
    assert analysis.attribution(db, batch.id)["total"] == 0


def test_slowest_runs_only_valid_duration_and_readonly(analysis_env):
    """9.5：只取有效耗时、Top N 降序、新批按快照关联；查询只读不改任何统计。"""
    db = analysis_env.session_factory()
    batch = _batch(db, total=4, passed=4, failed=0)
    fast = _run(db, batch.id, passed=True, case_id=1, duration_ms=5.0)
    slow = _run(db, batch.id, passed=True, case_id=2, duration_ms=300.0)
    zero = _run(db, batch.id, passed=True, case_id=3, duration_ms=0.0)  # 无耗时
    legacy = _batch(db, total=1, passed=1, failed=0)
    legacy_slow = _run(db, legacy.id, passed=True, case_id=5, duration_ms=150.0)
    single = _run(db, None, passed=True, case_id=6, duration_ms=200.0)
    batch.case_set = _case_set([
        (1, "executed", "pass", fast.id),
        (2, "executed", "pass", slow.id),
        (3, "executed", "pass", zero.id),
    ])
    db.commit()

    top = analysis.slowest_runs(db)
    ids = [r.id for r in top]
    # 300 > 200(单条) > 150(legacy) > 5；耗时为 0 的不进、不按 0 占一个位置。
    assert ids == [slow.id, single.id, legacy_slow.id, fast.id]
    assert analysis.slowest_runs(db, limit=2) == [slow, single]

    # 新批次限定在该批时，只按快照关联该批的首次执行，不含 legacy / 单条。
    assert [r.id for r in analysis.slowest_runs(db, batch.id)] == [slow.id, fast.id]

    before = (batch.total, batch.passed, batch.failed, batch.case_set,
              slow.passed, slow.duration_ms)
    analysis.slowest_runs(db, limit=10)
    db.commit()
    after = (batch.total, batch.passed, batch.failed, batch.case_set,
             slow.passed, slow.duration_ms)
    assert before == after  # 查询不改写执行结果或批次统计


def test_trend_excludes_legacy_and_compares_case_ids(analysis_env):
    """9.3：趋势只含非 legacy 批次；集合变化比 ID 集合，不是比数量；只读。"""
    db = analysis_env.session_factory()
    # 批 1：legacy（无快照），必须被趋势排除。
    legacy = _batch(db, total=2, passed=2, failed=0)
    _run(db, legacy.id, passed=True, case_id=1)
    # 批 2：新口径，用例 {1,2,3,4,5}
    first = _batch(db, total=5, passed=5, failed=0)
    first_runs = [_run(db, first.id, passed=True, case_id=i) for i in (1, 2, 3, 4, 5)]
    first.case_set = _case_set([
        (i, "executed", "pass", first_runs[i - 1].id) for i in (1, 2, 3, 4, 5)
    ])
    # 批 3：新口径，用例 {1,2,3,4,6}——数量同为 5，但 5 删、6 增。
    second = _batch(db, total=5, passed=5, failed=0)
    second_runs = [_run(db, second.id, passed=True, case_id=i) for i in (1, 2, 3, 4, 6)]
    second.case_set = _case_set([
        (i, "executed", "pass", second_runs[j].id)
        for j, i in enumerate((1, 2, 3, 4, 6))
    ])
    db.commit()

    data = analysis.trend(db)
    assert [r["batch_id"] for r in data["rows"]] == [first.id, second.id]  # legacy 排除
    assert data["excluded_legacy"] is True
    assert data["rows"][0]["set_changed"] is False  # 首批无可比对象
    changed = data["rows"][1]
    assert changed["set_changed"] is True
    assert changed["added"] == 1 and changed["removed"] == 1  # 数量相同也抓到

    # 只读：不改批次统计与快照。
    before = (first.total, first.passed, first.case_set, second.case_set)
    analysis.trend(db)
    db.commit()
    assert before == (first.total, first.passed, first.case_set, second.case_set)


def test_batches_page_shows_trend_section(analysis_env):
    """批次列表页展示趋势区块；只有 legacy 时明说不参与。"""
    client = analysis_env.client
    page = client.get("/batches")
    assert page.status_code == 200
    assert "批次趋势（仅新口径批次）" in page.text
    assert "历史批次（旧口径）没有判定快照，不参与趋势与集合比较" in page.text
    assert "暂无新口径批次" in page.text  # 空库时诚实空态


def test_analysis_page_slowest_observation_wording(analysis_env):
    """页面把最慢用例标成耗时观察，不宣称性能测试。"""
    db = analysis_env.session_factory()
    batch = _batch(db, total=1, passed=1, failed=0)
    slow = _run(db, batch.id, passed=True, case_id=1, duration_ms=88.8)
    batch.case_set = _case_set([(1, "executed", "pass", slow.id)])
    db.commit()
    page = analysis_env.client.get("/analysis")
    assert page.status_code == 200
    assert "最慢用例（耗时观察，非性能测试）" in page.text
    assert "不是性能基准" in page.text
    assert "88.8" in page.text
