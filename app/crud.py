"""数据库访问层：内容表查询、搜索与初始化播种。

本阶段 API 只读；建/改/删由测试用独立临时 SQLite 覆盖，
MySQL 接入与 Alembic 迁移留待下一阶段。
"""

from datetime import datetime, timedelta

from sqlalchemy import Engine, delete as sa_delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base
from app.fake_data import FAKE_CONTENTS
from app.models import (
    AnimeSchedule,
    Article,
    Content,
    ExecutionBatch,
    ExecutionRecord,
    ScheduleSyncRun,
    SyncTaskRun,
    TestCase,
    User,
)
from app.schemas import TestCaseCreate
from app.security import hash_password


def list_contents(
    db: Session,
    content_type: str | None = None,
) -> list[Content]:
    """查询内容列表，可按类型筛选，按 id 排序。"""
    stmt = select(Content)

    if content_type is not None:
        stmt = stmt.where(Content.type == content_type)

    stmt = stmt.order_by(Content.id)
    return list(db.scalars(stmt))


def count_contents_by_type(db: Session) -> dict[str, int]:
    """按类型统计内容条数（首页分类浏览卡片）；空表返回空 dict。"""
    rows = db.execute(
        select(Content.type, func.count()).group_by(Content.type)
    ).all()
    return {content_type: count for content_type, count in rows}


def list_top_contents(db: Session, limit: int = 6) -> list[Content]:
    """首页推荐作品：按评分从高到低取前 limit 条真实数据，同分按 id 稳定排序。"""
    stmt = (
        select(Content)
        .order_by(Content.rating.desc(), Content.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt))

def get_content(db: Session, content_id: int) -> Content | None:
    """按主键查询，不存在返回 None（由路由层转 404）。"""
    return db.get(Content, content_id)


def list_related_contents(
    db: Session,
    content: Content,
    limit: int = 6,
) -> list[Content]:
    """详情页相关内容推荐：同类型、排除自身，按评分从高到低。

    推荐依据只有库内真实数据（类型一致 + 评分排序）；无同类型条目时
    返回空列表，由页面负责展示空态。
    """
    stmt = (
        select(Content)
        .where(Content.type == content.type, Content.id != content.id)
        .order_by(Content.rating.desc(), Content.id.asc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def list_related_articles(
    db: Session,
    content: Content,
    limit: int = 5,
) -> list[Article]:
    """详情页相关资讯：标题/摘要/正文提到该作品标题的已发布资讯。

    关联依据是文章文本与作品标题的真实匹配（不区分大小写），不做任何
    推测性关联；无匹配或作品标题为空时返回空列表，由页面展示空态。
    """
    keyword = content.title.strip()
    if not keyword:
        return []
    pattern = f"%{keyword.lower()}%"
    stmt = (
        select(Article)
        .where(
            Article.is_published.is_(True),
            or_(
                func.lower(Article.title).like(pattern),
                func.lower(Article.summary).like(pattern),
                func.lower(Article.body).like(pattern),
            ),
        )
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def list_articles(
    db: Session,
    category: str | None = None,
    published_only: bool = True,
    limit: int | None = None,
) -> list[Article]:
    """查询资讯列表，可按分类筛选，默认只显示已发布文章；limit 限制返回条数。"""
    stmt = select(Article)

    if published_only:
        stmt = stmt.where(Article.is_published.is_(True))

    if category:
        stmt = stmt.where(Article.category == category)

    stmt = stmt.order_by(
        Article.published_at.desc(),
        Article.id.desc(),
    )

    if limit is not None:
        stmt = stmt.limit(limit)

    return list(db.scalars(stmt))


def get_article(
    db: Session,
    article_id: int,
    published_only: bool = True,
) -> Article | None:
    """按 ID 查询资讯；默认不向普通页面展示未发布文章。"""
    stmt = select(Article).where(Article.id == article_id)

    if published_only:
        stmt = stmt.where(Article.is_published.is_(True))

    return db.scalar(stmt)

def _content_search_stmt(keyword: str, content_type: str | None):
    """内容搜索的共享查询：标题/简介不区分大小写包含匹配，可叠加类型筛选。

    语义与第一阶段一致（lower 后包含匹配）；分页与不分页的调用方共用本构造，
    避免两套搜索实现漂移。
    """
    stmt = select(Content)
    if keyword:
        pattern = f"%{keyword.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Content.title).like(pattern),
                func.lower(Content.summary).like(pattern),
            )
        )
    if content_type is not None:
        stmt = stmt.where(Content.type == content_type)
    return stmt.order_by(Content.id)


def search_contents(db: Session, keyword: str) -> list[Content]:
    """标题/简介不区分大小写的包含匹配，语义与第一阶段一致。

    不分页、按 id 排序返回全部匹配；分页需求用 search_contents_page。
    """
    return list(db.scalars(_content_search_stmt(keyword, None)))


def search_contents_page(
    db: Session,
    *,
    keyword: str = "",
    content_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[Content]]:
    """分页搜索内容：关键词（标题/简介）与类型筛选可叠加，返回 (总数, 当前页)。

    关键词与类型均可缺省：只给类型即按类型浏览，只给关键词即全类型搜索。
    """
    stmt = _content_search_stmt(keyword, content_type)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = list(
        db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    )
    return total, items


def seed_if_empty(db: Session) -> int:
    """表为空时播种开发/测试数据，返回写入行数；表已有数据则不动（幂等）。"""
    if db.scalars(select(Content.id).limit(1)).first() is not None:
        return 0
    for row in FAKE_CONTENTS:
        db.add(Content(**row))
    db.commit()
    return len(FAKE_CONTENTS)


def init_db(engine: Engine) -> int:
    """建表（如无）并播种，返回播种行数。

    - create_all 只新增缺失的表，不改动、不清空已有表与数据；
    - 内容播种仅在 contents 为空时执行；
    - 账号播种仅在 users 为空时执行，因此不影响本地已有数据。
    生产化时换 Alembic 版本管理。
    """
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        seeded = seed_if_empty(db)
        ensure_default_users(db)
        return seeded
    finally:
        db.close()


# ---------- 账号与认证 ----------

# 角色取值：普通用户只能使用内容平台，管理员额外可使用测试管理平台。
ROLE_USER = "user"
ROLE_ADMIN = "admin"
VALID_ROLES = (ROLE_USER, ROLE_ADMIN)


def count_users(db: Session) -> int:
    """账号总数（用于判断是否需要创建初始账号）。"""
    return db.scalar(select(func.count()).select_from(User)) or 0


def get_user(db: Session, user_id: int) -> User | None:
    """按主键查询账号；不存在返回 None。"""
    return db.get(User, user_id)


def get_user_by_username(db: Session, username: str) -> User | None:
    """按用户名查询账号（登录时使用）；不存在返回 None。"""
    return db.scalar(select(User).where(User.username == username))


def create_user(db: Session, *, username: str, password: str, role: str = ROLE_USER) -> User:
    """创建账号：校验角色与用户名，密码只存哈希，明文不落库。"""
    name = username.strip()
    if not name:
        raise ValueError("用户名不能为空")
    if role not in VALID_ROLES:
        raise ValueError(f"角色不合法，可选值：{'、'.join(VALID_ROLES)}")
    if get_user_by_username(db, name) is not None:
        raise ValueError(f"用户名已存在：{name}")
    user = User(username=name, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def set_password(db: Session, user: User, password: str) -> User:
    """重置账号密码（密码只存哈希）。"""
    user.password_hash = hash_password(password)
    db.commit()
    db.refresh(user)
    return user


def ensure_default_users(db: Session) -> int:
    """users 表为空时创建初始管理员与普通用户，返回创建个数（幂等）。

    已有账号时直接跳过，不会覆盖或清空任何既有账号。
    """
    if count_users(db) > 0:
        return 0
    create_user(
        db,
        username=settings.default_admin_username,
        password=settings.default_admin_password,
        role=ROLE_ADMIN,
    )
    create_user(
        db,
        username=settings.default_user_username,
        password=settings.default_user_password,
        role=ROLE_USER,
    )
    return 2


# ---------- 测试用例管理 ----------


def list_cases(db: Session) -> list[TestCase]:
    """全部用例，按 id 排序。"""
    return list(db.scalars(select(TestCase).order_by(TestCase.id)))


def get_case(db: Session, case_id: int) -> TestCase | None:
    """按主键查询用例，不存在返回 None（由路由层转 404）。"""
    return db.get(TestCase, case_id)


def create_case(db: Session, data: TestCaseCreate) -> TestCase:
    """新建用例；字段已由 TestCaseCreate 完成校验。"""
    case = TestCase(
        name=data.name,
        method=data.method,
        path=data.path,
        expected_status=data.expected_status,
        enabled=data.enabled,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def update_case(db: Session, case: TestCase, data: TestCaseCreate) -> TestCase:
    """整体编辑用例（PUT 语义：提交完整字段）。"""
    case.name = data.name
    case.method = data.method
    case.path = data.path
    case.expected_status = data.expected_status
    case.enabled = data.enabled
    db.commit()
    db.refresh(case)
    return case


def delete_case(db: Session, case: TestCase) -> None:
    """删除用例。"""
    db.delete(case)
    db.commit()


# ---------- 执行记录 ----------


def list_runs(db: Session, case_id: int | None = None,
              batch_id: int | None = None) -> list[ExecutionRecord]:
    """执行记录，按 id 倒序（最新在前）；可按用例/批次过滤。

    用例被删除后记录仍在，按 case_id 过滤依旧可查（历史不丢失）。
    """
    stmt = select(ExecutionRecord).order_by(ExecutionRecord.id.desc())
    if case_id is not None:
        stmt = stmt.where(ExecutionRecord.case_id == case_id)
    if batch_id is not None:
        stmt = stmt.where(ExecutionRecord.batch_id == batch_id)
    return list(db.scalars(stmt))


def get_run(db: Session, run_id: int) -> ExecutionRecord | None:
    """按主键查询执行记录，不存在返回 None（由路由层转 404）。"""
    return db.get(ExecutionRecord, run_id)


def add_run(db: Session, record: ExecutionRecord) -> ExecutionRecord:
    """保存执行记录并返回含主键的实例。"""
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def list_runs_for_batch(db: Session, batch_id: int) -> list[ExecutionRecord]:
    """某批次内的全部执行记录，按记录 id 升序（执行顺序）。"""
    return list(db.scalars(
        select(ExecutionRecord)
        .where(ExecutionRecord.batch_id == batch_id)
        .order_by(ExecutionRecord.id)
    ))


def list_batches(db: Session) -> list[ExecutionBatch]:
    """全部批次，按 id 倒序（最新在前）。"""
    return list(db.scalars(select(ExecutionBatch).order_by(ExecutionBatch.id.desc())))


def get_batch(db: Session, batch_id: int) -> ExecutionBatch | None:
    """按主键查询批次，不存在返回 None（由路由层转 404）。"""
    return db.get(ExecutionBatch, batch_id)


def get_previous_valid_batch(db: Session, batch_id: int) -> ExecutionBatch | None:
    """该批次之前最近一个有用例数的批次作为对比基线；空批次不作基线。"""
    return db.scalars(
        select(ExecutionBatch)
        .where(ExecutionBatch.id < batch_id, ExecutionBatch.total > 0)
        .order_by(ExecutionBatch.id.desc())
    ).first()


# ---------- 新番档期（数据由 scripts/sync_yuc_schedule.py 写入） ----------

# 同步时参与“有变化才更新”比对的源站字段；source_key/season_code 是身份键不参与。
SCHEDULE_COMPARE_FIELDS = (
    "title_main",
    "title_cn",
    "title_jp",
    "weekday",
    "broadcast_time",
    "broadcast_note",
    "image_url",
    "platforms",
    "source_url",
)


def upsert_schedule(db: Session, data: dict) -> tuple[AnimeSchedule, str]:
    """按 (season_code, source_key) 幂等写入一条档期记录。

    - 不存在 -> 新增（added）；
    - 存在且源字段有变化 -> 更新相应字段（updated）；
    - 存在且无变化 -> 保持字段不动（unchanged）。
    三种情况都会刷新 last_synced_at（表示“最近一次被同步确认的时间”）。
    返回 (记录, 动作)；由同步脚本负责提交统计，本函数自带提交保证逐条落库。
    """
    record = db.scalar(
        select(AnimeSchedule).where(
            AnimeSchedule.season_code == data["season_code"],
            AnimeSchedule.source_key == data["source_key"],
        )
    )
    now = datetime.now()
    if record is None:
        record = AnimeSchedule(last_synced_at=now, **data)
        db.add(record)
        db.commit()
        return record, "added"

    changed = [
        field
        for field in SCHEDULE_COMPARE_FIELDS
        if getattr(record, field) != data.get(field)
    ]
    record.last_synced_at = now
    for field in changed:
        setattr(record, field, data.get(field))
    db.commit()
    return record, ("updated" if changed else "unchanged")


def _schedule_filter_stmt(
    season: str | None,
    keyword: str | None,
    weekday: int | None,
):
    """档期查询的共享过滤构造：档期、星期、关键词（标题/播出说明）。

    分页查询（list_schedules）与整季查询（list_all_schedules）共用，
    避免两套过滤实现漂移。
    """
    stmt = select(AnimeSchedule)
    if season:
        stmt = stmt.where(AnimeSchedule.season_code == season)
    if weekday is not None:
        stmt = stmt.where(AnimeSchedule.weekday == weekday)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                AnimeSchedule.title_main.like(pattern),
                AnimeSchedule.title_cn.like(pattern),
                AnimeSchedule.title_jp.like(pattern),
                AnimeSchedule.broadcast_note.like(pattern),
            )
        )
    return stmt


def list_all_schedules(
    db: Session,
    *,
    season: str | None = None,
    keyword: str | None = None,
    weekday: int | None = None,
) -> list[AnimeSchedule]:
    """整季档期作品（不分页）：页面默认视图按星期分组展示使用。

    排序与 list_schedules 一致：星期升序、无星期最后、同星期按主标题。
    """
    stmt = _schedule_filter_stmt(season, keyword, weekday)
    return list(
        db.scalars(
            stmt.order_by(
                AnimeSchedule.weekday.is_(None),
                AnimeSchedule.weekday.asc(),
                AnimeSchedule.title_main.asc(),
            )
        )
    )


def list_schedules(
    db: Session,
    *,
    season: str | None = None,
    keyword: str | None = None,
    weekday: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[AnimeSchedule]]:
    """分页查询档期作品：可按档期、星期、关键词（标题/播出说明）筛选。

    排序：星期几升序（便于按周一到周日浏览），未解析出星期的排在最后，
    同星期内按主标题排序。
    """
    stmt = _schedule_filter_stmt(season, keyword, weekday)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = list(
        db.scalars(
            stmt.order_by(
                AnimeSchedule.weekday.is_(None),  # SQLite 中 False 在前：有星期的排前面
                AnimeSchedule.weekday.asc(),
                AnimeSchedule.title_main.asc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return total, items


def get_schedule(db: Session, schedule_id: int) -> AnimeSchedule | None:
    """按主键查询档期作品；不存在返回 None（由路由层转 404）。"""
    return db.get(AnimeSchedule, schedule_id)


def list_schedule_seasons(db: Session) -> list[dict]:
    """已同步的档期列表：档期码、作品数、最近同步时间，按档期新到旧排序。"""
    rows = db.execute(
        select(
            AnimeSchedule.season_code,
            func.count().label("count"),
            func.max(AnimeSchedule.last_synced_at).label("last_synced_at"),
        )
        .group_by(AnimeSchedule.season_code)
        .order_by(AnimeSchedule.season_code.desc())
    ).all()
    return [
        {
            "season_code": row.season_code,
            "count": row.count,
            "last_synced_at": row.last_synced_at.isoformat(sep=" ", timespec="seconds")
            if row.last_synced_at
            else None,
        }
        for row in rows
    ]


def latest_schedule_season(db: Session) -> str | None:
    """最新已同步档期码（档期码本身按年月递增，字符串倒序即新到旧）。"""
    return db.scalar(
        select(AnimeSchedule.season_code)
        .order_by(AnimeSchedule.season_code.desc())
        .limit(1)
    )


def create_sync_run(db: Session, *, season_code: str | None, mode: str) -> ScheduleSyncRun:
    """开始一次同步运行记录（dry-run 与 apply 都会记录）。"""
    run = ScheduleSyncRun(season_code=season_code, mode=mode, started_at=datetime.now())
    db.add(run)
    db.commit()
    return run


def finish_sync_run(db: Session, run: ScheduleSyncRun, *, summary: dict) -> ScheduleSyncRun:
    """写入一次同步的统计结果并结束运行记录。

    summary 允许只包含部分统计键，未提供的保持原值。
    """
    for field in (
        "pages_fetched",
        "parsed",
        "valid",
        "added",
        "updated",
        "unchanged",
        "duplicates",
        "skipped",
        "failed",
    ):
        if field in summary:
            setattr(run, field, summary[field])
    run.message = summary.get("message")
    run.finished_at = datetime.now()
    db.commit()
    return run


def list_sync_runs(db: Session, limit: int = 50) -> tuple[int, list[ScheduleSyncRun]]:
    """同步运行记录：最新在前，返回 (总数, 截断后的记录)。"""
    total = db.scalar(select(func.count()).select_from(ScheduleSyncRun)) or 0
    runs = list(
        db.scalars(
            select(ScheduleSyncRun).order_by(ScheduleSyncRun.id.desc()).limit(limit)
        )
    )
    return total, runs


# ---------- 统一「数据同步」任务运行记录（数据管理 → 数据同步） ----------
#
# 与上面的 ScheduleSyncRun（档期同步专用）并存、互不影响：
# 这里记录的是管理端「数据同步」页发起的三类数据源同步（预览与正式都记录）。

# finish_sync_task_run 允许写入的计数列；未提供的列保持原值。
_SYNC_TASK_COUNT_FIELDS = (
    "added",
    "updated",
    "unchanged",
    "duplicates",
    "skipped",
    "pending",
    "failed",
)


def create_sync_task_run(
    db: Session,
    *,
    task_key: str,
    source_label: str,
    scope: str,
    mode: str,
    trigger: str = "manual",
    actor_id: int | None = None,
) -> SyncTaskRun:
    """开始一次同步任务运行记录（预览与正式都会记录），status 初始为 running。"""
    run = SyncTaskRun(
        task_key=task_key,
        source_label=source_label,
        scope=scope,
        mode=mode,
        trigger=trigger,
        status="running",
        started_at=datetime.now(),
        actor_id=actor_id,
    )
    db.add(run)
    db.commit()
    return run


def get_sync_task_run(db: Session, run_id: int) -> SyncTaskRun | None:
    """按主键查询运行记录；不存在返回 None（由路由层决定如何提示）。"""
    return db.get(SyncTaskRun, run_id)


def has_running_sync_task_run(
    db: Session, task_key: str, *, stale_after_seconds: int = 600
) -> bool:
    """该数据源是否已有未结束的同步在跑（用于拒绝重复发起）。

    超过 stale_after_seconds 仍未结束的记录视为僵死（进程异常退出遗留），
    不再拦截，避免一次异常把该数据源永久锁死。
    """
    cutoff = datetime.now() - timedelta(seconds=stale_after_seconds)
    stmt = (
        select(SyncTaskRun.id)
        .where(
            SyncTaskRun.task_key == task_key,
            SyncTaskRun.status == "running",
            SyncTaskRun.started_at >= cutoff,
        )
        .limit(1)
    )
    return db.scalar(stmt) is not None


def finish_sync_task_run(
    db: Session,
    run: SyncTaskRun,
    *,
    status: str,
    counts: dict | None = None,
    message: str | None = None,
    details: list | None = None,
) -> SyncTaskRun:
    """写入一次同步的最终状态与统计，并结束运行记录。

    counts 允许只包含部分计数键，未提供的保持原值；details 为逐条结果快照。
    """
    for field in _SYNC_TASK_COUNT_FIELDS:
        if counts and field in counts:
            setattr(run, field, int(counts[field] or 0))
    run.status = status
    run.message = message
    if details is not None:
        run.details = list(details)
    run.finished_at = datetime.now()
    db.commit()
    return run


def list_sync_task_runs(
    db: Session, *, limit: int = 20, task_key: str | None = None
) -> tuple[int, list[SyncTaskRun]]:
    """同步任务历史：最新在前，可按数据源筛选，返回 (总数, 截断后的记录)。"""
    conditions = []
    if task_key:
        conditions.append(SyncTaskRun.task_key == task_key)
    total = db.scalar(select(func.count()).select_from(SyncTaskRun).where(*conditions)) or 0
    rows = list(
        db.scalars(
            select(SyncTaskRun)
            .where(*conditions)
            .order_by(SyncTaskRun.id.desc())
            .limit(limit)
        )
    )
    return total, rows


# ---------- 管理端数据维护（作品 / 资讯 / 新番档期） ----------
#
# 面向「测试管理平台 → 数据管理」的查询与删除。
# 只做单表按主键删除，不做跨表级联清理，也不触碰其它模块的数据。
# 既有只读查询（list_contents / list_articles / list_schedules 等）保持不变。


def count_contents(db: Session) -> int:
    """作品（内容）总条数。"""
    return db.scalar(select(func.count()).select_from(Content)) or 0


def count_articles(db: Session) -> int:
    """资讯总条数（含未发布）。"""
    return db.scalar(select(func.count()).select_from(Article)) or 0


def count_schedules(db: Session) -> int:
    """新番档期条目总条数。"""
    return db.scalar(select(func.count()).select_from(AnimeSchedule)) or 0


def count_schedule_seasons(db: Session) -> int:
    """已同步的档期数量（不同 season_code 的个数）。"""
    return db.scalar(
        select(func.count(func.distinct(AnimeSchedule.season_code)))
    ) or 0


def _delete_by_ids(db: Session, model, ids) -> int:
    """按主键批量删除，返回实际删除行数；ids 为空时不做任何写操作。

    ids 先去重再删除，避免同一 id 重复提交导致行数统计偏差。
    """
    cleaned = sorted({int(item) for item in ids})
    if not cleaned:
        return 0
    result = db.execute(sa_delete(model).where(model.id.in_(cleaned)))
    db.commit()
    return result.rowcount or 0


def delete_contents(db: Session, ids) -> int:
    """删除作品条目（数据管理 → 作品数据）。返回删除行数。"""
    return _delete_by_ids(db, Content, ids)


def delete_articles(db: Session, ids) -> int:
    """删除资讯条目（数据管理 → 资讯数据）。返回删除行数。"""
    return _delete_by_ids(db, Article, ids)


def delete_schedules(db: Session, ids) -> int:
    """删除新番档期条目（数据管理 → 新番档期）。返回删除行数。"""
    return _delete_by_ids(db, AnimeSchedule, ids)


def search_articles_page(
    db: Session,
    *,
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
    published_only: bool = False,
) -> tuple[int, list[Article]]:
    """管理端资讯分页查询：标题/摘要/正文包含匹配（不区分大小写）。

    默认包含未发布文章（管理视角），排序与列表页一致：发布时间新到旧。
    """
    stmt = select(Article)
    if published_only:
        stmt = stmt.where(Article.is_published.is_(True))
    if keyword:
        pattern = f"%{keyword.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Article.title).like(pattern),
                func.lower(Article.summary).like(pattern),
                func.lower(Article.body).like(pattern),
            )
        )
    stmt = stmt.order_by(Article.published_at.desc(), Article.id.desc())

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = list(
        db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    )
    return total, items
