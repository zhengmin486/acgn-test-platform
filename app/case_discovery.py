"""用例自动生成：从应用自身的路由表发现「只读接口」，生成 / 更新 / 下线用例。

设计约束（按 2026-10-18 与用户确认的方案）：

- **只发现 GET / HEAD，且路径不带参数**：带 `{id}` 的接口需要示例值，本阶段一律跳过
  （页面里会单列出来，想加就手动勾一条并填 id）；
- **排除写操作与动作类路径**（delete / execute / apply / clear …）：执行器发的是真请求，
  自动生成一条删除就是真删库；
- **期望状态码按管理员身份推断**：有守卫的接口管理员可达，一律 200；
  owner-only 接口在库里没有站长账号时直接跳过，不猜一个错的值；
- **auto 用例只更新、不重建**：回归对比按 case_id 对齐两次批次，删了重建会让基线全变
  「新增 / 缺失」，回归功能直接失效。接口下线时只停用。
- **manual 用例永不改动**：不改名、不改期望值、不停用、不删除。
"""

from app import crud
from app.models import CASE_SOURCE_AUTO
from app.schemas import TestCaseCreate

# 只生成这两种方法：幂等、只读，误跑也不会改数据。
SAFE_METHODS = ("GET", "HEAD")

# 路径片段黑名单：即使方法是 GET 也跳过（这些是会改变状态的动作，或认证流程入口）。
EXCLUDED_SEGMENTS = frozenset({
    "delete", "execute", "apply", "clear", "remove", "resync",
    "verify", "revoke", "generate", "logout", "login", "register",
})

# 前缀黑名单：静态资源与外链代理，状态码取决于文件是否存在，属于噪声。
EXCLUDED_PREFIXES = ("/media/", "/static", "/covers")

# 参数全部可选、但业务上必须有值才返回的接口（空参直接 400）。
# 与带 {id} 的接口同理：本阶段不生成，等「用例参数」功能再做。
NEEDS_PARAMS_PATHS = frozenset({"/api/contents/search"})

# 期望状态码：管理员视角下，被发现出来的接口都应可访问。
EXPECTED_OK = 200

# owner-only 守卫的函数名（用于判断「需要站长账号」）。
OWNER_GUARD_NAME = "require_owner_page"


def _all_routes():
    """取全部待扫描的路由对象；延迟导入页面模块，避免与 app.main 形成循环依赖。"""
    from app import (
        api,
        business_pages,
        cases,
        content_admin_pages,
        site_admin_pages,
        space_pages,
        submission_pages,
        testing_pages,
    )

    modules = (api, business_pages, cases, content_admin_pages, site_admin_pages,
               space_pages, submission_pages, testing_pages)
    for module in modules:
        for route in getattr(module, "router").routes:
            yield route


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
    return {"add": add, "update": update, "skip": skip, "offline": offline,
            "acting": acting}


def apply_plan(db, selections: list[str]) -> dict:
    """按勾选结果落库：新增 / 更新 / 停用，返回计数。

    selections 里的值形如 ``GET /api/health``（新增或更新）或 ``disable:<id>``（下线）。
    不在 selection 里的项一律不动，避免一口气改掉几十条。
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
        crud.create_case(
            db,
            TestCaseCreate(
                name=case_name(item["method"], item["path"]),
                method=item["method"],
                path=item["path"],
                expected_status=EXPECTED_OK,
                enabled=True,
            ),
            source=CASE_SOURCE_AUTO,
        )
        added += 1

    for item in plan["update"]:
        key = f"{item['method']} {item['path']}"
        if key not in wanted:
            continue
        case = item["case"]
        case.name = case_name(item["method"], item["path"])
        case.expected_status = EXPECTED_OK
        case.enabled = True  # 接口回来了就重新启用
        db.commit()
        db.refresh(case)
        updated += 1

    for case in plan["offline"]:
        if f"disable:{case.id}" not in wanted:
            continue
        crud.set_case_enabled(db, case, False)
        disabled += 1

    return {"added": added, "updated": updated, "disabled": disabled}
