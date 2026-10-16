"""内容数据模型（SQLAlchemy 2.0 声明式风格）。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Content(Base):
    """ACGN 内容条目；字段与响应 JSON 一一对应。

    bangumi_id / source_url 为 Bangumi 导入脚本的溯源字段：
    - bangumi_id 唯一（对应库中已有索引 uq_contents_bangumi_id），重复导入据此跳过；
    - 旧示例数据两列均为 NULL，as_dict 不输出这两个字段，保持 API 契约不变。
    """

    __tablename__ = "contents"
    __table_args__ = (
        Index("uq_contents_bangumi_id", "bangumi_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    bangumi_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def as_dict(self) -> dict:
        """序列化为对外 JSON 结构，保持接口契约不变。"""
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "summary": self.summary,
            "rating": float(self.rating),
        }


class TestCase(Base):
    """接口自动化测试用例定义。

    本阶段只管理定义本身，不执行请求；请求路径经校验只允许本站本地路径。
    """

    __tablename__ = "test_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    expected_status: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    def as_dict(self) -> dict:
        """序列化 JSON 结构；创建时间输出为秒级 ISO 字符串。"""
        return {
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "expected_status": self.expected_status,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(sep=" ", timespec="seconds"),
        }


class ExecutionRecord(Base):
    """测试用例执行记录。

    case_id 为弱关联（不设外键级联）：用例删除后记录仍保留，
    依靠名称/方法/路径快照即可完整解读历史，不丢失执行历史。
    batch_id 指示所属批次；None 表示单条执行（不计入批次统计）。
    """

    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    case_name: Mapped[str] = mapped_column(String(100), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    expected_status: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    def as_dict(self) -> dict:
        """序列化 JSON 结构；耗时保留 2 位小数，时间为秒级 ISO 字符串。"""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "batch_id": self.batch_id,
            "case_name": self.case_name,
            "method": self.method,
            "path": self.path,
            "expected_status": self.expected_status,
            "actual_status": self.actual_status,
            "duration_ms": round(self.duration_ms, 2),
            "passed": self.passed,
            "error": self.error,
            "created_at": self.created_at.isoformat(sep=" ", timespec="seconds"),
        }


class ExecutionBatch(Base):
    """批次执行：对应一次"执行全部启用用例"。

    统计为批次完成时的快照；执行记录通过 batch_id 关联到批次，
    单条执行的记录 batch_id 为 NULL，永不计入批次统计。
    """

    __tablename__ = "test_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def as_dict(self) -> dict:
        """序列化 JSON 结构；通过率为百分比，无用例时为 None。"""
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(sep=" ", timespec="seconds"),
            "finished_at": self.finished_at.isoformat(sep=" ", timespec="seconds") if self.finished_at else None,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.passed / self.total * 100, 1) if self.total else None,
        }
class Article(Base):
    """ACGN news article."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, default="综合资讯"
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    original_url: Mapped[str | None] = mapped_column(
    String(1000), nullable=True
)

    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.now,
        onupdate=datetime.now,
    )


class AnimeSchedule(Base):
    """新番档期条目（数据来源：Yuc.wiki 档期页，独立于 Content / Article）。

    - season_code：档期标识，如 202610（2026 年 10 月新番）；
    - source_key：源站内唯一键。Yuc.wiki 档期页没有稳定的作品 ID，
      采用“规范化主标题”（去空白、小写）作为键，与 season_code 联合唯一，
      同一档期重复同步时据此去重与更新；
    - weekday：播出星期，1=周一 … 7=周日；无法可靠提取时为 NULL；
    - platforms：确认的播放平台链接列表 [{name, url}]，JSON 存储；
    - 同步只新增/更新本表记录，不触碰 Content、Article 等其他模块数据。
    """

    __tablename__ = "anime_schedules"
    __table_args__ = (
        UniqueConstraint(
            "season_code", "source_key", name="uq_anime_schedules_season_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_code: Mapped[str] = mapped_column(String(6), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    title_main: Mapped[str] = mapped_column(String(200), nullable=False)
    title_cn: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title_jp: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broadcast_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    broadcast_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    platforms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )

    def as_dict(self) -> dict:
        """序列化为对外 JSON 结构；缺失的可选字段返回 None，保持键稳定。"""
        return {
            "id": self.id,
            "season_code": self.season_code,
            "source_key": self.source_key,
            "title_main": self.title_main,
            "title_cn": self.title_cn,
            "title_jp": self.title_jp,
            "weekday": self.weekday,
            "broadcast_time": self.broadcast_time,
            "broadcast_note": self.broadcast_note,
            "image_url": self.image_url,
            "platforms": list(self.platforms or []),
            "source_url": self.source_url,
            "last_synced_at": self.last_synced_at.isoformat(sep=" ", timespec="seconds"),
        }


class SyncTaskRun(Base):
    """统一「数据同步」任务运行记录（测试管理平台 → 数据管理 → 数据同步）。

    一次手动同步（预览或正式）写一条记录，用于结果展示与历史审计：

    - task_key：数据源标识（bangumi / yuc_schedule / articles）；
    - mode：preview（只预览，不写业务数据）/ apply（正式写入业务数据）；
    - trigger：触发来源，当前固定 manual，将来接入定时任务后写 scheduled；
    - details：逐条结果快照 [{status, title, reason}]，便于回看本次究竟做了什么；
    - actor_id：发起人（管理员）主键，弱关联不设外键，账号删除后记录仍可解读。

    本表只记录同步过程，不参与业务数据读写，与 ScheduleSyncRun（档期专用同步记录）
    互不影响；新增本表不改动任何既有表结构与数据。
    """

    __tablename__ = "sync_task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    scope: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def as_dict(self) -> dict:
        """序列化为 JSON 结构；时间为秒级 ISO 字符串。"""
        return {
            "id": self.id,
            "task_key": self.task_key,
            "source_label": self.source_label,
            "scope": self.scope,
            "mode": self.mode,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat(sep=" ", timespec="seconds"),
            "finished_at": self.finished_at.isoformat(sep=" ", timespec="seconds")
            if self.finished_at
            else None,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "pending": self.pending,
            "failed": self.failed,
            "message": self.message,
            "details": list(self.details or []),
            "actor_id": self.actor_id,
        }


class User(Base):
    """平台账号：普通用户（user）与管理员（admin）。

    - password_hash 保存 PBKDF2-HMAC-SHA256 哈希，明文密码不落库；
    - role 只有 user / admin 两种取值，访问控制据此判断；
    - 新增本表不影响既有内容/测试数据，历史数据无需迁移。
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_username", "username", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ScheduleSyncRun(Base):
    """新番档期同步运行记录（独立于作品数据，dry-run 与 apply 均记录）。

    mode 区分运行模式：dry-run 只预览统计，不写入任何新番数据；
    apply 才会将解析结果写入 anime_schedules 表。
    """

    __tablename__ = "schedule_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    season_code: Mapped[str | None] = mapped_column(String(6), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def as_dict(self) -> dict:
        """序列化为对外 JSON 结构；时间为秒级 ISO 字符串。"""
        return {
            "id": self.id,
            "started_at": self.started_at.isoformat(sep=" ", timespec="seconds"),
            "finished_at": self.finished_at.isoformat(sep=" ", timespec="seconds")
            if self.finished_at
            else None,
            "season_code": self.season_code,
            "mode": self.mode,
            "pages_fetched": self.pages_fetched,
            "parsed": self.parsed,
            "valid": self.valid,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "failed": self.failed,
            "message": self.message,
        }