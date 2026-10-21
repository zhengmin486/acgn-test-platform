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
from app.config import settings
from app.database import SessionLocal
from scripts.import_articles import (
    DEFAULT_LIMIT as ARTICLE_DEFAULT_LIMIT,
    ArticleImportSummary,
    list_categories,
    run_import_articles,
)
from scripts.import_bangumi import ImportSummary, run_import
from scripts.news_daily import DailyNewsSummary, run_import_daily
from scripts.fill_schedule_works import fill_works_for_season
from scripts.sync_yuc_schedule import (
    ATOM_URL,
    SeasonNotFoundError,
    SyncSummary,
    build_opener,
    fetch_url,
    find_special_category,
    resolve_insecure,
    run_sync,
)

# ---------- 任务标识与状态 ----------

TASK_BANGUMI = "bangumi"
TASK_YUC = "yuc_schedule"
TASK_ARTICLES = "articles"
TASK_NEWS = "news"  # 多源每日资讯（cron 用；按自然日配额）

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
    TaskSpec(
        key=TASK_NEWS,
        label="每日资讯（多源）",
        source="多源：机核 / 游民星空 ACG / アニメ！アニメ！ 等",
        scope_kind="news",
        scope_label="配额",
        scope_placeholder="",
        scope_help=(
            f"按自然日配额抓取（当前上限 {settings.news_daily_quota} 条/天）；"
            "日文源需要配置 NEWS_LLM_* 才会启用"
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


def suggest_season_codes(
    local_seasons: list[dict] | None,
    *,
    today: datetime | None = None,
    back: int = 3,
    forward: int = 3,
) -> list[dict]:
    """档期输入建议项：本地已同步档期 + 今天前后若干月的档期码。

    档期输入框必须是自由输入的——只给「本地已同步档期」的下拉会让页面一旦有
    数据就只剩老选项，新档期反而无从同步。因此这里只补充建议列表：

    - 本地已同步的档期带本地条数，模板显示为「YYYYMM（本地 N 条）」；
    - 前后各若干个月（默认各 3 个月）也列进来，方便直接同步下个季度；
    - 按档期码倒序（最近的在前）返回，同一档期只出现一次；未同步过的档期
      ``count`` 为 None，模板据此不带本地条数后缀。

    ``today`` 可注入，便于测试固定结果。
    """
    local_counts: dict[str, int] = {}
    for row in local_seasons or []:
        code = (row or {}).get("season_code")
        if code:
            local_counts[str(code)] = row.get("count") or 0

    codes = set(local_counts)
    base = today or datetime.now()
    month_index = base.year * 12 + (base.month - 1)
    for step in range(-max(0, back), max(0, forward) + 1):
        year, month = divmod(month_index + step, 12)
        codes.add(f"{year:04d}{month + 1:02d}")

    suggestions = []
    for code in sorted(codes, reverse=True):
        count = local_counts.get(code)
        suggestions.append(
            {
                "season_code": code,
                "count": count,
                "label": f"{code}（本地 {count} 条）" if count is not None else code,
            }
        )
    return suggestions


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
        text = (scope_text or "").strip()
        if text in crud.SCHEDULE_CATEGORY_LABELS:
            # 特殊档期分类（未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版）：
            # 码即分类标识，展示名用本站统一名称。
            return ParsedScope(display=crud.SCHEDULE_CATEGORY_LABELS[text], season_code=text)
        code = parse_season_code(scope_text)
        return ParsedScope(display=code, season_code=code)

    if spec.scope_kind == "news":
        return ParsedScope(display=f"每日上限 {settings.news_daily_quota} 条")

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


def _from_daily(
    summary: DailyNewsSummary, spec: TaskSpec, parsed: ParsedScope, mode: str
) -> TaskResult:
    """把多源每日资讯的汇总映射为统一的 TaskResult。"""
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
    result.details = summary.details[:DETAILS_MAX]
    message = summary.message
    # getattr 兜底：服务器上的 scripts/news_daily.py 可能仍是旧版，没有
    # filtered / deduped 等后加的字段（直接取属性会 AttributeError，导致整条
    # 资讯任务失败、cron 退出码非 0）。等资讯图管线整批部署后可改回直接取属性。
    if getattr(summary, "skipped_sources", 0):
        message += f"；{summary.skipped_sources} 个日文源因未配置翻译 Key 跳过"
    if getattr(summary, "filtered", 0):
        message += f"；硬广过滤 {summary.filtered} 条"
    if getattr(summary, "deduped", 0):
        message += f"；同事件合并 {summary.deduped} 条"
    result.message = message
    return result


def _resolve_special_category(code: str) -> tuple[str, str]:
    """从 Atom 解析特殊档期分类的页面 URL 与展示名；取不到抛 ScopeError。

    特殊分类（未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版）不在季度档期发现
    路径里，需读 Atom 用 find_special_category 定位其页面地址与本站展示名。
    """
    try:
        text = fetch_url(
            ATOM_URL, opener=build_opener(None, resolve_insecure(None)), timeout=15.0
        )
    except Exception as exc:  # noqa: BLE001 - 取不到 Atom 直接回显原因，不抛栈
        raise ScopeError(f"无法读取 Atom 以定位特殊分类：{_short_reason(exc)}")
    try:
        info = find_special_category(text, code)
    except SeasonNotFoundError as exc:
        raise ScopeError(f"Atom 中未发现特殊档期分类：{code}") from exc
    return info.url, info.title


def _auto_fill_works(
    season_code: str, *, apply: bool, session_factory, trigger: str
) -> str:
    """档期同步后的自动补库：把档期里有、作品库里没有的作品补进作品库。

    补库要逐条联网解析（镜像搜索 + 取条目），一次约几十秒到一分钟。线上是
    单 worker 的 uvicorn，放在页面点击里会整站阻塞，因此**只在定时任务
    （trigger=scheduled）里接力**，手动点击不跑。
    补库失败不影响档期同步本身的结果，只把原因写进运行记录。
    """
    if not apply:
        return "预览模式未执行自动补库"
    if trigger != TRIGGER_SCHEDULED:
        return "手动同步不执行自动补库（仅定时任务接力，避免阻塞 Web 进程）"
    try:
        summary = fill_works_for_season(
            session_factory=session_factory,
            season_code=season_code,
            apply=True,
        )
    except Exception as exc:  # noqa: BLE001 - 补库失败不牵连档期同步结果
        return f"自动补库未执行：{_short_reason(exc)}"
    return summary.message


def _execute(
    spec: TaskSpec,
    parsed: ParsedScope,
    *,
    apply: bool,
    session_factory,
    trigger: str = TRIGGER_MANUAL,
) -> TaskResult:
    """按任务类型分派到既有同步流程，并把结果统一映射为 TaskResult。"""
    mode = MODE_APPLY if apply else MODE_PREVIEW

    if spec.key == TASK_BANGUMI:
        summary = run_import(list(parsed.ids), session_factory=session_factory, apply=apply)
        return _from_bangumi(summary, spec, parsed, mode)

    if spec.key == TASK_YUC:
        season_code = parsed.season_code
        if season_code in crud.SCHEDULE_CATEGORY_LABELS:
            # 特殊档期分类：从 Atom 解析页面 URL 与展示名后直接抓页面（不经档期发现），
            # 写入时带 category；特殊分类不做自动补库（不是 YYYYMM 档期，填库无意义）。
            url, label = _resolve_special_category(season_code)
            summary = run_sync(
                season_code,
                session_factory=session_factory,
                apply=apply,
                insecure=resolve_insecure(None),
                season_url=url,
                season_title=label,
                category=season_code,
            )
            result = _from_yuc(summary, spec, parsed, mode)
            return result
        summary = run_sync(
            season_code,
            session_factory=session_factory,
            apply=apply,
            insecure=resolve_insecure(None),
        )
        result = _from_yuc(summary, spec, parsed, mode)
        # 自动接力补库：档期里有、作品库里没有的作品直接入库。
        note = _auto_fill_works(
            season_code,
            apply=apply,
            session_factory=session_factory,
            trigger=trigger,
        )
        result.message = f"{result.message}；{note}" if result.message else note
        return result

    if spec.key == TASK_NEWS:
        summary = run_import_daily(session_factory, apply=apply)
        return _from_daily(summary, spec, parsed, mode)

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
            result = _execute(
                spec,
                parsed,
                apply=apply,
                session_factory=session_factory,
                trigger=trigger,
            )
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
