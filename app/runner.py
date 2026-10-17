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

import time
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import crud
from app.auth import SESSION_COOKIE
from app.config import settings
from app.models import ExecutionBatch, ExecutionRecord, TestCase
from app.security import create_session_token

_MAX_ERROR_CHARS = 200

_client: TestClient | None = None


def _get_client() -> TestClient:
    """进程内客户端：固定指向本应用；按当前库支持的能力降级构造参数。"""
    global _client
    if _client is None:
        from app.main import app  # 延迟导入避免循环依赖

        try:
            _client = TestClient(app, follow_redirects=False, timeout=5.0)
        except TypeError:
            # 旧参数顺序探测，均只在构造期发生，无请求副作用
            try:
                _client = TestClient(app, follow_redirects=False)
            except TypeError:
                _client = TestClient(app)
    return _client


def _is_local_path(path: str) -> bool:
    """与用例校验一致的本地路径判定，执行前做第二道防线。"""
    return (
        path.startswith("/")
        and not path.startswith("//")
        and "://" not in path
        and not any(ch.isspace() for ch in path)
    )


def _apply_identity(db: Session) -> None:
    """给进程内客户端带上身份：以权限最高的 staff 账号执行。

    测试工具是管理员用的——匿名身份只能测到一堆 401/303，测不到后台接口本身。
    库里没有管理员账号时清空身份，退回匿名（此时 401 就是真实结果，不伪装）。
    """
    client = _get_client()
    account = crud.highest_staff_account(db)
    if account is None:
        client.cookies.clear()
        return
    client.cookies.set(
        SESSION_COOKIE, create_session_token(account.id, settings.auth_secret_key)
    )


def run_case(db: Session, case: TestCase, batch_id: int | None = None) -> ExecutionRecord:
    """执行单条用例：发请求、比对预期状态码、保存记录并返回（已含主键）。

    batch_id 默认 None 表示单条执行；批次执行传入批次 ID 以关联记录。
    """
    started = time.perf_counter()
    actual: int | None = None
    passed = False
    error: str | None = None
    if not _is_local_path(case.path):
        error = "路径不合法：仅允许本站本地路径，未发送请求"
    else:
        try:
            _apply_identity(db)
            response = _get_client().request(case.method, case.path)
            actual = response.status_code
            passed = actual == case.expected_status
            if not passed:
                error = f"实际状态码 {actual} 与预期 {case.expected_status} 不一致"
        except Exception as exc:  # noqa: BLE001 —— 执行器兜底：任何异常都记为失败，绝不误判为通过
            error = f"请求异常：{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            passed = False
    duration_ms = (time.perf_counter() - started) * 1000
    record = ExecutionRecord(
        case_id=case.id,
        batch_id=batch_id,
        case_name=case.name,
        method=case.method,
        path=case.path,
        expected_status=case.expected_status,
        actual_status=actual,
        duration_ms=duration_ms,
        passed=passed,
        error=error,
    )
    return crud.add_run(db, record)


def run_batch(db: Session) -> ExecutionBatch:
    """把全部启用用例作为一批执行；返回带统计的批次。

    每条用例独立执行并立即落库：单条失败/异常只影响该条记录，
    不影响其余用例，也不会丢失整批结果；用例集为空时同样
    创建 total=0 的有效批次。单条执行（batch_id=None）不计入批次。
    """
    batch = ExecutionBatch(started_at=datetime.now())
    db.add(batch)
    db.commit()
    db.refresh(batch)

    cases = [c for c in crud.list_cases(db) if c.enabled]
    passed = 0
    failed = 0
    for case in cases:
        record = run_case(db, case, batch_id=batch.id)
        if record.passed:
            passed += 1
        else:
            failed += 1

    batch.total = len(cases)
    batch.passed = passed
    batch.failed = failed
    batch.finished_at = datetime.now()
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


def _batch_pass_rate(batch: ExecutionBatch) -> float | None:
    """批次通过率（百分比，保留 1 位小数）；无用例时为 None。"""
    return round(batch.passed / batch.total * 100, 1) if batch.total else None


def _empty_summary() -> dict:
    """六类变化的零值计数，保证汇总键结构稳定（无可比较批次时同构返回）。"""
    return {change: 0 for change in CHANGE_TYPES}


def compare_batches(db: Session, current: ExecutionBatch) -> dict:
    """当前批次与之前最近的有效批次（非空）做回归对比。

    以稳定的 case_id 为主键对齐（用例集合不一致时逐条判定）：
    - missing：当前批次没有该用例记录（不算通过）；
    - added：上一批没有该用例记录（视为新增）；
    - regression：上批通过、本批失败（新增失败）；
    - fixed：上批失败、本批通过（已修复失败）；
    - unchanged_fail：两次均失败（持续失败）；
    - unchanged_pass：两次均通过（持续通过）。

    返回结构：
    - comparable / reason：是否可对比及原因（当前批次为空、无更早有效批次）；
    - pass_rate：上批/本批通过率及变化（百分点，保留 1 位小数）；
    - failure_change：上批/本批失败数及变化（正数代表失败变多）；
    - summary：六类变化的计数，直接对应四个核心分类；
    - items：逐用例差异明细（无论是否可对比，items 恒为列表）。
    无可比较批次或当前批次为空时 comparable=False，items 为空、summary 全零，
    不编造任何对比数据。
    """
    if current.total == 0:
        return {"comparable": False, "reason": "当前批次没有用例，无法对比",
                "items": [], "summary": _empty_summary()}
    previous = crud.get_previous_valid_batch(db, current.id)
    if previous is None:
        return {"comparable": False, "reason": "没有更早的有效批次可供对比",
                "items": [], "summary": _empty_summary()}

    current_runs = {r.case_id: r for r in crud.list_runs_for_batch(db, current.id)}
    previous_runs = {r.case_id: r for r in crud.list_runs_for_batch(db, previous.id)}
    items = []
    summary = _empty_summary()
    for case_id in sorted(set(current_runs) | set(previous_runs)):
        current_run = current_runs.get(case_id)
        previous_run = previous_runs.get(case_id)
        if current_run is None:
            change = "missing"
            detail = "当前批次没有该用例的记录（不计为通过）"
        elif previous_run is None:
            change = "added"
            detail = "上一批次没有该用例的记录（新增用例）"
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

    current_rate = _batch_pass_rate(current)
    previous_rate = _batch_pass_rate(previous)
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
    }
