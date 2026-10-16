"""统一「数据同步」任务的注册与执行入口（测试管理平台 → 数据管理 → 数据同步）。

设计要点（给后续维护者）：
- 本模块只做编排：范围校验 → 防重 → 运行记录 → 调用既有同步流程 → 汇总结果。
  三类数据的实际同步逻辑**一律复用既有实现**，不复制、不改写：
  * Bangumi 条目：scripts.import_bangumi.run_import
  * 新番档期：scripts.sync_yuc_schedule.run_sync
  * 官方资讯：scripts.import_articles.run_import_articles
- 预览（preview）与正式（apply）走同一条执行路径，只有 apply 开关不同；
  预览也会写一条 sync_task_runs 记录（便于审计），但绝不写业务数据。
- 防重：同一数据源同一时刻只允许一个任务在执行（进程内互斥锁），
  并且数据库里存在未结束的 running 记录时拒绝发起（超过 RUN_STALE_SECONDS
  的 running 记录视为僵死，不再拦截，避免一次异常把数据源永久锁死）。
- 幂等由既有同步逻辑保证（bangumi_id 唯一索引 / 档期 (season_code, source_key)
  唯一约束 / 资讯按 original_url、title 去重），因此重复点击「确认写入」
  不会产生重复数据，也不会覆盖本地不应覆盖的内容。
- 无第三方依赖：只用标准库 threading + 既有 SQLAlchemy 数据层。
- 将来接入定时任务时无需改动本模块：在进程启动处把任务注册进调度器，
  以相同参数调用 run_task(..., apply=True, trigger=TRIGGER_SCHEDULED) 即可。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime

from app import crud
from app.database import SessionLocal
from scripts.import_articles import (
    DEFAULT_LIMIT as ARTICLE_DEFAULT_LIMIT,
    ArticleImportSummary,
    list_categories,
    run_import_articles,
)
from scripts.import_bangumi import ImportSummary, run_import
from scripts.sync_yuc_schedule import SyncSummary, run_sync

# ---------- 任务标识与状态 ----------

TASK_BANGUMI = "bangumi"
TASK_YUC = "yuc_schedule"
TASK_ARTICLES = "articles"

MODE_PREVIEW = "preview"
MODE_APPLY = "apply"

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"  # 预留：接入定时任务后使用

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

STATUS_LABELS = {
    STATUS_RUNNING: "进行中",
    STATUS_OK: "成功",
    STATUS_PARTIAL: "部分成功",
    STATUS_FAILED: "失败",
}
STATUS_BADGES = {
    STATUS_RUNNING: "bg-secondary",
    STATUS_OK: "bg-success",
    STATUS_PARTIAL: "bg-warning text-dark",
    STATUS_FAILED: "bg-danger",
}
MODE_LABELS = {MODE_PREVIEW: "预览", MODE_APPLY: "正式写入"}

# 逐条结果状态 -> 中文标签（三类数据源的状态名在此统一，便于同一个模板渲染）
DETAIL_LABELS = {
    "added": "已新增",
    "updated": "已更新",
    "unchanged": "无变化",
    "duplicates": "重复跳过",
    "duplicate": "重复跳过",
    "skipped": "跳过",
    "failed": "失败",
    "pending": "待写入",
    "preview": "待写入",
    "existing": "已存在跳过",
    "imported": "已新增",
}

# 单次同步的范围上限：同步请求是阻塞式网络调用（页面会等待），因此限制批量大小。
MAX_BANGUMI_IDS = 30
# 资讯每条都要抓一次详情页（并发 4 线程，单条约 0.5-2 秒），限制单次条数以控制等待时间。
MAX_ARTICLE_LIMIT = 30
RUN_STALE_SECONDS = 600
DETAILS_MAX = 60  # 落库的逐条明细上限，避免记录无限膨胀

SEASON_CODE_RE = re.compile(r"^\d{4}(0[1-9]|1[0-2])$")


class ScopeError(ValueError):
    """同步范围参数不合法（页面直接回显原因，不发起同步）。"""


class TaskBusyError(RuntimeError):
    """同一数据源已有同步任务在执行。"""


# ---------- 任务注册表 ----------


@dataclass(frozen=True)
class TaskSpec:
    """一个可同步的数据源（表单渲染与执行分发共用这一份描述）。"""

    key: str
    label: str
    source: str
    scope_kind: str  # ids / season / articles
    scope_label: str
    scope_placeholder: str
    scope_help: str


TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        key=TASK_BANGUMI,
        label="Bangumi 条目",
        source="Bangumi 官方 API（api.bgm.tv）",
        scope_kind="ids",
        scope_label="Bangumi 条目 ID",
        scope_placeholder="265 975",
        scope_help=f"可填多个，用空格、逗号或换行分隔，最多 {MAX_BANGUMI_IDS} 个",
    ),
    TaskSpec(
        key=TASK_YUC,
        label="新番档期",
        source="Yuc.wiki 档期页（yuc.wiki）",
        scope_kind="season",
        scope_label="档期",
        scope_placeholder="202610",
        scope_help="选择或填写档期码（YYYYMM），一次同步一个档期",
    ),
    TaskSpec(
        key=TASK_ARTICLES,
        label="官方资讯",
        source="ANIPLEX NEWS BOX（news.aniplex.co.jp）",
        scope_kind="articles",
        scope_label="范围",
        scope_placeholder="",
        scope_help=(
            f"从 Aniplex 官方新闻站抓取，按站点栏目筛选，"
            f"留空取最新 {ARTICLE_DEFAULT_LIMIT} 条"
        ),
    ),
)


def get_task(task_key: str) -> TaskSpec | None:
    """按 key 取任务描述；未知 key 返回 None（由调用方决定回退或报错）。"""
    for spec in TASKS:
        if spec.key == task_key:
            return spec
    return None


def task_labels() -> dict[str, str]:
    """task_key -> 中文名称（历史列表渲染用）。"""
    return {spec.key: spec.label for spec in TASKS}


def article_categories() -> list[str]:
    """资讯站点支持的分类（= 站点栏目，供页面下拉）。"""
    return list_categories()


# ---------- 范围解析与校验 ----------


@dataclass(frozen=True)
class ParsedScope:
    """规范化后的同步范围：display 落库展示，其余字段供执行分发使用。"""

    display: str
    ids: tuple[int, ...] = ()
    season_code: str = ""
    category: str = ""
    limit: int = 0


def parse_bangumi_ids(text: str) -> list[int]:
    """解析 Bangumi ID 文本：空格/逗号/换行/顿号/分号分隔，去重保序。"""
    tokens = [token for token in re.split(r"[\s,，、;；]+", text or "") if token]
    if not tokens:
        raise ScopeError("请填写至少一个 Bangumi 条目 ID")
    ids: list[int] = []
    for token in tokens:
        if not token.isdigit() or int(token) <= 0:
            raise ScopeError(f"条目 ID 必须是正整数（无法识别：{token}）")
        value = int(token)
        if value not in ids:
            ids.append(value)
    if len(ids) > MAX_BANGUMI_IDS:
        raise ScopeError(f"单次最多同步 {MAX_BANGUMI_IDS} 个条目（当前 {len(ids)} 个）")
    return ids


def parse_season_code(text: str) -> str:
    """校验档期码：4 位年份 + 01-12 月份。"""
    code = (text or "").strip()
    if not code:
        raise ScopeError("请选择或填写档期")
    if not SEASON_CODE_RE.match(code):
        raise ScopeError(f"档期码格式应为 YYYYMM（如 202610），当前为「{code}」")
    return code


def parse_article_limit(text: str | None) -> int:
    """解析资讯条数上限：留空表示不限，超过 MAX_ARTICLE_LIMIT 直接拒绝。"""
    raw = (text or "").strip()
    if not raw:
        return 0
    if not raw.isdigit() or int(raw) <= 0:
        raise ScopeError(f"条数上限必须是正整数或留空（当前为「{raw}」）")
    value = int(raw)
    if value > MAX_ARTICLE_LIMIT:
        raise ScopeError(f"单次最多同步 {MAX_ARTICLE_LIMIT} 条资讯（当前 {value} 条）")
    return value


def prepare_scope(spec: TaskSpec, scope_text: str, limit_text: str | None = None) -> ParsedScope:
    """按任务类型校验并规范化范围参数；不合法时抛 ScopeError。"""
    if spec.scope_kind == "ids":
        ids = parse_bangumi_ids(scope_text)
        return ParsedScope(display=", ".join(str(i) for i in ids), ids=tuple(ids))

    if spec.scope_kind == "season":
        code = parse_season_code(scope_text)
        return ParsedScope(display=code, season_code=code)

    category = (scope_text or "").strip()
    limit = parse_article_limit(limit_text)
    if category and category not in article_categories():
        raise ScopeError(f"未知的资讯分类：{category}")
    display = f"分类={category or '全部'}，上限={limit or ARTICLE_DEFAULT_LIMIT}"
    return ParsedScope(display=display, category=category, limit=limit)


# ---------- 执行结果 ----------


@dataclass
class TaskResult:
    """一次同步任务的执行结果（预览与正式共用；字段名与 SyncTaskRun 对齐便于渲染）。"""

    task_key: str
    task_label: str
    source_label: str
    scope: str
    mode: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    skipped: int = 0
    pending: int = 0
    failed: int = 0
    message: str = ""
    details: list[dict] = field(default_factory=list)
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def status(self) -> str:
        """运行状态：整体失败 / 部分成功（有失败也有写入） / 成功。"""
        written = self.added + self.updated + self.pending
        if self.failed and not written:
            return STATUS_FAILED
        if self.failed:
            return STATUS_PARTIAL
        return STATUS_OK


# ---------- 防重锁 ----------

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _task_lock(task_key: str) -> threading.Lock:
    """取得（必要时创建）某个数据源的进程内互斥锁。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(task_key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[task_key] = lock
        return lock


# ---------- 汇总映射 ----------


def _join_reasons(prefix: str, reasons: list[str], limit: int = 5) -> str:
    """把失败/跳过原因拼成一句人类可读的说明（超出部分折叠）。"""
    if not reasons:
        return prefix
    head = "；".join(reasons[:limit])
    tail = f"；（其余 {len(reasons) - limit} 条省略）" if len(reasons) > limit else ""
    return f"{prefix}：{head}{tail}"


def _from_bangumi(summary: ImportSummary, spec: TaskSpec, parsed: ParsedScope, mode: str) -> TaskResult:
    """把 ImportSummary 映射为统一的 TaskResult。"""
    result = TaskResult(
        task_key=spec.key,
        task_label=spec.label,
        source_label=spec.source,
        scope=parsed.display,
        mode=mode,
        added=summary.imported,
        pending=summary.preview,
        duplicates=summary.duplicates,
        skipped=summary.skipped,
        failed=summary.failed,
        started_at=datetime.now(),
    )
    status_map = {
        "imported": "added",
        "preview": "pending",
        "duplicate": "duplicates",
        "skipped": "skipped",
        "failed": "failed",
    }
    result.details = [
        {
            "status": status_map.get(outcome.status, outcome.status),
            "title": outcome.title or f"#{outcome.bangumi_id}",
            "reason": outcome.reason,
        }
        for outcome in summary.outcomes[:DETAILS_MAX]
    ]
    if mode == MODE_PREVIEW:
        base = f"预览完成：可导入 {summary.importable} 条，未写入数据库"
    else:
        base = f"导入完成：新增 {summary.imported} 条"
    reasons = [o.reason for o in summary.outcomes if o.status in {"failed", "skipped"}]
    result.message = _join_reasons(base, reasons)
    return result


def _from_yuc(summary: SyncSummary, spec: TaskSpec, parsed: ParsedScope, mode: str) -> TaskResult:
    """把 SyncSummary 映射为统一的 TaskResult。"""
    result = TaskResult(
        task_key=spec.key,
        task_label=spec.label,
        source_label=spec.source,
        scope=parsed.display,
        mode=mode,
        added=summary.added,
        updated=summary.updated,
        unchanged=summary.unchanged,
        duplicates=summary.duplicates,
        skipped=summary.skipped,
        pending=summary.preview,
        failed=summary.failed,
        started_at=datetime.now(),
    )
    status_map = {"duplicate": "duplicates", "preview": "pending"}
    result.details = [
        {
            "status": status_map.get(outcome.status, outcome.status),
            "title": outcome.title or "（未识别条目）",
            "reason": outcome.reason,
        }
        for outcome in summary.outcomes[:DETAILS_MAX]
    ]
    if mode == MODE_PREVIEW:
        base = (
            f"预览完成：档期 {summary.season_code} 解析 {summary.valid} 条有效记录，"
            "未写入新番数据"
        )
    else:
        base = (
            f"同步完成：新增 {summary.added} 条、更新 {summary.updated} 条、"
            f"无变化 {summary.unchanged} 条"
        )
    result.message = _join_reasons(base, [o.reason for o in summary.issues])
    return result


def _from_articles(
    summary: ArticleImportSummary, spec: TaskSpec, parsed: ParsedScope, mode: str
) -> TaskResult:
    """把 ArticleImportSummary 映射为统一的 TaskResult。"""
    result = TaskResult(
        task_key=spec.key,
        task_label=spec.label,
        source_label=spec.source,
        scope=parsed.display,
        mode=mode,
        added=summary.added,
        pending=summary.pending,
        duplicates=summary.existing,
        failed=summary.failed,
        started_at=datetime.now(),
    )
    status_map = {
        "added": "added",
        "pending": "pending",
        "existing": "duplicates",
        "failed": "failed",
    }
    result.details = [
        {
            "status": status_map.get(outcome.status, outcome.status),
            "title": outcome.title,
            "reason": outcome.reason,
        }
        for outcome in summary.outcomes[:DETAILS_MAX]
    ]
    counts = (
        f"抓取 {summary.requested} 条，可新增 {summary.pending} 条，"
        f"已存在跳过 {summary.existing} 条，抓取失败 {summary.failed} 条"
    )
    if mode == MODE_PREVIEW:
        result.message = f"预览完成：{counts}，未写入数据库"
    else:
        result.message = (
            f"导入完成：新增 {summary.added} 条，已存在跳过 {summary.existing} 条，"
            f"抓取失败 {summary.failed} 条"
        )
    return result


def _execute(
    spec: TaskSpec, parsed: ParsedScope, *, apply: bool, session_factory
) -> TaskResult:
    """按任务类型分派到既有同步流程，并把结果统一映射为 TaskResult。"""
    mode = MODE_APPLY if apply else MODE_PREVIEW

    if spec.key == TASK_BANGUMI:
        summary = run_import(list(parsed.ids), session_factory=session_factory, apply=apply)
        return _from_bangumi(summary, spec, parsed, mode)

    if spec.key == TASK_YUC:
        summary = run_sync(parsed.season_code, session_factory=session_factory, apply=apply)
        return _from_yuc(summary, spec, parsed, mode)

    summary = run_import_articles(
        session_factory, apply=apply, category=parsed.category or None, limit=parsed.limit or None
    )
    return _from_articles(summary, spec, parsed, mode)


# ---------- 对外入口 ----------


def run_task(
    task_key: str,
    scope_text: str,
    *,
    apply: bool,
    scope_limit: str | None = None,
    trigger: str = TRIGGER_MANUAL,
    actor_id: int | None = None,
    session_factory=None,
) -> TaskResult:
    """执行一次数据同步任务（预览或正式），并落一条 sync_task_runs 记录。

    - scope_text / scope_limit：页面提交的原始范围文本（未解析前）；
    - 范围不合法 -> ScopeError；同数据源正在执行 -> TaskBusyError；
    - 同步过程中的异常会把运行记录标记为失败并向上抛出，由页面回显原因，
      不会留下「永远 running」的记录。
    """
    spec = get_task(task_key)
    if spec is None:
        raise ScopeError(f"未知的数据源：{task_key}")

    parsed = prepare_scope(spec, scope_text, scope_limit)
    session_factory = session_factory or SessionLocal
    mode = MODE_APPLY if apply else MODE_PREVIEW

    lock = _task_lock(spec.key)
    if not lock.acquire(blocking=False):
        raise TaskBusyError(f"「{spec.label}」已有同步任务在执行，请稍后再试")

    run_id: int | None = None
    try:
        db = session_factory()
        try:
            if crud.has_running_sync_task_run(
                db, spec.key, stale_after_seconds=RUN_STALE_SECONDS
            ):
                raise TaskBusyError(f"「{spec.label}」已有同步任务在执行，请稍后再试")
            run = crud.create_sync_task_run(
                db,
                task_key=spec.key,
                source_label=spec.source,
                scope=parsed.display,
                mode=mode,
                trigger=trigger,
                actor_id=actor_id,
            )
            run_id = run.id
        finally:
            db.close()

        try:
            result = _execute(spec, parsed, apply=apply, session_factory=session_factory)
        except Exception as exc:  # 失败也要留下记录，页面再回显原因
            _finalize(run_id, session_factory, status=STATUS_FAILED,
                      message=_short_reason(exc), details=[])
            raise

        result.run_id = run_id
        result.finished_at = datetime.now()
        _finalize(
            run_id,
            session_factory,
            status=result.status,
            counts={
                "added": result.added,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "duplicates": result.duplicates,
                "skipped": result.skipped,
                "pending": result.pending,
                "failed": result.failed,
            },
            message=result.message,
            details=result.details,
        )
        return result
    finally:
        lock.release()


def _short_reason(exc: Exception, limit: int = 300) -> str:
    """把异常折叠成一行说明（与页面回显风格一致）。"""
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _finalize(
    run_id: int,
    session_factory,
    *,
    status: str,
    counts: dict | None = None,
    message: str = "",
    details: list | None = None,
) -> None:
    """结束运行记录（独立会话，避免与业务写入共用事务）。"""
    if run_id is None:  # pragma: no cover - 记录创建失败时不会走到这里
        return
    db = session_factory()
    try:
        run = crud.get_sync_task_run(db, run_id)
        if run is None:  # pragma: no cover - 记录刚创建，正常不会缺失
            return
        crud.finish_sync_task_run(
            db, run, status=status, counts=counts, message=message, details=details
        )
    finally:
        db.close()


def preview(task_key: str, scope_text: str, *, scope_limit: str | None = None,
            actor_id: int | None = None, session_factory=None) -> TaskResult:
    """便捷入口：预览（不写业务数据）。"""
    return run_task(
        task_key,
        scope_text,
        apply=False,
        scope_limit=scope_limit,
        actor_id=actor_id,
        session_factory=session_factory,
    )


def apply_task(task_key: str, scope_text: str, *, scope_limit: str | None = None,
               actor_id: int | None = None, session_factory=None) -> TaskResult:
    """便捷入口：正式写入（定时任务将来也用这个入口，只是 trigger 不同）。"""
    return run_task(
        task_key,
        scope_text,
        apply=True,
        scope_limit=scope_limit,
        actor_id=actor_id,
        session_factory=session_factory,
    )


__all__ = [
    "TASK_ARTICLES",
    "TASK_BANGUMI",
    "TASK_YUC",
    "TASKS",
    "MODE_APPLY",
    "MODE_LABELS",
    "MODE_PREVIEW",
    "DETAIL_LABELS",
    "STATUS_BADGES",
    "STATUS_LABELS",
    "TRIGGER_MANUAL",
    "TRIGGER_SCHEDULED",
    "ParsedScope",
    "ScopeError",
    "TaskBusyError",
    "TaskResult",
    "TaskSpec",
    "apply_task",
    "article_categories",
    "get_task",
    "prepare_scope",
    "preview",
    "run_task",
    "task_labels",
]
