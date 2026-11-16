"""用例自动生成：从应用自身的路由表发现「只读接口」，生成 / 更新 / 下线用例。

设计约束（按 2026-10-18 与用户确认的方案）：

- **只发现 GET / HEAD，且路径不带参数**：带 `{id}` 的接口需要示例值，本阶段一律跳过
  （页面里会单列出来，想加就手动勾一条并填 id）；
- **排除写操作与动作类路径**（delete / execute / apply / clear …）：执行器发的是真请求，
  自动生成一条删除就是真删库；
- **期望状态码按管理员身份推断**：有守卫的接口管理员可达，一律 200；
  少量已知非 200 的只读路由（旧地址 302 跳转、/settings 303）走
  ``EXPECTED_STATUS_OVERRIDES`` 显式钉死，新路由猜错会先在批次里失败暴露；
  owner-only 接口在库里没有站长账号时直接跳过，不猜一个错的值；
- **auto 用例只更新、不重建**：回归对比按 case_id 对齐两次批次，删了重建会让基线全变
  「新增 / 缺失」，回归功能直接失效。接口下线时只停用。
- **manual 用例永不改动**：不改名、不改期望值、不停用、不删除。
- **路由来源 = app.routes 反推**（2026-10-22 改）：不硬编码页面模块清单，
  新模块只要在 app.main 挂载就自动进回归集；防回退靠 canary + AST 守护测试。
"""

from app import case_samples, crud
from app.models import (
    CASE_SOURCE_AUTO,
    CASE_STATUS_ACTIVE,
    CASE_STATUS_PENDING,
    CASE_STATUS_REJECTED,
)
from app.schemas import TestCaseCreate

# 只生成这两种方法：幂等、只读，误跑也不会改数据。
SAFE_METHODS = ("GET", "HEAD")

# 路径片段黑名单：即使方法是 GET 也跳过（这些是会改变状态的动作，或认证流程入口）。
EXCLUDED_SEGMENTS = frozenset({
    "delete", "execute", "apply", "clear", "remove", "resync",
    "verify", "revoke", "generate", "logout", "login", "register",
    # 认证流程入口：执行器固定以 staff 身份请求，这些路由的状态码取决于
    # 「是否已登录 / token 是否有效」，不是稳定信号（如 /forgot-password
    # 已登录会 303、/reset-password 无 token 会 400）。
    "confirm", "forgot-password", "reset-password",
})

# 前缀黑名单：静态资源与外链代理，状态码取决于文件是否存在，属于噪声。
EXCLUDED_PREFIXES = ("/media/", "/static", "/covers")

# 参数全部可选、但业务上必须有值才返回的接口（空参直接 400）。
# 与带 {id} 的接口同理：本阶段不生成，等「用例参数」功能再做。
NEEDS_PARAMS_PATHS = frozenset({"/api/contents/search"})

# 期望状态码：管理员视角下，被发现出来的接口都应可访问。
EXPECTED_OK = 200

# 少量已知非 200 的只读路由：按真实行为钉死期望值，而不是猜 200。
# 新路由状态码若不是 200，会先在批次里以失败暴露，来这里补一条即可——
# 显式清单胜过静默猜错。
EXPECTED_STATUS_OVERRIDES = {
    "/admin/data": 302,      # 旧地址兼容：跳 /manage
    "/schedule/runs": 302,   # 旧地址兼容：跳 /manage/sync/runs
    "/settings": 303,        # 跳 /settings/profile
}

# owner-only 守卫的函数名（用于判断「需要站长账号」）。
OWNER_GUARD_NAME = "require_owner_page"


def _iter_route_objects(routes):
    """递归摊平路由容器，兼容两种 FastAPI include 结构。

    - 旧版：``include_router`` 把路由直接摊平进 ``app.routes``，元素就是 Route；
    - 0.143 起：包一层 ``_IncludedRouter``（带 ``original_router`` 指向原 APIRouter）；
    - 嵌套 router / Mount 一律递归进 ``.routes``；没有 ``.routes`` 的（如静态文件
      Mount）天然跳过——它们本来也不该进用例集。
    """
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_route_objects(original.routes)
        elif hasattr(route, "routes") and not hasattr(route, "methods"):
            yield from _iter_route_objects(route.routes)
        elif hasattr(route, "methods"):
            yield route


def _all_routes():
    """取全部待扫描的路由对象：从**已挂载的应用实例**反推，不硬编码页面模块清单。

    为什么改（2026-10-22）：模块清单写死在本文件里，新增页面模块忘记加进去，
    它的路由就永远不进回归集，且没有任何测试会因此报红——实战中 poster_admin
    与 persona_pages.public_router 漏了一阵子。app.main 已经 import 并挂载了
    每一个 router，从 app.routes 反推是唯一不会漏的源。

    延迟 import app.main：generate 页面触发时应用已在运行，无循环导入风险；
    pytest 里同样可导入。防回退由 tests/test_case_discovery.py 的 canary 与
    AST 断言守着（本文件不得再出现任何页面模块名）。
    """
    from app.main import app  # 延迟导入：避免模块级循环依赖

    yield from _iter_route_objects(app.routes)


def _dependency_names(route) -> set[str]:
    """路由级守卫的函数名集合（用于识别 owner-only 接口）。"""
    names = set()
    for dependency in getattr(route, "dependencies", None) or []:
        target = getattr(dependency, "dependency", None)
        if target is not None:
            names.add(getattr(target, "__name__", ""))
    names.discard("")
    return names


def _is_candidate(method: str, path: str) -> bool:
    """是否是可自动生成的候选项：只读方法 + 无路径参数 + 不在黑名单里。"""
    if method not in SAFE_METHODS:
        return False
    if "{" in path or "}" in path:
        return False
    if path.startswith(EXCLUDED_PREFIXES):
        return False
    segments = {segment.lower() for segment in path.split("/") if segment}
    return not (segments & EXCLUDED_SEGMENTS)


def discover_routes() -> list[dict]:
    """扫描全部路由，返回可自动生成的只读接口清单（按路径排序）。"""
    seen: set[tuple[str, str]] = set()
    items: list[dict] = []
    for route in _all_routes():
        methods = [m for m in getattr(route, "methods", ()) if m in SAFE_METHODS]
        if not methods:
            continue
        path = getattr(route, "path", "")
        method = "GET" if "GET" in methods else methods[0]
        if not _is_candidate(method, path):
            continue
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "method": method,
            "path": path,
            "owner_only": OWNER_GUARD_NAME in _dependency_names(route),
        })
    return sorted(items, key=lambda item: item["path"])


def case_name(method: str, path: str) -> str:
    """auto 用例的展示名：就是「方法 + 路径」，不加前缀污染列表。"""
    return f"{method} {path}"


def build_plan(db) -> dict:
    """生成变更方案：新增 / 更新 / 跳过 / 下线四类，不写库。

    没有管理员或站长账号时返回 error——匿名生成的用例跑不出 200，只会得到一堆
    假的 401/303，不如不做。
    """
    acting = crud.highest_staff_account(db)
    if acting is None:
        return {"error": "库里没有管理员或站长账号，无法生成用例（请先用脚本创建）",
                "add": [], "update": [], "skip": [], "offline": []}

    is_owner = crud.has_role(acting.role, crud.ROLE_OWNER)
    routes = discover_routes()
    all_cases = {(case.method.upper(), case.path): case for case in crud.list_cases(db)}

    add: list[dict] = []
    update: list[dict] = []
    skip: list[dict] = []
    covered: set[tuple[str, str]] = set()

    for item in routes:
        if item["path"] in NEEDS_PARAMS_PATHS:
            # 不计入 covered：已存在的同名 auto 用例会被算作「下线」，可一键停用
            skip.append({**item, "reason": "需要查询参数才能返回 200，本阶段不生成"})
            continue
        key = (item["method"], item["path"])
        covered.add(key)
        if item["owner_only"] and not is_owner:
            skip.append({**item, "reason": "需要站长账号，当前库里最高权限账号不是站长"})
            continue
        existing = all_cases.get(key)
        if existing is None:
            add.append(item)
        elif existing.source == CASE_SOURCE_AUTO:
            update.append({**item, "case": existing})
        else:
            skip.append({**item, "reason": "已有人工用例，未改动"})

    offline = [
        case for key, case in all_cases.items()
        if case.source == CASE_SOURCE_AUTO and key not in covered
    ]
    # 探测：让生成页直接显示「这个地址现在返回什么」，人工过目才有依据（方案 6.3）。
    # update 也探测：期望值与实际不符时要报出来（drift），而不是悄悄改掉期望值。
    if add or update:
        from app import runner  # 延迟导入：runner 依赖 crud，放模块级会绕一圈

        sessions = runner.prepare_identities(db)
        for item in add:
            item["probe"] = probe_route(db, item, sessions)
        for item in update:
            item["probe"] = probe_route(db, item, sessions)
            actual = (item["probe"] or {}).get("status")
            current = item["case"].expected_status
            if actual is not None and actual != current:
                item["drift"] = {"expected": current, "actual": actual}
    return {"add": add, "update": update, "skip": skip, "offline": offline,
            "acting": acting}


def apply_plan(db, selections: list[str], confirm: bool = False) -> dict:
    """按勾选结果落库：新增 / 更新 / 停用，返回计数。

    selections 里的值形如 ``GET /api/health``（新增或更新）或 ``disable:<id>``（下线）。
    不在 selection 里的项一律不动，避免一口气改掉几十条。

    ``confirm=False``（默认）时新增用例进 ``pending_review`` 且不启用：
    探测出来的期望值必须经人工过目才生效（方案 6.3），否则一次错的首次探测
    会被固化成「正确」的期望，之后每批都替它开脱。
    """
    added = updated = disabled = 0
    plan = build_plan(db)
    if plan.get("error"):
        return {"added": 0, "updated": 0, "disabled": 0, "error": plan["error"]}

    wanted = set(selections)

    for item in plan["add"]:
        key = f"{item['method']} {item['path']}"
        if key not in wanted:
            continue
        expected, expect_range = _probe_values(
            (item.get("probe") or {}).get("status"),
            EXPECTED_STATUS_OVERRIDES.get(item["path"], EXPECTED_OK),
        )
        crud.create_case(
            db,
            TestCaseCreate(
                name=case_name(item["method"], item["path"]),
                method=item["method"],
                path=item["path"],
                expected_status=expected,
                enabled=confirm,
            ),
            source=CASE_SOURCE_AUTO,
            # owner-only 接口只能用站长跑；标错身份会得到 403/303 并被当成真实失败。
            identity="owner" if item["owner_only"] else "staff",
            case_status=CASE_STATUS_ACTIVE if confirm else CASE_STATUS_PENDING,
            expect_range=expect_range,
        )
        added += 1

    for item in plan["update"]:
        key = f"{item['method']} {item['path']}"
        if key not in wanted:
            continue
        case = item["case"]
        case.name = case_name(item["method"], item["path"])
        # 身份随守卫变化：路由加了 owner 守卫就要跟着改成 owner，反之回落 staff。
        case.identity = "owner" if item["owner_only"] else "staff"
        if case.case_status == CASE_STATUS_ACTIVE:
            # 只重新启用，**不覆盖期望值**：生成动作改成「报告差异」（plan 里的 drift），
            # 改期望值走人工编辑。否则接口真回归了，点一次生成就把 404 刷回 200，
            # 回归永远红不了——那正是这个平台要防的事。
            case.enabled = True
        db.commit()
        db.refresh(case)
        updated += 1

    for case in plan["offline"]:
        if f"disable:{case.id}" not in wanted:
            continue
        crud.set_case_enabled(db, case, False)
        disabled += 1

    return {"added": added, "updated": updated, "disabled": disabled}


def _probe_values(status: int | None,
                  fallback: int = EXPECTED_OK) -> tuple[int, str | None]:
    """探测结果 → (expected_status, expect_range)，按方案 6.3 的四类写入规则。

    3xx 写 302 占位 + ``'3xx'``：判定分支不读这个占位值，实际状态码只在
    生成页当场展示；探测失败（None）退回 fallback，交人工重点复查。
    """
    if status is None:
        return fallback, None
    if 300 <= status <= 399:
        return 302, "3xx"
    return status, None


def probe_route(db, item: dict, sessions: dict | None = None) -> dict:
    """实跑一次，返回 ``{"status", "error", "path"}``。

    探测不是判定：唯一用途是让生成页显示真实状态码供人工核对，所以任何
    异常都吞掉记 error，不让一条探测失败打乱整张生成页。
    """
    from app import runner  # 延迟导入：runner 依赖 crud，放模块级会绕一圈

    identity = item.get("identity") or (
        "owner" if item.get("owner_only") else "staff")
    sessions = sessions or runner.prepare_identities(db)
    session = sessions.get(identity) or {}
    if not session.get("available"):
        return {"status": None, "error": f"{identity} 身份不可用", "path": None}

    request_path = item["path"]
    if case_samples.needs_sample(request_path):
        request_path, reason, detail = runner.resolve_sample_path(
            db, request_path, sessions)
        if request_path is None:
            return {"status": None, "error": detail or reason, "path": None}

    try:
        response = runner.client_for(identity, session.get("token")).request(
            item["method"], request_path)
        return {"status": response.status_code, "error": None, "path": request_path}
    except Exception as exc:  # noqa: BLE001 —— 单条探测失败不该中断整张列表
        return {"status": None, "error": f"{type(exc).__name__}: {exc}",
                "path": request_path}


def sync_auto_identities(db) -> int:
    """按当前路由守卫纠正 auto 用例的执行身份，返回改动条数（幂等）。

    为什么需要：二期给 ``identity`` 回填的默认值是 staff，但 owner-only 路由
    （如 /admin/roles）用 staff 跑必然 403。只在「从接口清单生成」时纠正还不够——
    用户可能只生成异常用例、不点常规生成，那条用例就会一直红着。
    只改 auto 用例：manual 用例的身份是人工定的，不自动覆盖。
    """
    owners = {item["path"] for item in discover_routes() if item["owner_only"]}
    fixed = 0
    for case in crud.list_cases(db):
        if case.source != CASE_SOURCE_AUTO:
            continue
        want = "owner" if case.path in owners else "staff"
        if case.identity != want:
            case.identity = want
            fixed += 1
    if fixed:
        db.commit()
    return fixed


def confirm_cases(db, case_ids: list[int], operator_id: int | None = None) -> dict:
    """把待确认用例转正：active + 启用，并记录确认人与时间（方案 6.3 第 4 步）。

    只处理 ``pending_review``：已驳回的用例不能凭一次点击复活，
    否则“确认不采用”和“还没看”又混成一类。
    ``operator_id`` 必须由调用方从**登录会话**取——页面传 request.state.current_user.id，
    脚本传 None（只记时间、不记人）。不接受请求体里的 confirmed_by。
    """
    from datetime import datetime

    confirmed = 0
    for case_id in case_ids:
        case = crud.get_case(db, case_id)
        if case is None or case.case_status != CASE_STATUS_PENDING:
            continue
        case.case_status = CASE_STATUS_ACTIVE
        case.enabled = True
        case.confirmed_at = datetime.now()
        case.confirmed_by = operator_id
        confirmed += 1
    db.commit()
    return {"confirmed": confirmed}


def reject_cases(db, case_ids: list[int]) -> dict:
    """驳回待确认用例：rejected + 停用，保留记录以便追溯集合变化。"""
    rejected = 0
    for case_id in case_ids:
        case = crud.get_case(db, case_id)
        if case is None or case.case_status != CASE_STATUS_PENDING:
            continue
        case.case_status = CASE_STATUS_REJECTED
        case.enabled = False
        rejected += 1
    db.commit()
    return {"rejected": rejected}


# 不依赖样本的异常用例：(method, path, identity, 说明)。
# 期望值一律靠探测得来，不在这里写死——本项目页面路由走 RedirectResponse，
# 匿名访问后台是 303 不是 401，按惯例写会得到一条永远红的用例。
ANOMALY_MANUAL = (
    ("GET", "/api/contents/99999999", "staff", "不存在的内容 id，期望 404"),
)


def _anomaly_items() -> list[dict]:
    """异常用例的三个来源：越权访问后台 + 带参接口取真实样本 + 手工清单。"""
    from app.main import app

    paths = app.openapi()["paths"]
    items: list[dict] = []
    for path in sorted(paths):
        if path.startswith("/manage") and "get" in paths[path]:
            items.append({"method": "GET", "path": path, "identity": "anon",
                          "owner_only": False, "note": "匿名访问后台页面"})
    for path in sorted(paths):
        if "get" not in paths[path] or not case_samples.needs_sample(path):
            continue
        if case_samples.spec_for(path) is None:  # 未配置样本的一律不生成
            continue
        items.append({"method": "GET", "path": path, "identity": "staff",
                      "owner_only": False, "note": "带路径参数，取真实样本"})
    for method, path, identity, note in ANOMALY_MANUAL:
        items.append({"method": method, "path": path, "identity": identity,
                      "owner_only": False, "note": note})
    return items


def build_anomaly_plan(db) -> dict:
    """异常用例候选：探测后返回 add 列表，结构与 build_plan 的 add 同构。

    已存在同 method + path + identity 的用例则不再列出——同一条路径可以同时
    有 staff 与 anon 两条用例，它们的期望值本来就不同。
    """
    from app import runner

    existing = {(c.method.upper(), c.path, (c.identity or "staff"))
                for c in crud.list_cases(db)}
    sessions = runner.prepare_identities(db)
    add = []
    for item in _anomaly_items():
        if (item["method"], item["path"], item["identity"]) in existing:
            continue
        item["probe"] = probe_route(db, item, sessions)
        add.append(item)
    return {"add": add}


def apply_anomaly_plan(db, selections: list[str], confirm: bool = False) -> dict:
    """落库异常用例：写 pending_review（或 confirm 时直接 active）。

    来源标 ``manual``：它们是人工确认过的一组用例，不该被下次路由扫描当成
    「接口下线」而停用（方案第十节待定项 3 定为 manual）。
    """
    wanted = set(selections)
    added = 0
    for item in build_anomaly_plan(db)["add"]:
        if f"{item['method']} {item['path']}" not in wanted:
            continue
        expected, expect_range = _probe_values(
            (item.get("probe") or {}).get("status"))
        crud.create_case(
            db,
            TestCaseCreate(
                name=case_name(item["method"], item["path"]),
                method=item["method"],
                path=item["path"],
                expected_status=expected,
                enabled=confirm,
            ),
            source=crud.CASE_SOURCE_MANUAL,
            identity=item["identity"],
            case_status=CASE_STATUS_ACTIVE if confirm else CASE_STATUS_PENDING,
            expect_range=expect_range,
        )
        added += 1
    return {"added": added}
