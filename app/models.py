"""内容数据模型（SQLAlchemy 2.0 声明式风格）。"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint
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

    remote_image_url 为「封面按需下载」的来源地址：批量导入只给高热作品
    立刻下封面，低热作品只记远端地址，等详情页真的被打开时再下载落盘
    并回填 image_url（见 app/media.py 的 fetch_cover_on_demand）。
    同样不进 as_dict。
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
    # Bangumi 原始封面地址（未下载到本地时使用）。
    # 批量导入只给高热作品下封面，低热作品把地址存这里：详情页第一次被访问时
    # 再按需下载落盘（见 app/media.py 的 fetch_cover_on_demand），
    # 避免为「可能永远没人看的长尾」预付 1000 张图的带宽，也避免留下永久空洞。
    remote_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
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
    # 最近一次补全**失败**的时间（见 app/bangumi_meta.mark_meta_failed）。
    # 只靠 meta_fetched_at 不够：失败时它保持 NULL，于是每次访问都会重新尝试，
    # Bangumi 不可达时每个详情页都要白等满超时（线上实测每页 12.5 秒）。
    # 失败后写本列，冷却期内不再发起请求；成功补全时清空。
    meta_failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 所属系列（系列文件夹 series.id）：同系列的多季 / 剧场版指向同一行，
    # NULL 表示未归系列。与 year 一样经 crud.ensure_content_series_column
    # 幂等补列，且不进 as_dict（API 契约保持不变）。
    series_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 日文原名与别名（搜索增强）：
    # - title_original：Bangumi 的 name（多为日文）。title 只存 name_cn or name
    #   二选一，中文名条目的日文原名此前不入库，搜原名永远搜不到；
    # - aliases：infobox「别名」键拆出的别名（英文名 / 台版译名等），JSON 数组。
    # 经 crud.ensure_content_title_alias_columns 幂等补列；历史行由
    # scripts/backfill_title_aliases.py 按官方归档回填。同样不进 as_dict
    # （API 契约保持不变）。
    title_original: Mapped[str | None] = mapped_column(String(200), nullable=True)
    aliases: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 原文简介（简介语言切换的「原文」一侧）：
    # summary 存中文简介，本列存 Bangumi 的原始语言简介（多为日文）。
    # 两者都只由 bangumi_meta.sync_content_meta 在**本列为空且与中文简介不同**时
    # 写入（不覆盖人工维护），历史行由 scripts/backfill_meta.py 回填。
    # 为空表示这部作品只有一种语言，详情页不显示语言切换入口。
    # 经 crud.ensure_content_summary_original_column 幂等补列；不进 as_dict
    # （API 契约保持不变）。
    summary_original: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 书籍细分标注：light_novel（文库轻小说）/ literature（日本普通文学）。
    # Bangumi 只有一个「小说」platform，两类都在里面；本站不为此拆类型，
    # 只在卡片与详情页打一枚「文学」角标让用户一眼分辨（详见 app/page_common）。
    # NULL = 非书籍条目或尚未判定。同样不进 as_dict。
    book_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)

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

    # 头图地址：资讯导入时下载源站 keyvisual 到本地（/article-images/...）；
    # 下载失败时保留源站外链（同域可直连），历史库留 NULL（由回填脚本补）。
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # 列表页「作品名／」前缀（aniplex 源站的 IP 归属信号）：导入时从标题
    # 拆分落库，供识别回填（scripts/link_articles.py）作 extra_names 强信号；
    # 历史行留 NULL（导入时无此前缀的资讯）。
    ip_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

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

    # ---------- 分类 / 优先级（2026-10 资讯重构，三维度分离） ----------
    # 主体类型（面向用户的分类）：动画 / 漫画 / 轻小说 / 游戏 / 综合动态。
    # 音乐 / 创作者 / 业界不另立主体，按关联作品归入四类之一（见 scripts/news_classify）。
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="综合动态")
    # 游戏子类（仅游戏有意义）：Galgame / 二游 / 其他游戏。
    subcategory: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # 事件类型（辅助分类，不替代主体）：新作发布 / 发售 / 歌曲发布 / 创作者动态 /
    # 健康状况 / 制作消息 / 行业事件 / 其他。
    event_type: Mapped[str] = mapped_column(String(20), nullable=False, default="其他")
    # 地区（内部权重参考，不面向用户）：日本 / 国产 / 欧美 / 其他。
    region: Mapped[str] = mapped_column(String(20), nullable=False, default="日本")
    # 来源类型（排序与折叠用）：媒体编译 / 原文 / 官方PR / 用户投稿。
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="媒体编译")
    # 优先级总分（落库，供首页 / 资讯频道排序）：主体 + 子类 + 地区 + 事件 + 来源 叠加。
    priority_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 成人向标记：展示层按全局「隐藏成人内容」开关遮蔽图片与露骨摘要，不删除、不过滤。
    is_adult: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class AnimeSchedule(Base):
    """新番档期条目（数据来源：Yuc.wiki 档期页，独立于 Content / Article）。

    - season_code：档期标识，如 202610（2026 年 10 月新番）；
    - source_key：源站内唯一键。Yuc.wiki 档期页没有稳定的作品 ID，
      采用“规范化主标题”（去空白、小写）作为键，与 season_code 联合唯一，
      同一档期重复同步时据此去重与更新；
    - weekday：播出星期，1=周一 … 7=周日；无法可靠提取时为 NULL；
    - broadcast_time：具体播出时间 HH:MM（可能 24:00+ 深夜时段），
      取自档期页星期区播出位，按海报/标题关联回填；
    - work_type：作品类型（原创动画 / 漫画改编动画 / 小说改编动画 等）；
    - work_tags：题材标签，源站原文（斜杠或换行分隔）；
    - platforms：确认的播放平台链接列表 [{name, url}]，JSON 存储；
    - category：分类标识，NULL/空表示季度档期（season_code 为 YYYYMM），
      特殊分类使用固定码：unreleased=未上线新番、ova_sp=剧场版ova与特别篇、
      cn_movie=大陆引进剧场版；源站 Atom 条目名（新番卫星观测站 / Movie-OVA-SP /
      内地上映动画电影）在入库时统一改写为本站点名；
    - bangumi_subject_id / content_id：档期与作品库的关联（见下方列注释）；
    - 同步只新增/更新本表记录，不触碰 Content、Article 等其他模块数据。
    """

    __tablename__ = "anime_schedules"
    __table_args__ = (
        UniqueConstraint(
            "season_code", "source_key", name="uq_anime_schedules_season_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    title_main: Mapped[str] = mapped_column(String(200), nullable=False)
    title_cn: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title_jp: Mapped[str | None] = mapped_column(String(200), nullable=True)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broadcast_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    broadcast_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    work_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    work_tags: Mapped[str | None] = mapped_column(String(200), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    platforms: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 档期 ↔ 作品库关联（2026-10 起）：
    # - bangumi_subject_id：把档期标题解析到的 Bangumi 条目 id（唯一键）。
    #   翻译差异完全不影响它——同一部作品在任何站点都是同一个 subject id；
    # - content_id：站内作品主键（contents.id）。档期卡片有它就直跳作品详情页，
    #   没有才退化成按标题搜索（译名不一致时搜索会空手而归）。
    # 两列都是弱关联（不建外键）：作品被删时档期行保留，只是失去跳转目标。
    # 经 crud.ensure_schedule_link_columns 幂等补列。
    bangumi_subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    content_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
            "category": self.category,
            "title_main": self.title_main,
            "title_cn": self.title_cn,
            "title_jp": self.title_jp,
            "weekday": self.weekday,
            "broadcast_time": self.broadcast_time,
            "broadcast_note": self.broadcast_note,
            "work_type": self.work_type,
            "work_tags": self.work_tags,
            "image_url": self.image_url,
            "platforms": list(self.platforms or []),
            "source_url": self.source_url,
            "last_synced_at": self.last_synced_at.isoformat(sep=" ", timespec="seconds"),
            # 关联到站内作品的 id；有值时档期卡片直跳作品详情页，
            # 没有则退化成按标题搜索。bangumi_subject_id 是内部匹配键，不对外暴露。
            "content_id": self.content_id,
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
    - gender / birthday / location 为「个人资料」三字段，**只在个人空间展示给本人**：
      站内没有公开用户主页，任何页面都不会把这三项展示给别人；gender 取值见
      crud.GENDER_LABELS（NULL = 保密未填），birthday 只存日期不含时间，
      星座与年龄由 crud.zodiac_sign / crud.age_from_birthday 现算，不落库。
    - phone / phone_bound_at 为「绑定手机」（账号与安全页）：
      * phone 只在**验证码校验通过之后**才落库，因此「有值」就等于「已验证」，
        不需要额外的 phone_verified 标志位；NULL 表示未绑定；
      * 归一化规则见 crud.normalize_phone（去空格/连字符，去掉 +86 前缀，
        只存 11 位大陆号码）；
      * 历史库由 crud.ensure_user_profile_columns 幂等补列。
    - llm_provider / llm_api_key / llm_base_url / llm_model 为「成分检测 LLM 文案」
      的用户级接口配置（OpenAI 兼容）：填了 Key 就走用户自己的额度生成文案，
      没填回落到站点级配置（PERSONA_LLM_* 环境变量）。Key 只服务端使用、
      页面脱敏回显；历史库由 crud.ensure_user_llm_columns 幂等补列。
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("uq_users_username", "username", unique=True),
        Index("uq_users_email", "email", unique=True),
        # 手机号唯一：绑定后即「第二身份凭据」，同一个号不能挂在两个账号上。
        # 唯一索引允许多行 NULL，未绑定账号互不冲突。
        Index("uq_users_phone", "phone", unique=True),
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
    # 头像：站内相对地址（/avatars/avatar_u<id>_<ts>.webp）；NULL 表示用渐变首字占位。
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # --- 个人资料（仅本人可见）：性别 / 生日 / 所在地 ---
    # gender 取值见 crud.GENDER_LABELS；NULL 表示保密或没填。
    gender: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # birthday 只存日期（不存时间）；NULL 表示未填写。星座与年龄现算，不落库。
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    # location 为所在地自由文本（如「浙江 杭州」），站内不做省市级联。
    location: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # --- 账号与安全：绑定手机（有值即已验证，见类文档） ---
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone_bound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 成分检测 LLM 文案的用户级接口配置（OpenAI 兼容协议）。
    llm_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    llm_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    llm_base_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ContentFavorite(Base):
    """个人空间「作品收藏」：登录用户与内容平台作品的关系行。

    - (user_id, content_id) 联合唯一：同一作品只保留一行，收藏 / 标注都幂等；
    - 一行 =「这个用户和这部作品有关系」，两个业务字段各自独立：
      ``favorited`` 表示在不在收藏名单里，``status`` 表示看过 / 未看 / 想看（可空）；
    - **标注自动收藏**：写标注时把 favorited 置 True，没有行就建行——标注过的
      作品天然是名单成员，不需要用户再点一次收藏；
    - **取消收藏不动标注**：favorited 置 False，status 原样保留。收藏了但没看
      是名单里的常态，看过 / 想看这类事实不该因为移出名单而消失；只有两个字段
      都空了才删行，不留空壳；
    - content_id 为弱关联（不设外键级联）：作品被管理员删除后关系行仍保留
      （列表页内连接查不到即不展示），清理由用户显式操作触发；
    - 查询一律带 user_id，保证列表只属于当前登录账号。
    """

    __tablename__ = "content_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "content_id", name="uq_content_favorites_user_content"),
        Index("ix_content_favorites_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    favorited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )
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


class ContentSubmission(Base):
    """作品申请：登录用户对内容的「新增 / 修改」提案，需管理员审核。

    - kind：create（新增作品）/ update（修改已有作品，content_id 指向目标）；
    - 提交时把提案字段（标题 / 类型 / 简介）整体快照存入本表，
      审核通过后才写到 contents，审核前不改动任何业务数据；
    - rating 不接收用户提交：只存「提交时目标作品的当前评分」快照，供审核页
      展示原值；新增申请存 0.0。评分由外部数据源（Bangumi 等）链路维护；
    - year / media / episodes / source_material 是客观信息，与 contents 同名列一一
      对应（经 crud.ensure_content_submission_extra_columns 幂等补列），
      让申请能覆盖详情页展示的全部结构化信息，而不是只有标题与简介；
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
    # 客观信息（与 contents 同名列对应）；全部可空：修改申请里留空表示「不改动原值」。
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media: Mapped[str | None] = mapped_column(String(20), nullable=True)
    episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_material: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    reviewer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_content_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BangumiCollection(Base):
    """个人空间「御宅成分检测」的 Bangumi 收藏快照（按用户名读取公开收藏）。

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


class BilibiliFollow(Base):
    """个人空间「御宅成分检测」的 B站追番快照（按数字 UID 读公开追番列表）。

    - (user_id, season_id) 联合唯一：同一账号同一番剧只保留一条，重新同步为覆盖写；
    - season_type 为 B站原始类型（1 番剧 / 4 国创，均按动画参与画像；影视不拉）；
    - follow_status 为追番状态（1 想看 / 2 在看 / 3 看过），分析时映射到
      统一口径（在看 / 看过都算看过，与 Bangumi 一致）；
    - rating 为 B站评分（部分条目没有，None）；follow_count 为追番人数（热度）；
    - tags 存 styles 题材标签（缺的是真没打标签）；series_title / season_title
      保留系列与分季信息，供跨源去重（Bangumi 优先）与系列归并；
    - 接口匿名可读（无需 cookie），53013 = 用户把追番列表设成隐私。
    """

    __tablename__ = "bilibili_follows"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "season_id", name="uq_bilibili_follows_user_season"
        ),
        Index("ix_bilibili_follows_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    season_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    season_type: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    season_type_name: Mapped[str | None] = mapped_column(String(20), nullable=True)
    follow_status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    follow_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    series_title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    season_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_finish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class PersonaReport(Base):
    """御宅成分检测报告（每个账号一份，重新生成为覆盖写）。

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
    # 自定义语气说明（tone=custom 时生效）与用户追加的大段提示词，
    # 重新生成时沿用，不清空。
    tone_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # B站追番表绑定的数字 UID（成分检测第二数据源；NULL 表示未绑定）。
    bilibili_mid: Mapped[str | None] = mapped_column(String(30), nullable=True)
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
    （contents.title）、译名（title_original / aliases）与作品信息词表
    （作者 / 公司 / 系列）在资讯文本里匹配，命中的作品按得分截断后入库，
    是「这篇资讯在讲哪部作品」的结构化答案。

    - 替换式写入（crud.replace_article_content_links）：重新识别先删旧关联；
    - 弱关联：作品被后台删除后关联行仍保留，页面内连接查不到即不展示；
    - 与作品详情页的「相关资讯」（crud.list_related_articles）互为反向：
      那一侧 = 本表识别关联 ∪ 标题 LIKE（显式关联优先占位），这一侧把
      识别结果落库，供资讯页展示与作品页反查使用。
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


class ArticleEntity(Base):
    """资讯-作品信息关联表：(article_id, name) 联合唯一，重复识别幂等。

    「作品信息」标签（与 article_tags 的题材词表是两套东西）：词表来自
    库内作品的结构化信息，不是 Bangumi 题材标签——

    - ``kind=entity``：作者 / 公司名（entities 表，Bangumi infobox 识别），
      ``entity_type`` 快照 person / company / association；
    - ``kind=series``：系列名（series 表，同系列的多季 / 剧场版）；
    - ``kind=alias``：作品译名（contents 的 title_original / aliases），
      ``content_id`` 指向该作品（实体 / 系列行留 NULL）。

    由自动识别写入（见 app/article_tags.match_work_info）：资讯文本命中
    后按得分截断入库；实体 / 系列名再经 content_entity_links /
    contents.series_id 反查作品，为 article_content_links 补分（标题
    精确命中优先，作品信息命中权重更低、只作补充关联）。

    与 ArticleTag / ArticleContentLink 同一套弱关联 + 替换式写入语义：
    资讯被后台删除后关联行仍保留（文章 id 不会再被复用命中）。
    """

    __tablename__ = "article_entities"
    __table_args__ = (
        UniqueConstraint("article_id", "name", name="uq_article_entities_article_name"),
        Index("ix_article_entities_name", "name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    content_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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


class ContentRelation(Base):
    """作品之间的真实关系：前作 / 续作 / 衍生作。

    与 ContentEntityLink（作品 ↔ 作者/公司）互补：那张表回答「谁做的」，
    这张表回答「和哪部作品是同一条线上的」。

    - **数据来源是 Bangumi 关联条目端点**（/v0/subjects/{id}/subjects），
      由 scripts/backfill_content_relations.py 批量回填；**不靠标题前缀推断**，
      前缀聚类（crud.series_title_key）只服务首页分组去重，不进本表；
    - 方向语义：``from_content_id`` 视角下 ``to_content_id`` 是它的
      ``relation_type``（「本作 → 第2期」= 续作）。反向查询时类型取镜像
      （续作 ↔ 前作，衍生作不变），见 crud.list_content_relations；
    - 只有**双方作品都在库内**才写（脚本侧按 bangumi_id 配对保证），
      页面查询无需再过滤悬空 id；
    - 与系列（contents.series_id）是两条独立来源：系列文件夹覆盖多季/
      剧场版，关系表覆盖前作/续作/衍生作，详情页系列区取两者并集；
    - 弱关联风格与其余关联表一致：作品被删后行可留，内连接查不到即不展示。
    """

    __tablename__ = "content_relations"
    __table_args__ = (
        UniqueConstraint(
            "from_content_id", "to_content_id", "relation_type",
            name="uq_content_relations_from_to_type",
        ),
        Index("ix_content_relations_from", "from_content_id"),
        Index("ix_content_relations_to", "to_content_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_content_id: Mapped[int] = mapped_column(Integer, nullable=False)
    relation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now
    )


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


class EmailChangeToken(Base):
    """更换 / 绑定邮箱的确认令牌：库中只存哈希，明文只出现在确认邮件里。

    与 EmailVerificationToken 分开的**唯一原因**是它还需要记住「换成哪个邮箱」：
    验证令牌只管「这个账号的邮箱已验证」，换绑令牌管「这个账号的邮箱要改成 X」。
    因此多一列 new_email（目标是哪个地址），其余同构：一次性、带过期时间、
    签发时作废旧令牌。

    - 确认时把 users.email 改写成 new_email，并顺带标记为已验证
      （能收到这封信本身就证明了地址可用）；
    - 未消费的令牌永远不改动 users，用户放弃确认即等于什么都没发生。
    """

    __tablename__ = "email_change_tokens"
    __table_args__ = (
        Index("uq_email_change_tokens_hash", "token_hash", unique=True),
        Index("ix_email_change_tokens_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 目标邮箱（已归一化）；确认前不动 users.email。
    new_email: Mapped[str] = mapped_column(String(200), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class PhoneVerificationCode(Base):
    """手机绑定 / 解绑的短信验证码：库中只存验证码哈希与目标号码。

    - purpose 只有 bind / unbind 两种：bind 走「校验新号码」，unbind 走
      「校验当前绑定号码」，两者的码互不通用；
    - 6 位数字码的空间只有一百万，必须同时限制有效期（默认 5 分钟）与
      尝试次数（crud.PHONE_CODE_MAX_ATTEMPTS），超限即作废、要求重新发送；
    - 签发时作废该账号同类未消费记录，保证「同一时刻只有一个有效码」。
    """

    __tablename__ = "phone_verification_codes"
    __table_args__ = (
        Index("ix_phone_codes_user", "user_id"),
        Index("ix_phone_codes_hash", "code_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 验证码针对的号码（bind = 新号码，unbind = 当前绑定号码）。
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    purpose: Mapped[str] = mapped_column(String(10), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    season_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
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