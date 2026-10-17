"""用例自动生成（从接口清单发现只读接口）测试。

覆盖：候选筛选（只读 / 无参数 / 排除写操作）、新增与更新的幂等、
人工用例不被改动、接口下线只停用不删除、缺少管理员账号时拒绝生成，
以及生成出来的用例真跑一次能通过。

独立临时 SQLite + get_db 覆盖，不触碰开发库。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import case_discovery, crud, runner
from app.database import Base, get_db
from app.main import app
from app.models import CASE_SOURCE_AUTO, CASE_SOURCE_MANUAL, TestCase
from app.schemas import TestCaseCreate
from auth_helpers import create_accounts, login_as_admin

SAFE = ("GET", "HEAD")
BLACKLIST = case_discovery.EXCLUDED_SEGMENTS


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
