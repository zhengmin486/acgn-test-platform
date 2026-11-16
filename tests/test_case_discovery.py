"""用例自动生成（从接口清单发现只读接口）测试。

覆盖：候选筛选（只读 / 无参数 / 排除写操作）、新增与更新的幂等、
人工用例不被改动、接口下线只停用不删除、缺少管理员账号时拒绝生成，
以及生成出来的用例真跑一次能通过。

独立临时 SQLite + get_db 覆盖，不触碰开发库。
"""

import pytest
from auth_helpers import create_accounts, login_as_admin
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import case_discovery, crud, runner
from app.database import Base, get_db
from app.main import app
from app.models import CASE_SOURCE_AUTO, CASE_SOURCE_MANUAL, TestCase
from app.schemas import TestCaseCreate

SAFE = ("GET", "HEAD")
BLACKLIST = case_discovery.EXCLUDED_SEGMENTS

# canary：这些路径必须出现在 discover_routes() 里。新增页面模块若漏进回归集
# （2026-10-22 实战：poster_admin / persona_pages.public_router 漏过），这里就红。
CANARY_PATHS = (
    "/", "/contents", "/articles", "/schedule", "/me",
    "/api/health", "/api/contents", "/api/tags",
    "/testcases", "/runs", "/batches",
    "/manage", "/manage/contents", "/manage/posters",
    "/persona", "/creators", "/admin",
)

# case_discovery 允许 import 的 app 模块（main 仅限函数内延迟导入）。
# case_samples / runner 不是页面模块：前者是参数样本配置，后者提供探测用的
# 进程内客户端。生成流程本来就包含「候选 → 探测」，这两个依赖是预期的；
# 这个断言真正要防的是把 article_pages / testing_pages 之类写死成清单。
ALLOWED_APP_IMPORTS = {
    "app", "app.crud", "app.models", "app.schemas", "app.main",
    "app.case_samples", "app.runner",
}


@pytest.fixture
def gen_env(tmp_path):
    """临时库 + 管理员账号 + get_db 覆盖；返回 session 工厂与管理员客户端。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cases.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(testing_session, admin=True, user=True)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    admin = TestClient(app)
    login_as_admin(admin)
    try:
        yield type("Env", (), {"session_factory": testing_session, "admin": admin})
    finally:
        app.dependency_overrides.pop(get_db, None)


def _db(gen_env):
    return gen_env.session_factory()


def test_discovery_returns_read_only_routes_without_params():
    """只发现 GET/HEAD、路径不带参数、不含动作词与静态资源。"""
    items = case_discovery.discover_routes()
    assert items, "至少要发现出接口，否则功能没生效"
    for item in items:
        assert item["method"] in SAFE
        assert "{" not in item["path"] and "}" not in item["path"]
        assert not item["path"].startswith(case_discovery.EXCLUDED_PREFIXES)
        segments = {s.lower() for s in item["path"].split("/") if s}
        assert not (segments & BLACKLIST)


def test_discovery_canary_paths_are_never_missed():
    """canary：核心路由必须被发现——新页面模块漏进回归集时这条就红。"""
    found = {item["path"] for item in case_discovery.discover_routes()}
    missing = [path for path in CANARY_PATHS if path not in found]
    assert not missing, f"这些路由没进回归集（新模块没挂载或被误排）：{missing}"


def test_discovery_source_has_no_page_module_imports():
    """AST 断言：case_discovery 不得 import 页面模块，防止又改回硬编码清单。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "case_discovery.py").read_text(encoding="utf-8")

    def is_app_module(name: str) -> bool:
        """名字是否对应真实存在的 app 模块文件（过滤掉函数/类名）。"""
        parts = name.split(".")
        if parts[0] != "app":
            return False
        rel = "/".join(parts)
        return ((root / f"{rel}.py").is_file()
                or (root / rel / "__init__.py").is_file())

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    offenders = sorted(m for m in imported
                       if is_app_module(m) and m not in ALLOWED_APP_IMPORTS)
    assert not offenders, f"case_discovery 又 import 了页面模块（改回硬编码？）：{offenders}"


def test_first_plan_lists_every_route_as_add(gen_env):
    """空库：全部候选都是「新增」，没有更新 / 下线 / 跳过。"""
    db = _db(gen_env)
    routes = case_discovery.discover_routes()
    plan = case_discovery.build_plan(db)
    assert plan.get("error") is None
    # 夹具里只有管理员、没有站长 → owner-only 接口跳过；
    # 需要查询参数才有 200 的接口（如 /api/contents/search）同样跳过
    owner_only = [item for item in routes if item["owner_only"]]
    needs_params = [item for item in routes
                    if item["path"] in case_discovery.NEEDS_PARAMS_PATHS]
    assert len(plan["add"]) == len(routes) - len(owner_only) - len(needs_params)
    assert len(plan["skip"]) == len(owner_only) + len(needs_params)
    assert plan["update"] == [] and plan["offline"] == []


def test_apply_then_reapply_is_idempotent(gen_env):
    """第二次生成：不新增、只更新，且用例 id 不变（回归对比靠 id 对齐）。"""
    db = _db(gen_env)
    plan = case_discovery.build_plan(db)
    routes = plan["add"]
    keys = [f"{i['method']} {i['path']}" for i in routes]

    summary = case_discovery.apply_plan(db, keys)
    assert summary["added"] == len(routes) and summary["updated"] == 0
    first_ids = {(c.method, c.path): c.id for c in crud.list_cases(db)}

    summary = case_discovery.apply_plan(db, keys)
    assert summary["added"] == 0 and summary["updated"] == len(routes)
    second_ids = {(c.method, c.path): c.id for c in crud.list_cases(db)}
    assert first_ids == second_ids, "重新生成不能重建用例，否则回归基线断裂"


def test_manual_case_is_never_touched(gen_env):
    """同路径已有人工用例：列入跳过，落库时不改它。"""
    db = _db(gen_env)
    target = case_discovery.discover_routes()[0]
    manual = crud.create_case(
        db,
        TestCaseCreate(name="我手写的用例", method=target["method"], path=target["path"],
                       expected_status=418, enabled=False),
        source=CASE_SOURCE_MANUAL,
    )
    plan = case_discovery.build_plan(db)
    skipped = {(i["method"], i["path"]) for i in plan["skip"]}
    assert (target["method"], target["path"]) in skipped

    keys = [f"{i['method']} {i['path']}" for i in plan["add"]]
    case_discovery.apply_plan(db, keys)

    db.refresh(manual)
    assert manual.name == "我手写的用例"
    assert manual.expected_status == 418
    assert manual.enabled is False
    assert manual.source == CASE_SOURCE_MANUAL


def test_offline_route_disables_auto_case_without_deleting(gen_env):
    """接口下线：auto 用例被停用而不是删除（历史执行记录仍可追溯）。"""
    db = _db(gen_env)
    ghost = crud.create_case(
        db,
        TestCaseCreate(name="GET /api/已经下线的接口", method="GET",
                       path="/api/已经下线的接口", expected_status=200, enabled=True),
        source=CASE_SOURCE_AUTO,
    )
    plan = case_discovery.build_plan(db)
    assert [c.id for c in plan["offline"]] == [ghost.id]

    case_discovery.apply_plan(db, [f"disable:{ghost.id}"])
    still = db.get(TestCase, ghost.id)
    assert still is not None and still.enabled is False


def test_owner_only_route_is_skipped_without_owner(gen_env):
    """没有站长账号时：owner-only 接口跳过，不猜期望值。"""
    db = _db(gen_env)
    admin_account = crud.highest_staff_account(db)
    assert crud.has_role(admin_account.role, crud.ROLE_OWNER) is False
    plan = case_discovery.build_plan(db)
    reasons = {i["path"]: i["reason"] for i in plan["skip"]}
    assert "/admin/roles" in reasons


def test_no_staff_account_refuses_to_generate(tmp_path):
    """库里没有管理员账号：直接给出原因，不生成一堆假的 401 用例。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'nostaff.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    create_accounts(testing_session, admin=False, user=True)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        db = testing_session()
        plan = case_discovery.build_plan(db)
        assert plan["error"]
        summary = case_discovery.apply_plan(db, ["GET /api/health"])
        assert summary["added"] == 0 and summary["error"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_generated_case_actually_passes(gen_env):
    """生成出来的用例真跑一次：以管理员身份访问，结果通过。"""
    db = _db(gen_env)
    health = next(i for i in case_discovery.discover_routes() if i["path"] == "/api/health")
    case_discovery.apply_plan(db, [f"{health['method']} {health['path']}"])
    case = crud.find_case(db, health["method"], health["path"])
    assert case is not None and case.source == CASE_SOURCE_AUTO

    record = runner.run_case(db, case)
    assert record.passed is True, record.error
    assert record.actual_status == 200


def test_generate_page_is_admin_only(gen_env):
    """生成页与提交接口同样只有管理员能进。"""
    guest = TestClient(app)
    resp = guest.get("/testcases/generate", follow_redirects=False)
    assert resp.status_code == 303
    assert gen_env.admin.get("/testcases/generate").status_code == 200


def test_apply_creates_pending_and_confirm_activates(gen_env):
    """方案 6.3：生成进 pending_review 且不启用，人工确认后才转 active。"""
    db = _db(gen_env)
    plan = case_discovery.build_plan(db)
    keys = [f"{i['method']} {i['path']}" for i in plan["add"][:3]]
    case_discovery.apply_plan(db, keys)

    pending = [c for c in crud.list_cases(db) if c.case_status == "pending_review"]
    assert len(pending) == 3
    assert all(not c.enabled for c in pending), "未确认的用例不能进批次"

    assert case_discovery.confirm_cases(db, [c.id for c in pending])["confirmed"] == 3
    db.expire_all()
    for case in crud.list_cases(db):
        assert case.case_status == "active" and case.enabled


def test_reject_keeps_record_and_cannot_be_confirmed(gen_env):
    """驳回保留记录但不执行；已驳回的用例不能被一次确认复活。"""
    db = _db(gen_env)
    plan = case_discovery.build_plan(db)
    item = plan["add"][0]
    case_discovery.apply_plan(db, [f"{item['method']} {item['path']}"])
    pending = [c for c in crud.list_cases(db) if c.case_status == "pending_review"]
    case_id = pending[0].id

    assert case_discovery.reject_cases(db, [case_id])["rejected"] == 1
    db.expire_all()
    case = crud.get_case(db, case_id)
    assert case.case_status == "rejected" and not case.enabled
    assert case_discovery.confirm_cases(db, [case_id])["confirmed"] == 0


def test_anomaly_plan_covers_overreach_and_params(gen_env):
    """异常用例 ≥20 条：越权期望值来自探测（303），带参取真实样本。"""
    db = _db(gen_env)
    plan = case_discovery.build_anomaly_plan(db)
    assert len(plan["add"]) >= 20
    assert {i["identity"] for i in plan["add"]} == {"anon", "staff"}

    anon = [i for i in plan["add"] if i["identity"] == "anon"]
    assert anon, "没有越权用例就谈不上覆盖异常请求"
    # 本项目页面路由走 RedirectResponse，匿名访问后台是 303 而不是 401
    assert all((i.get("probe") or {}).get("status") == 303 for i in anon)


def test_param_sample_missing_is_not_run(gen_env):
    """未配置样本的模板路径：不发请求、不写记录，记 missing_sample。"""
    db = _db(gen_env)
    case = crud.create_case(
        db,
        TestCaseCreate(name="GET /cast/{person_id}", method="GET",
                       path="/cast/{person_id}", expected_status=200),
        source=CASE_SOURCE_MANUAL,
        identity="staff",
    )
    judgment: dict = {}
    assert runner.run_case(db, case, judgment=judgment) is None
    assert judgment["reason"] == "missing_sample"
    assert crud.list_runs(db) == []


def test_confirm_records_operator_reject_does_not(gen_env):
    """确认写确认人与时间；确认人取自会话，忽略请求体里伪造的 confirmed_by。"""
    db = _db(gen_env)
    plan = case_discovery.build_plan(db)
    keys = [f"{i['method']} {i['path']}" for i in plan["add"][:2]]
    case_discovery.apply_plan(db, keys)
    ids = [c.id for c in crud.list_cases(db) if c.case_status == "pending_review"]
    assert len(ids) == 2

    resp = gen_env.admin.post(
        "/testcases/confirm",
        data={"case_id": [str(ids[0])], "confirmed_by": "999"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    confirmed = crud.get_case(db, ids[0])
    operator = crud.highest_staff_account(db)
    assert confirmed.case_status == "active" and confirmed.enabled
    assert confirmed.confirmed_at is not None
    assert confirmed.confirmed_by == operator.id != 999, "确认人必须来自会话，不能采信请求体"

    resp = gen_env.admin.post("/testcases/reject", data={"case_id": str(ids[1])},
                              follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    rejected = crud.get_case(db, ids[1])
    assert rejected.case_status == "rejected" and not rejected.enabled
    assert rejected.confirmed_at is None and rejected.confirmed_by is None


def test_apply_keeps_active_expectation_and_reports_drift(gen_env):
    """生成不覆盖已 active 用例的期望值，只报告差异（drift）。"""
    db = _db(gen_env)
    plan = case_discovery.build_plan(db)
    item = next(i for i in plan["add"] if i["path"] == "/api/health")
    case_discovery.apply_plan(db, [f"{item['method']} {item['path']}"])
    pending = [c for c in crud.list_cases(db) if c.case_status == "pending_review"]
    case_discovery.confirm_cases(db, [pending[0].id], operator_id=1)

    case = crud.get_case(db, pending[0].id)
    case.expected_status = 418
    db.commit()

    plan2 = case_discovery.build_plan(db)
    upd = next(i for i in plan2["update"] if i["case"].id == case.id)
    assert upd["drift"] == {"expected": 418, "actual": 200}

    case_discovery.apply_plan(db, [f"{case.method} {case.path}"])
    db.expire_all()
    assert crud.get_case(db, case.id).expected_status == 418, "回归基准不能被生成动作抹平"


def test_sample_value_shapes():
    """样本取值：兼容 items/data 包裹，空数组与缺字段各归各的原因。"""
    from app import case_samples

    spec = {"response_path": "items", "id_field": "id", "type": "int"}
    assert case_samples.pick_value({"items": [{"id": 7}]}, spec)[0] == 7
    assert case_samples.pick_value({"data": [{"id": 3}]}, spec)[0] == 3
    assert case_samples.pick_value({"items": []}, spec)[1] == "missing_sample"
    assert case_samples.pick_value({"items": [{}]}, spec)[1] == "invalid_sample"
