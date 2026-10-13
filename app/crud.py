"""数据库访问层：内容表查询、搜索与初始化播种。

本阶段 API 只读；建/改/删由测试用独立临时 SQLite 覆盖，
MySQL 接入与 Alembic 迁移留待下一阶段。
"""

from datetime import datetime

from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.fake_data import FAKE_CONTENTS
from app.models import (
    AnimeSchedule,
    Article,
    Content,
    ExecutionBatch,
    ExecutionRecord,
    ScheduleSyncRun,
    TestCase,
)
from app.schemas import TestCaseCreate


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

def get_content(db: Session, content_id: int) -> Content | None:
    """按主键查询，不存在返回 None（由路由层转 404）。"""
    return db.get(Content, content_id)
def list_articles(
    db: Session,
    category: str | None = None,
    published_only: bool = True,
) -> list[Article]:
    """查询资讯列表，可按分类筛选，默认只显示已发布文章。"""
    stmt = select(Article)

    if published_only:
        stmt = stmt.where(Article.is_published.is_(True))

    if category:
        stmt = stmt.where(Article.category == category)

    stmt = stmt.order_by(
        Article.published_at.desc(),
        Article.id.desc(),
    )
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

def search_contents(db: Session, keyword: str) -> list[Content]:
    """标题/简介不区分大小写的包含匹配，语义与第一阶段一致。

    数据量小，先在 Python 侧过滤以稳定契约；引入分页时再下沉到 SQL。
    """
    lowered = keyword.lower()
    return [
        item
        for item in list_contents(db)
        if lowered in item.title.lower() or lowered in item.summary.lower()
    ]


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

    本阶段用 create_all 最小启动；生产化时换 Alembic 版本管理。
    """
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        return seed_if_empty(db)
    finally:
        db.close()


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
