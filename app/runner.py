"""测试执行器：对当前应用自身发起进程内请求并落库执行记录。

安全边界（第一版）：
- 仅向本应用的 ASGI 实例发请求：无真实网络出口，访问不到任何外部主机/内网地址；
- 执行前再次校验路径必须是本站本地路径（与用例管理同一规则，防脏数据）；
- 不跟随重定向；本版不读取响应体（仅比较状态码，读取量为 0）；
- 任何异常（超时/请求错误/应用异常）都记为失败，绝不误判为通过。

已知限制：当前 starlette TestClient 构造器不支持 timeout 参数
（TypeError），且进程内执行没有可超时的网络 I/O；超时保护待
执行器接入真实 HTTP 服务时再以 httpx 原生超时启用。
"""

import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import case_samples, crud
from app.auth import SESSION_COOKIE
from app.config import settings
from app.models import CASE_SET_SCHEMA, ExecutionBatch, ExecutionRecord, TestCase, batch_pass_rate
from app.security import create_session_token

_MAX_ERROR_CHARS = 200

# 失败原因前缀（方案 6.4）：集中定义，归因层按它归一。半角冒号。
# 判定结果另写 case_set.cases[].outcome（pass/fail/redirect），页面只读它不重算。
ERR_SERVER = "服务端错误:"
ERR_STATUS = "状态码不符:"
ERR_APP = "应用异常:"
ERR_REQUEST = "请求异常:"
ERR_PATH = "路径不合法:"
ERR_TOOL = "工具异常:"

IDENTITY_ANON = "anon"
IDENTITY_STAFF = "staff"
IDENTITY_OWNER = "owner"
DEFAULT_IDENTITY = IDENTITY_STAFF
_IDENTITIES = (IDENTITY_ANON, IDENTITY_STAFF, IDENTITY_OWNER)

# 按身份缓存客户端：每个实例各自独立的 cookie jar（方案 6.6）。
_clients: dict[str, TestClient] = {}


def _new_client() -> TestClient:
    """进程内客户端：固定指向本应用；按当前库支持的能力降级构造参数。"""
    from app.main import app  # 延迟导入避免循环依赖

    try:
        return TestClient(app, follow_redirects=False, timeout=5.0)
    except TypeError:
        # 旧参数顺序探测，均只在构造期发生，无请求副作用
        try:
            return TestClient(app, follow_redirects=False)
        except TypeError:
            return TestClient(app)


def _is_local_path(path: str) -> bool:
    """与用例校验一致的本地路径判定，执行前做第二道防线。"""
    return (
        path.startswith("/")
        and not path.startswith("//")
        and "://" not in path
        and not any(ch.isspace() for ch in path)
    )


def client_for(identity: str, token: str | None) -> TestClient:
    """取该身份专属的客户端，并重置成本次会话的 Cookie。

    每个身份一个实例：anon 与 staff 共用一个 client 时，匿名用例会带着上一条
    staff 用例留下的 Cookie 跑，越权结果直接失真——这是最难发现的一类假通过。
    """
    client = _clients.get(identity)
    if client is None:
        client = _new_client()
        _clients[identity] = client
    client.cookies.clear()
    if token:
        client.cookies.set(SESSION_COOKIE, token)
    return client


def prepare_identities(db: Session) -> dict[str, dict]:
    """批次开始前一次性解析三种身份的可用性（方案 6.6）。

    staff 取「非站长」的最高权限账号，owner 取站长；任一不可用都**不降级**，
    调用方记 not_run / identity_unavailable。用别的身份顶替会把「根本没验证
    staff 权限」报成通过，比失败更危险。
    """
    accounts = crud.list_users(db)
    staff_pool = [u for u in accounts
                  if crud.has_role(u.role, crud.ROLE_ADMIN)
                  and not crud.has_role(u.role, crud.ROLE_OWNER)]
    owner_pool = [u for u in accounts if crud.has_role(u.role, crud.ROLE_OWNER)]

    def _pick(pool):
        if not pool:
            return {"available": False, "token": None}
        account = max(pool, key=lambda u: crud.role_rank(u.role))
        return {"available": True,
                "token": create_session_token(account.id, settings.auth_secret_key)}

    return {
        IDENTITY_ANON: {"available": True, "token": None},
        IDENTITY_STAFF: _pick(staff_pool),
        IDENTITY_OWNER: _pick(owner_pool),
    }


def _identity_of(case: TestCase) -> str:
    """用例的执行身份；脏值回落到 staff（列有默认值，正常不会走到）。"""
    identity = getattr(case, "identity", None) or DEFAULT_IDENTITY
    return identity if identity in _IDENTITIES else DEFAULT_IDENTITY


def resolve_sample_path(
    db: Session, template: str, sessions: dict[str, dict]
) -> tuple[str | None, str | None, str | None]:
    """把 ``/contents/{content_id}`` 渲染成 ``/contents/12``（方案 6.7）。

    返回 ``(path, reason, detail)``；path 为 None 时 reason 是 not_run 的原因。
    取样本本身也是发请求，所以同样按配置里的身份走独立 client。
    """
    spec = case_samples.spec_for(template)
    if spec is None:
        return None, "missing_sample", "该路径未配置参数样本，未生成可执行路径"

    values: dict[str, object] = {}
    for name, param in spec.get("params", {}).items():
        identity = param.get("identity") or DEFAULT_IDENTITY
        session = sessions.get(identity) or {}
        if not session.get("available"):
            return None, "identity_unavailable", f"取样本需要 {identity} 身份，但该身份不可用"
        try:
            response = client_for(identity, session.get("token")).request(
                "GET", param["source_path"], params=param.get("query") or {})
        except Exception as exc:  # noqa: BLE001 —— 取样本失败不该让整批中断
            return None, "invalid_sample", f"取样本请求失败：{type(exc).__name__}"
        if response.status_code != 200:
            return None, "missing_sample", f"取样本接口返回 {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return None, "invalid_sample", "取样本接口返回的不是 JSON"

        value, reason, detail = case_samples.pick_value(payload, param)
        if reason is not None:
            return None, reason, detail
        if param.get("type") == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return None, "invalid_sample", f"样本值 {value!r} 不是整数"
        values[name] = value

    path = case_samples.render_path(template, values)
    if "{" in path:
        return None, "invalid_sample", "渲染后仍残留未替换的占位符"
    if not _is_local_path(path):
        return None, "invalid_sample", "渲染后的路径不是本站本地路径"
    return path, None, None


def _classify_status(case: TestCase, actual: int) -> tuple[bool, str | None, str]:
    """按 expect_range 判定状态码，返回 (passed, error, outcome)。

    - expect_range='3xx' 且实际 3xx → redirect（passed=True 仅占位，expected_status 不读）；
    - expect_range='3xx' 但实际非 3xx → fail；
    - expect_range=NULL → 精确匹配（现有用例全走这条，行为与二期前一致）。
    """
    if case.expect_range == "3xx":
        if 300 <= actual <= 399:
            return True, None, "redirect"
        return False, f"{ERR_STATUS} 实际状态码 {actual} 非预期的 3xx 区间", "fail"
    passed = actual == case.expected_status
    if passed:
        return True, None, "pass"
    # 保留「不一致」三字：历史测试断言依赖这段文本，且它本身就准确。
    return False, f"{ERR_STATUS} 实际状态码 {actual} 与预期 {case.expected_status} 不一致", "fail"


def _execute_once(case: TestCase, request_path: str, identity: str,
                  token: str | None) -> tuple[int | None, bool, str | None, str, float]:
    """发一次请求并按判定口径归类，返回 (实际码, 通过, 错误, outcome, 耗时ms)。

    首次执行与第 4 步的复跑共用这一函数，保证两次判定口径完全一致；
    它不改任何记录，纯发请求 —— 复跑结果由调用方写进 `retries` 数组。
    """
    started = time.perf_counter()
    actual: int | None = None
    passed = False
    error: str | None = None
    outcome = "fail"  # 默认失败；只有判定通过/跳转才改写
    if not _is_local_path(request_path):
        error = f"{ERR_PATH} 仅允许本站本地路径，未发送请求"
    else:
        try:
            response = client_for(identity, token).request(
                case.method, request_path)
            actual = response.status_code
            # 5xx 一律失败、归 To Investigate（不猜根因）；先于区间判定。
            if actual is not None and actual >= 500:
                passed = False
                error = f"{ERR_SERVER} 服务端返回 {actual}"
            else:
                passed, error, outcome = _classify_status(case, actual)
        except Exception as exc:  # noqa: BLE001 —— 执行器兜底：任何异常都记为失败，绝不误判为通过
            error = f"{ERR_REQUEST} {type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            passed = False
    return actual, passed, error, outcome, (time.perf_counter() - started) * 1000


def _retry_after_failure(case: TestCase, request_path: str, identity: str,
                         token: str | None) -> str:
    """同一批次内失败后立即复跑 2 次，返回 retries JSON（方案 5.2 / 9.4）。

    复跑只写进数组，绝不覆盖首次的 passed / actual_status / error / duration_ms；
    执行口径与首次一致（共用 `_execute_once`）。样本路径沿用首次渲染的结果，
    不重新取样本——否则复跑之间取到不同 id，结果不可比。
    """
    attempts = []
    for attempt in (1, 2):
        actual, passed, error, _outcome, duration = _execute_once(
            case, request_path, identity, token)
        attempts.append({
            "attempt": attempt,
            "passed": passed,
            "actual_status": actual,
            "error": error,
            "duration_ms": round(duration, 3),
        })
    return json.dumps(attempts, ensure_ascii=False)


def _retry_kind(record: ExecutionRecord) -> str | None:
    """读一条失败记录的复跑标记：'flaky' / 'mixed' / None（方案 9.4）。

    - flaky：首次失败 + 连续 2 次复跑均通过（启发式「疑似不稳定」）；
    - mixed：复跑结果不一致（如 失败→通过→失败），单列一类，不猜稳定性；
    - 复跑不足 2 次、复跑记录损坏、首次通过的，返回 None。
    比较阶段只读这个已算好的标记，不重算 HTTP 判定。
    """
    if record.passed or not record.retries:
        return None
    try:
        attempts = json.loads(record.retries)
    except (ValueError, TypeError):
        return None
    if not isinstance(attempts, list) or len(attempts) != 2:
        return None
    flags = [isinstance(a, dict) and a.get("passed") is True for a in attempts]
    if all(flags):
        return "flaky"
    if any(flags):
        return "mixed"
    return None


def run_case(
    db: Session,
    case: TestCase,
    batch_id: int | None = None,
    judgment: dict | None = None,
    sessions: dict[str, dict] | None = None,
    retry_on_fail: bool = False,
) -> ExecutionRecord | None:
    """执行单条用例：发请求、比对预期状态码、保存记录并返回（已含主键）。

    batch_id 默认 None 表示单条执行；批次执行传入批次 ID 以关联记录。
    judgment 为可选的回传字典：批次执行时写入 outcome（pass/fail/redirect），
    供 case_set 收集；单条执行不传，行为与二期前完全一致。
    sessions 是批次预解析的身份结果，不传则当场解析。
    retry_on_fail 仅批次执行传 True：首次失败后立刻复跑 2 次写进 `retries`，
    首次结果、批次计数、通过率与最慢统计均不受影响（方案 5.2 / 9.4）。
    **身份不可用时不发请求、不写记录，返回 None**（方案 6.6），绝不降级。
    """
    identity = _identity_of(case)
    sessions = sessions or prepare_identities(db)
    session = sessions.get(identity) or {}
    if not session.get("available"):
        if judgment is not None:
            judgment["outcome"] = "not_run"
            judgment["reason"] = "identity_unavailable"
        return None

    # 带占位符的路径先取样本；取不到就不发请求，不猜一个假 id。
    request_path = case.path
    if case_samples.needs_sample(request_path):
        request_path, reason, detail = resolve_sample_path(db, case.path, sessions)
        if request_path is None:
            if judgment is not None:
                judgment["outcome"] = "not_run"
                judgment["reason"] = reason
                judgment["detail"] = detail
            return None

    actual, passed, error, outcome, duration_ms = _execute_once(
        case, request_path, identity, session.get("token"))
    retries_json = (_retry_after_failure(case, request_path, identity, session.get("token"))
                    if retry_on_fail and not passed else None)
    record = ExecutionRecord(
        case_id=case.id,
        batch_id=batch_id,
        case_name=case.name,
        method=case.method,
        path=request_path,  # 存渲染后的实际路径，事后能复核到底请求了哪个 id
        expected_status=case.expected_status,
        actual_status=actual,
        duration_ms=duration_ms,
        passed=passed,
        error=error,
        retries=retries_json,
    )
    if judgment is not None:
        judgment["outcome"] = outcome
    return crud.add_run(db, record)


def _collect_meta(db: Session, sessions: dict[str, dict]) -> str:
    """批次元数据 JSON：commit / dirty / result_schema / runtime / selection。

    commit 识别不了未提交改动，dirty 单独标；runtime=real 表示真实执行
    （本版没有缓存，留字段供将来缓存命中批次排除出回归对比）。
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    try:
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=3,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = None
    return json.dumps({
        "commit": commit or None,
        "dirty": dirty,
        "result_schema": CASE_SET_SCHEMA,
        "runtime": "real",
        "selection": "all",
        # 身份准备的真实结果：缺失的身份对应用例会记 not_run / identity_unavailable
        "identity_prepare": {
            identity: ("ok" if data.get("available") else "missing")
            for identity, data in sessions.items()
        },
    }, ensure_ascii=False)


def _collect_env(db: Session) -> str:
    """运行环境 JSON：python / platform / db_path（相对路径落绝对）。"""
    db_path = getattr(getattr(db, "bind", None), "url", None)
    try:
        path = str(db_path.database) if db_path is not None else None
        if path and path != ":memory:" and not os.path.isabs(path):
            path = os.path.abspath(path)
    except AttributeError:
        path = None
    return json.dumps({
        "python": ".".join(platform.python_version_tuple()[:3]),
        "platform": f"{platform.system()} {platform.release()}",
        "db_path": path,
    }, ensure_ascii=False)


def _build_case_set(entries: list[dict], eligible_ids: list[int]) -> str:
    """由执行阶段收集的结果生成 case_set JSON，并断言两条恒等式。

    entries 中每条为 {case_id, state, outcome|reason, run_id}；
    eligible_ids 是进候选集的用例 id（active + enabled=1）。
    """
    cases = sorted(entries, key=lambda e: e["case_id"])
    eligible = set(eligible_ids)
    executed = [e for e in cases if e["state"] == "executed"]
    not_run = [e for e in cases if e["state"] == "not_run" and e["case_id"] in eligible]
    disabled = [e for e in cases if e.get("reason") == "disabled"]
    pending = [e for e in cases if e.get("reason") == "not_confirmed"]
    rejected = [e for e in cases if e.get("reason") == "rejected"]
    counts = {
        "case_count": len(cases),
        "eligible_count": len(eligible),
        "executed_count": len(executed),
        "redirect_count": sum(1 for e in executed if e.get("outcome") == "redirect"),
        "not_run_count": len(not_run),
        "disabled_count": len(disabled),
        "pending_count": len(pending),
        "rejected_count": len(rejected),
    }
    # 两条恒等式：口径不自洽就当场炸，不把错误数据写进库（方案 5.3）。
    assert counts["case_count"] == (
        counts["eligible_count"] + counts["disabled_count"]
        + counts["pending_count"] + counts["rejected_count"]
    ), f"case_set 恒等式 1 不成立: {counts}"
    assert counts["eligible_count"] == counts["executed_count"] + counts["not_run_count"], (
        f"case_set 恒等式 2 不成立: {counts}"
    )
    return json.dumps({
        "schema": CASE_SET_SCHEMA,
        "case_ids": [e["case_id"] for e in cases],
        "counts": counts,
        "cases": cases,
    }, ensure_ascii=False)


def run_batch(db: Session) -> ExecutionBatch:
    """把全部启用用例作为一批执行；返回带统计的批次。

    每条用例独立执行并立即落库：单条失败/异常只影响该条记录，
    不影响其余用例，也不会丢失整批结果；用例集为空时同样
    创建 total=0 的有效批次。单条执行（batch_id=None）不计入批次。

    二期：额外写 case_set / meta / env。身份在批次开始时一次性解析，
    不可用的身份**不发请求、不降级**，其用例记 not_run / identity_unavailable
    ——这类用例仍在 eligible 里（本该执行），只是没执行，故分母会排除它。
    """
    # auto 用例的身份跟随路由守卫纠正一次（幂等）：回填默认值是 staff，
    # 但 owner-only 路由用 staff 跑必然 403，不能等用户点「从接口清单生成」才修。
    from app import case_discovery  # 延迟导入：case_discovery 依赖本模块

    case_discovery.sync_auto_identities(db)

    batch = ExecutionBatch(started_at=datetime.now())
    db.add(batch)
    db.commit()
    db.refresh(batch)

    sessions = prepare_identities(db)
    all_cases = crud.list_cases(db)
    # 底稿：四类生命周期状态各自归位，再按身份可用性分流。
    entries: list[dict] = []
    eligible_ids: list[int] = []
    candidates: list[TestCase] = []
    for case in all_cases:
        status = case.case_status or "active"
        if status == "active" and case.enabled:
            eligible_ids.append(case.id)
            if not (sessions.get(_identity_of(case)) or {}).get("available"):
                entries.append({"case_id": case.id, "state": "not_run",
                                "reason": "identity_unavailable"})
                continue
            candidates.append(case)
            entries.append({"case_id": case.id, "state": "executed", "outcome": "fail"})
        elif status == "active":
            entries.append({"case_id": case.id, "state": "not_run", "reason": "disabled"})
        elif status in ("probing", "pending_review"):
            entries.append({"case_id": case.id, "state": "not_run", "reason": "not_confirmed"})
        else:  # rejected
            entries.append({"case_id": case.id, "state": "not_run", "reason": "rejected"})

    passed = 0
    failed = 0
    by_id = {e["case_id"]: e for e in entries}
    for case in candidates:
        judgment: dict = {}
        record = run_case(db, case, batch_id=batch.id, judgment=judgment,
                          sessions=sessions, retry_on_fail=True)
        entry = by_id[case.id]
        if record is None:  # 身份不可用或取不到参数样本：没有发请求，记 not_run
            entry["state"] = "not_run"
            entry["reason"] = judgment.get("reason", "missing_sample")
            entry.pop("outcome", None)
            continue
        entry["outcome"] = judgment.get("outcome", "fail")
        entry["run_id"] = record.id
        if record.passed:
            passed += 1
        else:
            failed += 1

    batch.total = len(candidates)
    batch.passed = passed
    batch.failed = failed
    batch.finished_at = datetime.now()
    batch.meta = _collect_meta(db, sessions)
    batch.env = _collect_env(db)
    batch.case_set = _build_case_set(entries, eligible_ids)
    db.commit()
    db.refresh(batch)
    return batch


# 回归对比的变化类型全集；前四项为核心分类（新增失败/已修复失败/持续失败/持续通过），
# 后两项用于表达两次批次间用例集合不一致的情况（新增用例/缺失用例）。
CHANGE_TYPES = (
    "regression",
    "fixed",
    "unchanged_fail",
    "unchanged_pass",
    "added",
    "missing",
)


def _snapshot_of(batch: ExecutionBatch) -> dict | None:
    """解析判定快照；legacy（无快照或结构版本不符）返回 None。

    方案 5.6：legacy 批次不伪造快照、不套新口径，所以缺快照时必须回退，
    不能拿 total/passed 去猜 eligible/redirect/not_run。
    """
    if not batch.case_set:
        return None
    try:
        data = json.loads(batch.case_set)
    except (ValueError, TypeError):
        return None
    return data if data.get("schema") == CASE_SET_SCHEMA else None


def _empty_summary() -> dict:
    """六类变化的零值计数，保证汇总键结构稳定（无可比较批次时同构返回）。"""
    return {change: 0 for change in CHANGE_TYPES}


def compare_batches(db: Session, current: ExecutionBatch) -> dict:
    """当前批次与之前最近的有效批次（非空）做回归对比。

    两条取数路径（方案 5.3 / 5.6）：

    - 两批都有 `case_set`：以快照的用例全集与 outcome 为准。只有快照里
      根本不存在该 case_id 才算 `missing`（真删除）；停用 / 未确认 / 驳回 /
      缺样本 / 身份不可用等 `not_run` 不进六类，单列明细；跳转也不进六类。
    - 任一批是 legacy：没有快照，退回 `test_runs` 的旧口径，并标
      `set_incomplete=True`，页面明示 added/missing 不可信。

    六类语义：regression（上批通过本批失败）、fixed、unchanged_fail、
    unchanged_pass、added、missing；summary 只含这六类，其余状态在
    items 里以各自 change 值呈现但不计数。
    """
    if current.total == 0:
        return {"comparable": False, "reason": "当前批次没有用例，无法对比",
                "items": [], "summary": _empty_summary(), "set_incomplete": False}
    previous = crud.get_previous_valid_batch(db, current.id)
    if previous is None:
        return {"comparable": False, "reason": "没有更早的有效批次可供对比",
                "items": [], "summary": _empty_summary(), "set_incomplete": False}

    current_snap = _snapshot_of(current)
    previous_snap = _snapshot_of(previous)
    use_snapshots = current_snap is not None and previous_snap is not None

    current_runs = {r.case_id: r for r in crud.list_runs_for_batch(db, current.id)}
    previous_runs = {r.case_id: r for r in crud.list_runs_for_batch(db, previous.id)}
    items = []
    summary = _empty_summary()

    if use_snapshots:
        # 新口径：快照里的用例全集才是对齐基准，test_runs 只用来取名称与错误文本。
        cur_entries = {e["case_id"]: e for e in current_snap.get("cases", [])}
        prev_entries = {e["case_id"]: e for e in previous_snap.get("cases", [])}
        for case_id in sorted(set(cur_entries) | set(prev_entries)):
            cur = cur_entries.get(case_id)
            prev = prev_entries.get(case_id)
            run = current_runs.get(case_id) or previous_runs.get(case_id)
            name = run.case_name if run is not None else f"用例 {case_id}"
            if cur is None:
                change, detail = "missing", "当前批次快照中无此用例（已删除），计为缺失"
                cur_passed = prev_passed = None
            elif prev is None:
                change, detail = "added", "上一批次快照中无此用例（新增用例）"
                cur_passed = prev_passed = None
            elif cur.get("state") == "not_run":
                change = "not_run"
                detail = f"本批未执行（{cur.get('reason')}），不参与六类对比"
                cur_passed = prev_passed = None
            elif prev.get("state") == "not_run":
                change = "not_run"
                detail = f"上批未执行（{prev.get('reason')}），不参与六类对比"
                cur_passed = prev_passed = None
            elif cur.get("outcome") == "redirect" or prev.get("outcome") == "redirect":
                change = "redirect"
                detail = "存在跳转记录（单独记录，不进通过/失败比较）"
                cur_passed = prev_passed = None
            else:
                co, po = cur.get("outcome"), prev.get("outcome")
                if po == "pass" and co == "fail":
                    # 9.4：复跑标记只读不重算。flaky / mixed 从 regression 扣除，
                    # 单列一行但不进六类 summary；通过率仍按首次 fail 计（不变）。
                    retry_kind = (_retry_kind(current_runs[case_id])
                                  if case_id in current_runs else None)
                    if retry_kind == "flaky":
                        change = "flaky"
                        detail = "疑似不稳定（首次失败 + 连续 2 次复跑通过），不计入回归；通过率仍按首次失败计"
                    elif retry_kind == "mixed":
                        change = "mixed"
                        detail = "复跑结果不一致，尚不能确认稳定性，单列不计入回归"
                    else:
                        change = "regression"
                        detail = (f"上批通过本次失败："
                                  f"{(current_runs[case_id].error or '')[:80]}"
                                  if case_id in current_runs else "上批通过本次失败")
                elif po == "fail" and co == "pass":
                    change, detail = "fixed", "上批失败本次通过"
                elif co == po == "pass":
                    change, detail = "unchanged_pass", "两次均通过，状态不变"
                elif co == po == "fail":
                    change, detail = "unchanged_fail", "两次均失败，状态不变"
                else:
                    change, detail = "not_run", "判定结果缺失，不参与六类对比"
                cur_passed, prev_passed = co == "pass", po == "pass"
            if change in summary:
                summary[change] += 1
            items.append({
                "case_id": case_id,
                "case_name": name,
                "change": change,
                "current_passed": cur_passed,
                "previous_passed": prev_passed,
                "detail": detail,
                "retry_kind": (_retry_kind(current_runs[case_id])
                               if change in ("flaky", "mixed")
                               and case_id in current_runs else None),
                "retries": (current_runs[case_id].retries
                            if change in ("flaky", "mixed")
                            and case_id in current_runs else None),
            })
    else:
        # legacy：无快照可还原集合，只能按执行记录比；added/missing 不可信。
        for case_id in sorted(set(current_runs) | set(previous_runs)):
            current_run = current_runs.get(case_id)
            previous_run = previous_runs.get(case_id)
            if current_run is None:
                change = "missing"
                detail = "当前批次没有该用例的记录（不计为通过）；旧批次集合不完整，此项不可信"
            elif previous_run is None:
                change = "added"
                detail = "上一批次没有该用例的记录（新增用例）；旧批次集合不完整，此项不可信"
            elif previous_run.passed and not current_run.passed:
                change = "regression"
                detail = f"上批通过本次失败：{current_run.error or ''}"
            elif not previous_run.passed and current_run.passed:
                change = "fixed"
                detail = "上批失败本次通过"
            elif current_run.passed:
                change = "unchanged_pass"
                detail = "两次均通过，状态不变"
            else:
                change = "unchanged_fail"
                detail = "两次均失败，状态不变"
            summary[change] += 1
            items.append({
                "case_id": case_id,
                "case_name": (current_run or previous_run).case_name,
                "change": change,
                "current_passed": current_run.passed if current_run else None,
                "previous_passed": previous_run.passed if previous_run else None,
                "detail": detail,
            })

    current_rate = batch_pass_rate(current)
    previous_rate = batch_pass_rate(previous)
    delta = (
        round(current_rate - previous_rate, 1)
        if current_rate is not None and previous_rate is not None
        else None
    )
    return {
        "comparable": True,
        "previous_batch_id": previous.id,
        "current_batch_id": current.id,
        "pass_rate": {
            "current": current_rate,
            "previous": previous_rate,
            "delta": delta,
        },
        "failure_change": {
            "current": current.failed,
            "previous": previous.failed,
            "delta": current.failed - previous.failed,
        },
        "summary": summary,
        "items": items,
        "set_incomplete": not use_snapshots,
    }
