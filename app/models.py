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

    image_url 为封面地址：Bangumi 导入时把远端图下载到本地
    （cover_storage_dir，见 app/config.py），本列存站内相对地址
    （/covers/cover_{bangumi_id}.jpg）；页面不直接引用原图，而是经
    /media/thumb/ 与 /media/full/ 派生图端点取缩略图与压缩全图
    （见 app/media.py）。NULL 表示该条目没有本地封面，模板回退到
    渐变占位图。
    与 bangumi_id 一样，image_url 也不进 as_dict：API 契约保持不变，
    封面是页面展示关注点。
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
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 作品发行年（来自 Bangumi date）。单列存而不是塞进标签：
    # 年份对筛选/排序有用，但混进 content_tags 会污染标签云。
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 客观信息：媒介（TV / 剧场版 / OVA / Web）、原作载体、话数。
    # 只展示，不参与标签筛选（见 app/bangumi_meta.py）。
    media: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_material: Mapped[str | None] = mapped_column(String(20), nullable=True)
    episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 制作 / 声优 / 客观信息最近一次从 Bangumi 补全的时间（为空表示还没补）。
    meta_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 所属系列（系列文件夹 series.id）：同系列的多季 / 剧场版指向同一行，
    # NULL 表示未归系列。与 year 一样经 crud.ensure_content_series_column
    # 幂等补列，且不进 as_dict（API 契约保持不变）。
    series_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def as_dict(self) -> dict:
        """序列化为对外 JSON 结构，保持接口契约不变。"""
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type,
            "summary": self.summary,
            "rating": float(self.rating),
        }


class ContentCredit(Base):
    """制作人员（来自 Bangumi 条目 infobox + persons 接口）。

    - ``role`` 直接存中文职位（导演 / 脚本 / 音乐 / 人物设定 …），展示即用；
    - ``tier`` 区分 main（高级职位，全收）与 long（原画 / 作画监督等长尾，
      只收知名或有作品积累的人），长尾默认折叠展示；
    - ``person_id`` 是 Bangumi 人物 id，用于「按声优 / 制作人找作品」聚合页。
    """

    __tablename__ = "content_credits"
    __table_args__ = (
        UniqueConstraint(
            "content_id", "role", "person_id", name="uq_content_credits_content_role_person"
        ),
        Index("ix_content_credits_content", "content_id"),
        Index("ix_content_credits_person", "person_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tier: Mapped[str] = mapped_column(String(10), nullable=False, default="main")


class ContentCast(Base):
    """角色-声优（只保留原版配音，翻配不入库）。

    Bangumi 的 ``actors`` 数组第一位即原版配音（日配），后续是中配 / 英配等，
    因此取第一位即可，无需语言字段。
    """

    __tablename__ = "content_cast"
    __table_args__ = (
        UniqueConstraint(
            "content_id", "character", "person_id",
            name="uq_content_cast_content_character_person",
        ),
        Index("ix_content_cast_content", "content_id"),
        Index("ix_content_cast_person", "person_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    character: Mapped[str] = mapped_column(String(100), nullable=False)
    character_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ContentFact(Base):
    """客观信息键值（放送星期 / 播放结束 / 官方网站 / 别名 …）。

    值域不固定、不参与筛选，只做详情页展示；固定且要用的字段
    （媒介 / 原作载体 / 话数 / 年份）已单独成列。
    """

    __tablename__ = "content_facts"
    __table_args__ = (
        UniqueConstraint("content_id", "key", name="uq_content_facts_content_key"),
        Index("ix_content_facts_content", "content_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(String(50), nullable=False)
    value: Mapped[str] = mapped_column(String(500), nullable=False)


class ContentStudio(Base):
    """作品制作公司（来自 Bangumi 标签归一后的公司名）。

    - ``(content_id, name)`` 联合唯一：重复导入幂等，不会堆叠行；
    - 公司不再混进 ``content_tags``，标签云与标签筛选只看到题材；
    - 弱关联风格与 ContentTag 一致：作品被删后行可留，列表内连接查不到即不展示。
    """

    __tablename__ = "content_studios"
    __table_args__ = (
        UniqueConstraint("content_id", "name", name="uq_content_studios_content_name"),
        Index("ix_content_studios_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


# 用例来源取值：manual 人工录入，auto 自动生成（可被生成流程更新 / 下线）。
CASE_SOURCE_MANUAL = "manual"
CASE_SOURCE_AUTO = "auto"


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
    # 用例来源：manual（人工录入）/ auto（从路由表自动生成）。
    # auto 用例可被「从接口清单生成」更新或下线，manual 用例永不被自动改动。
    source: Mapped[str] = mapped_column(String(10), nullable=False, default=CASE_SOURCE_MANUAL)
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


class BangumiCollection(Base):
    """个人空间「阿宅画像」的 Bangumi 收藏快照（按用户名读取公开收藏）。

    - (user_id, subject_id) 联合唯一：同一账号同一作品只保留一条，重新同步为覆盖写；
    - subject_type 为 Bangumi 原始类型（1 书籍 / 2 动画 / 3 音乐 / 4 游戏 / 6 三次元），
      分析时映射到站内的动画 / 漫画 / 游戏 / 轻小说；
    - collection_type 为 Bangumi 收藏状态（1 想看 / 2 在看 / 3 看过 / 4 搁置 / 5 抛弃），
      是「避雷类型」的核心负向信号；
    - rate 为用户个人评分（0 表示未评分）；
    - subject_tags 存作品标签（[{name, count}]），user_tags 存用户自己打的标签；
    - updated_at 为收藏最后更新时间（Bangumi 接口不提供首次收藏时间，
      因此「入坑年份」只能用作品发行年 / 最早更新时间近似）。
    """

    __tablename__ = "bangumi_collections"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "subject_id", name="uq_bangumi_collections_user_subject"
        ),
        Index("ix_bangumi_collections_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_type: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    name_cn: Mapped[str | None] = mapped_column(String(300), nullable=True)
    date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collection_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    collection_type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subject_tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    user_tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class PersonaReport(Base):
    """阿宅画像报告（每个账号一份，重新生成为覆盖写）。

    - payload：结构化画像（维度分数、标签、统计、模板文案、LLM 提示词）；
    - narrative：可选的 LLM 文案（直接调用或用户从网页版贴回）；
    - narrative_source：template / manual / llm，便于区分文案来源；
    - tone：语气（sharp 善意锐评 / gentle 温和中性）。
    """

    __tablename__ = "persona_reports"
    __table_args__ = (
        Index("uq_persona_reports_user", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bangumi_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tone: Mapped[str] = mapped_column(String(20), nullable=False, default="sharp")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative_source: Mapped[str] = mapped_column(String(20), nullable=False, default="template")
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class UserSiteImport(Base):
    """「收藏同步到本站」的导入记录：谁在什么时候把哪部作品带进了作品库。

    - 只记录**新建**的作品；站内已有（按 bangumi_id 命中）只做关联，不算导入；
    - ``(user_id, content_id)`` 联合唯一：重复同步不会堆叠记录；
    - 用途：个人空间「我导入的作品」+ 撤销（没被别人使用才真删），
      让「安全源免审核」的入库仍然可追溯、可回滚。
    """

    __tablename__ = "user_site_imports"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "content_id", name="uq_user_site_imports_user_content"
        ),
        Index("ix_user_site_imports_user", "user_id"),
        Index("ix_user_site_imports_content", "content_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )


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


class ArticleTag(Base):
    """资讯-标签关联表：(article_id, tag_id) 联合唯一，重复识别幂等。

    标签由「自动识别」写入（见 app/article_tags.py）：拿库中已有作品标签
    （tags 表，Bangumi 来源）当受控词表，在资讯标题/摘要/正文里做子串匹配，
    命中的标签按得分截断后入库。与 ContentTag 同一风格：

    - 替换式写入（crud.replace_article_tags）：重新识别先删旧关联再建新关联；
    - 弱关联：资讯被后台删除后关联行仍保留（文章 id 不会再被复用命中）；
    - 标签只随识别写入，站内不提供手工增改入口。
    """

    __tablename__ = "article_tags"
    __table_args__ = (
        UniqueConstraint("article_id", "tag_id", name="uq_article_tags_article_tag"),
        Index("ix_article_tags_tag", "tag_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tag_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ArticleContentLink(Base):
    """资讯-作品关联表：(article_id, content_id) 联合唯一，重复识别幂等。

    作品由「自动识别」写入（见 app/article_tags.py）：拿库中作品标题
    （contents.title）在资讯文本里做子串匹配，命中的作品按出现频次截断后
    入库，是「这篇资讯在讲哪部作品」的结构化答案。

    - 替换式写入（crud.replace_article_content_links）：重新识别先删旧关联；
    - 弱关联：作品被后台删除后关联行仍保留，页面内连接查不到即不展示；
    - 与作品详情页的「相关资讯」（crud.list_related_articles，请求时实时
      文本匹配）互为反向：那一侧保证新导入作品立即可见，这一侧把识别结果
      落库，供资讯页展示与后续统计使用。
    """

    __tablename__ = "article_content_links"
    __table_args__ = (
        UniqueConstraint("article_id", "content_id", name="uq_article_content_links"),
        Index("ix_article_content_links_content", "content_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class Entity(Base):
    """作者 / 公司花名册（来自 Bangumi 条目的 infobox 信息卡）。

    - ``name`` 唯一（归一化后）：同一作者或公司只登记一行，多作品共用，
      反查「这个人的全部作品」与按公司筛选都依赖这一点；
    - ``entity_type`` 区分 个人 / 公司 / 协会（PersonType 同款三分）；
    - ``bangumi_person_id`` 为 Bangumi 人物/公司条目 id 的溯源位（infobox
      只给名字，暂为 NULL；将来接关联条目端点时补齐）；
    - 与 Content 为多对多（ContentEntityLink）：弱关联风格，作品被删后
      关联行仍保留，页面内连接查不到即不展示。
    """

    __tablename__ = "entities"
    __table_args__ = (
        Index("uq_entities_name", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, default="person")
    bangumi_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ContentEntityLink(Base):
    """作品-实体关联表：(content_id, entity_id, role) 联合唯一。

    由 infobox 识别写入（见 app/entity_links.py）：哪部作品 ↔ 哪个作者/
    公司 ↔ 什么角色（原作 / 动画制作 / 导演 …）。与 ContentTag 同一风格：

    - 替换式写入（crud.replace_content_entity_links）：重新导入先删旧
      关联再建新关联，不会堆叠行；源站信息卡更新后本地同步更新；
    - 同一个人可在一部作品里以多个角色出现，故联合唯一含 role；
    - 弱关联：作品或实体被删后关联行可留，页面内连接查不到即不展示。
    """

    __tablename__ = "content_entity_links"
    __table_args__ = (
        UniqueConstraint(
            "content_id", "entity_id", "role",
            name="uq_content_entity_links_content_entity_role",
        ),
        Index("ix_content_entity_links_entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)


class Series(Base):
    """系列文件夹：同系列的作品（多季 / 剧场版 / 外传）指向同一行。

    - ``name`` 唯一：由 infobox「系列」字段或关联条目端点回填产生，
      也可在管理端手工维护；
    - 作品侧只存一个 ``contents.series_id``：一部作品最多属于一个系列，
      查询「同系列作品」就是一次按 series_id 的等值查询；
    - 弱关联风格：系列行可保留，作品被删后计数自然下降。
    """

    __tablename__ = "series"
    __table_args__ = (
        Index("uq_series_name", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class InviteCode(Base):
    """注册邀请码：公开注册关闭（REGISTRATION_MODE=invite）后，注册必须提供有效邀请码。

    - code 唯一且**明文存储**：邀请码本身就是管理员要分发的东西，列表页需要
      展示与转交，因此不做哈希（与密码、一次性令牌的保密语义不同）；
    - 单次有效：used_by / used_at 落库即消费，重复使用被拒；
    - expires_at 可空：为空表示永不过期；revoked_at 非空表示管理员提前作废；
    - created_by 为弱关联（不设外键）：签发人账号被删除后记录仍可解读。

    借鉴 LunaTV 邀请码系统的数据模型（过期 / 使用痕迹 / 追溯），
    字符集去掉 0/O/1/I 等易混淆字符，避免口头转抄时出错。
    """

    __tablename__ = "invite_codes"
    __table_args__ = (
        Index("uq_invite_codes_code", "code", unique=True),
        Index("ix_invite_codes_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def as_dict(self) -> dict:
        """序列化为 JSON 结构；时间为秒级 ISO 字符串（管理接口备用）。"""
        return {
            "id": self.id,
            "code": self.code,
            "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(sep=" ", timespec="seconds"),
            "expires_at": self.expires_at.isoformat(sep=" ", timespec="seconds")
            if self.expires_at
            else None,
            "used_by": self.used_by,
            "used_at": self.used_at.isoformat(sep=" ", timespec="seconds")
            if self.used_at
            else None,
            "revoked_at": self.revoked_at.isoformat(sep=" ", timespec="seconds")
            if self.revoked_at
            else None,
        }


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