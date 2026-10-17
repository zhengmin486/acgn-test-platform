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

    只管理定义本身，不执行请求；请求路径经校验只允许本站本地路径。
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
    - email / email_verified / email_verified_at 为邮箱验证字段：
      * email 为 NULL 表示「历史账号」（脚本创建、无邮箱），一律视为已验证，
        这样启用邮箱验证后不会把既有账号（含管理员）锁在门外；
      * 注册带邮箱的新账号 email_verified 默认 False，必须验证后才能登录。
    - password_changed_at 记录最近一次改密时间：会话 Cookie 只带签发时间（iat），
      签发早于该时间的会话一律失效，这样「改密码」能把已登录的旧设备踢下线。
      历史账号该列为 NULL，按「从未改密」处理，不受影响。
    - bio 为个人空间「简介」：自由文本，NULL / 空串表示未填写；
      历史库由 crud.ensure_user_profile_columns 幂等补列，不影响既有账号。
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_username", "username", unique=True),
        Index("uq_users_email", "email", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ContentFavorite(Base):
    """个人空间「作品收藏」：登录用户对内容平台作品的收藏关系。

    - (user_id, content_id) 联合唯一：重复收藏幂等，不会产生重复行；
    - content_id 为弱关联（不设外键级联）：作品被管理员删除后，收藏行
      仍保留（列表页内连接查不到即不展示），清理由用户显式操作触发；
    - 查询一律带 user_id，保证收藏列表只属于当前登录账号。
    """

    __tablename__ = "content_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "content_id", name="uq_content_favorites_user_content"),
        Index("ix_content_favorites_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ContentViewHistory(Base):
    """个人空间「浏览记录」：登录用户浏览内容平台作品留痕。

    - (user_id, content_id) 联合唯一：同一用户重复浏览同一作品只更新
      viewed_at，不堆叠多行，记录列表保持「最近浏览」语义；
    - 每人最多保留 VIEW_HISTORY_LIMIT（crud 常量）条，写入时裁剪最旧记录，
      避免无限增长；超限裁剪以 viewed_at 倒序为准；
    - content_id 同为弱关联，作品删除后记录行保留但不展示。
    """

    __tablename__ = "content_view_history"
    __table_args__ = (
        UniqueConstraint("user_id", "content_id", name="uq_content_view_history_user_content"),
        Index("ix_content_view_history_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    viewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ContentStatus(Base):
    """个人空间「作品标注」：登录用户对作品的显式标记（看过 / 未看 / 想看）。

    - (user_id, content_id) 联合唯一：同一用户对同一作品只保留一条标注，
      重复标注为更新，不堆叠行；
    - status 取值为 seen / unseen / want（见 crud.CONTENT_STATUS_LABELS）；
    - 与收藏互相独立：可以只标注不收藏，也可以只收藏不标注；
    - content_id 为弱关联，作品删除后标注行保留但不展示。
    """

    __tablename__ = "content_status"
    __table_args__ = (
        UniqueConstraint("user_id", "content_id", name="uq_content_status_user_content"),
        Index("ix_content_status_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )


class ContentSubmission(Base):
    """词条申请：登录用户对内容的「新增 / 修改」提案，需管理员审核。

    - kind：create（新增词条）/ update（修改已有词条，content_id 指向目标）；
    - 提交时把提案字段（标题 / 类型 / 简介 / 评分）整体快照存入本表，
      审核通过后才写到 contents，审核前不改动任何业务数据；
    - status：pending（待审核）/ approved（已通过）/ rejected（已驳回）；
    - reviewer_id / reviewed_at / review_note 记录审核人、时间与驳回说明；
    - applied_content_id 记录通过后实际写入 / 更新的作品主键，便于追溯；
    - 同一申请只处理一次（仅 pending 可审核），避免重复写入。
    """

    __tablename__ = "content_submissions"
    __table_args__ = (
        Index("ix_content_submissions_user", "user_id"),
        Index("ix_content_submissions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    content_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    reviewer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_content_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Tag(Base):
    """作品标签（来源：Bangumi 条目 tags，按标注数取前若干个入库）。

    - name 唯一：多作品共用同一标签只保留一行，筛选与计数按标签聚合；
    - 与 Content 为多对多（ContentTag）：作品被后台删除后关联行仍保留
      （弱关联风格），列表页内连接查不到即不展示；
    - 标签只随 Bangumi 导入写入，站内不提供手工增改入口。
    """

    __tablename__ = "tags"
    __table_args__ = (
        Index("uq_tags_name", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ContentTag(Base):
    """作品-标签关联表：(content_id, tag_id) 联合唯一，重复导入幂等。

    替换式写入（crud.replace_content_tags）：同一作品重新导入时先删旧关联
    再建新关联，不会堆叠行；标签数量截断规则与导入侧一致。
    """

    __tablename__ = "content_tags"
    __table_args__ = (
        UniqueConstraint("content_id", "tag_id", name="uq_content_tags_content_tag"),
        Index("ix_content_tags_tag", "tag_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tag_id: Mapped[int] = mapped_column(Integer, nullable=False)


class EmailVerificationToken(Base):
    """邮箱验证令牌：库中只保存令牌的 SHA-256 哈希，明文仅出现在邮件/页面链接里。

    - 签发时用户旧的未消费令牌一律作废，保证「同一时刻只有一个有效链接」；
    - expires_at 之前且 consumed_at 为空的令牌才可用，验证成功后两者落库；
    - 新增本表不影响 users 及其余业务表。
    """

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("uq_email_tokens_hash", "token_hash", unique=True),
        Index("ix_email_tokens_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class PasswordResetToken(Base):
    """找回密码令牌：与邮箱验证令牌同构（只存哈希、一次性、带过期时间），
    但独立成表——两类令牌的用途、有效期与审计语义不同，分开便于分别清理。

    - 签发时该用户旧的未消费令牌一律作废，保证同一时刻只有一个有效链接；
    - 使用成功后 consumed_at 落库，并同步写入 users.password_changed_at；
    - 新增本表不影响 users 及其余业务表。
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("uq_password_reset_tokens_hash", "token_hash", unique=True),
        Index("ix_password_reset_tokens_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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