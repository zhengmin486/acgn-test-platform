"""数据库访问层：内容表查询、搜索与初始化播种。

当前 API 只读；建/改/删由测试用独立临时 SQLite 覆盖，
MySQL 接入与 Alembic 迁移尚未落地。
"""

import secrets
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete as sa_delete, func, inspect, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import Base
from app.fake_data import FAKE_CONTENTS
from app.models import (
    AnimeSchedule,
    Article,
    Content,
    ContentFavorite,
    ContentStatus,
    ContentSubmission,
    ContentTag,
    ContentViewHistory,
    EmailVerificationToken,
    ExecutionBatch,
    ExecutionRecord,
    PasswordResetToken,
    ScheduleSyncRun,
    SyncTaskRun,
    Tag,
    TestCase,
    User,
)
from app.schemas import TestCaseCreate
from app.security import hash_password, hash_token


def list_contents(
    db: Session,
    content_type: str | None = None,
    tag: str | None = None,
) -> list[Content]:
    """查询内容列表，可按类型与标签筛选，按 id 排序。

    ``tag`` 为标签名称（精确匹配、区分大小写）；传入时走 Tags/ContentTags
    多对多连接，与类型筛选可叠加。
    """
    stmt = select(Content)

    if content_type is not None:
        stmt = stmt.where(Content.type == content_type)
    if tag:
        stmt = (
            stmt.join(ContentTag, ContentTag.content_id == Content.id)
            .join(Tag, Tag.id == ContentTag.tag_id)
            .where(Tag.name == tag)
            .distinct()
        )

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


def get_contents_by_ids(db: Session, content_ids: list[int]) -> list[Content]:
    """按主键批量取作品，供词条审核页展示「修改目标」；空列表返回空。"""
    if not content_ids:
        return []
    return list(db.scalars(select(Content).where(Content.id.in_(content_ids))))


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


# ---------- 作品标签（随 Bangumi 导入写入，站内只读） ----------

# 单个作品最多保留的标签数：与 Bangumi 导入的截断规则一致，防止标签刷屏。
MAX_CONTENT_TAGS = 5

# 标签名长度上限（Tag.name 为 String(80)）：超长标签直接丢弃，不截断——
# 截断会产生语义错误的标签，丢弃只损失一个可选筛选维度。
TAG_NAME_MAX_LENGTH = 80


def get_or_create_tag(db: Session, name: str) -> Tag:
    """按名称取标签，不存在则创建；调用方负责把名称裁剪到长度上限内。"""
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name)
        db.add(tag)
        db.flush()  # 先拿到 id，便于同批次的关联行引用
    return tag


def replace_content_tags(db: Session, content_id: int, tag_names: list[str]) -> list[str]:
    """用给定标签列表整体替换某作品的标签，返回最终生效的标签名列表。

    - 名称去空白、去重（保序）、丢弃空名与超长名（TAG_NAME_MAX_LENGTH）；
    - 最多保留前 MAX_CONTENT_TAGS 个（导入侧已按标注数截断，这里兜底）；
    - 先删旧关联再建新关联：重复导入同一作品不会堆叠关联行，
      标签被源站移除后本地同步移除（标签是派生命名，不是本地资产）。
    """
    names: list[str] = []
    for raw in tag_names:
        name = (raw or "").strip()
        if not name or len(name) > TAG_NAME_MAX_LENGTH or name in names:
            continue
        names.append(name)
    names = names[:MAX_CONTENT_TAGS]

    db.execute(sa_delete(ContentTag).where(ContentTag.content_id == content_id))
    for name in names:
        tag = get_or_create_tag(db, name)
        db.add(ContentTag(content_id=content_id, tag_id=tag.id))
    db.commit()
    return names


def list_content_tags(db: Session, content_id: int) -> list[str]:
    """某作品的标签名列表（按 tag_id 稳定排序，即入库顺序）。"""
    return list(
        db.scalars(
            select(Tag.name)
            .join(ContentTag, ContentTag.tag_id == Tag.id)
            .where(ContentTag.content_id == content_id)
            .order_by(Tag.id)
        )
    )


def list_tags_with_counts(db: Session, limit: int | None = 30) -> list[tuple[str, int]]:
    """全部标签及关联作品数，按作品数从多到少（筛选栏用）；空库返回空列表。

    count 统计的是 content_tags 关联行数；作品被删除后关联行仍在，
    计数可能略高于可见作品数——标签筛选走内连接，结果不受影响。
    """
    stmt = (
        select(Tag.name, func.count(ContentTag.id))
        .join(ContentTag, ContentTag.tag_id == Tag.id)
        .group_by(Tag.id, Tag.name)
        .order_by(func.count(ContentTag.id).desc(), Tag.name)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(name, count) for name, count in db.execute(stmt).all()]


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

def _content_search_stmt(keyword: str, content_type: str | None, tag: str | None = None):
    """内容搜索的共享查询：标题/简介不区分大小写包含匹配，可叠加类型与标签筛选。

    匹配语义保持原样（lower 后包含匹配）；分页与不分页的调用方共用本构造，
    避免两套搜索实现漂移。标签筛选走 content_tags/tags 连接下推到 SQL，
    不在应用层过滤；同一作品命中标签时只出现一次（联合唯一约束保证）。
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
    if tag:
        stmt = (
            stmt.join(ContentTag, ContentTag.content_id == Content.id)
            .join(Tag, Tag.id == ContentTag.tag_id)
            .where(Tag.name == tag)
        )
    return stmt.order_by(Content.id)


def search_contents(db: Session, keyword: str, tag: str | None = None) -> list[Content]:
    """标题/简介不区分大小写的包含匹配，与分页版本语义一致。

    不分页、按 id 排序返回全部匹配；分页需求用 search_contents_page。
    """
    return list(db.scalars(_content_search_stmt(keyword, None, tag)))


def search_contents_page(
    db: Session,
    *,
    keyword: str = "",
    content_type: str | None = None,
    tag: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[Content]]:
    """分页搜索内容：关键词（标题/简介）、类型与标签筛选可叠加，返回 (总数, 当前页)。

    三类条件均可缺省：只给类型即按类型浏览，只给关键词即全类型搜索，
    只给标签即按标签归类浏览。
    """
    stmt = _content_search_stmt(keyword, content_type, tag)
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


# 启动时按需补齐的 users 列：``(列名, DDL 片段)``，SQLite 与 MySQL 通用。
# 包含邮箱验证与找回密码两组字段——项目没有 Alembic，历史库只能这样平滑加列。
_USER_EMAIL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("email", "VARCHAR(200)"),
    ("email_verified", "BOOLEAN NOT NULL DEFAULT 0"),
    ("email_verified_at", "DATETIME"),
    ("password_changed_at", "DATETIME"),
)


def ensure_user_email_columns(engine: Engine) -> list[str]:
    """为已存在的 users 表补齐邮箱验证列与邮箱唯一索引（幂等，返回新增列名）。

    ``create_all`` 不会给已存在的表追加列，历史库（SQLite / MySQL）在启用邮箱验证后
    会因「缺少 email_verified 列」直接报错，这里在启动时按需 ALTER TABLE。
    补列后把既有行统一回填为「已验证」——它们是加该功能之前的账号（脚本创建的管理员等），
    不应该因为启用邮箱验证而被挡在登录之外。
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("users")}
    added: list[str] = []
    for name, ddl in _USER_EMAIL_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
        added.append(name)

    # 只有 email_verified 这一列本身是新增时才回填：后续再加别的新列（例如
    # password_changed_at）不能顺带把「已注册但尚未验证」的账号改成已验证。
    if "email_verified" in added:
        with engine.begin() as connection:
            connection.execute(text("UPDATE users SET email_verified = 1"))

    inspector = inspect(engine)
    if "uq_users_email" not in {index["name"] for index in inspector.get_indexes("users")}:
        with engine.begin() as connection:
            connection.execute(text("CREATE UNIQUE INDEX uq_users_email ON users (email)"))
    return added


# 启动时按需补齐的 users「个人空间」列（简介）。历史库同样只能幂等 ALTER，
# 不回填任何值——简介默认为空，由用户自行填写。
_USER_PROFILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bio", "TEXT"),
)


def ensure_user_profile_columns(engine: Engine) -> list[str]:
    """为已存在的 users 表补齐个人空间列（简介 bio），幂等，返回新增列名。

    与 ensure_user_email_columns 同一模式：create_all 只建新表、不给已有表加列，
    个人空间功能上线前创建的库需要在启动时补列，否则查询 bio 直接报错。
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("users")}
    added: list[str] = []
    for name, ddl in _USER_PROFILE_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


def init_db(engine: Engine) -> int:
    """建表（如无）并播种，返回播种行数。

    - create_all 只新增缺失的表，不改动、不清空已有表与数据；
    - 内容播种仅在 contents 为空时执行；
    - 账号播种仅在 users 为空时执行，因此不影响本地已有数据。
    生产化时换 Alembic 版本管理。
    """
    Base.metadata.create_all(bind=engine)
    # 历史库补列：create_all 只建新表、不给已有表加列，老库需按需 ALTER TABLE。
    ensure_user_email_columns(engine)
    ensure_user_profile_columns(engine)
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


def get_user_by_email(db: Session, email: str) -> User | None:
    """按邮箱查询账号（邮箱唯一时使用）；空邮箱返回 None。"""
    address = normalize_email(email)
    if not address:
        return None
    return db.scalar(select(User).where(User.email == address))


def normalize_email(email: str | None) -> str | None:
    """清理邮箱：去首尾空格并转小写；空串统一视为 None。"""
    address = (email or "").strip().lower()
    return address or None


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    role: str = ROLE_USER,
    email: str | None = None,
    require_verification: bool | None = None,
) -> User:
    """创建账号：校验角色与用户名，密码只存哈希，明文不落库。

    - ``email`` 为空表示该账号不参与邮箱验证（脚本创建的管理员、历史账号），
      落地时 ``email_verified=True``，启用验证功能后也不会被挡在登录之外；
    - ``require_verification`` 为 None 时自动判断：带邮箱且功能开启 -> 需要验证，
      否则不需要；调用方也可显式指定（测试与管理入口用）。
    """
    name = username.strip()
    if not name:
        raise ValueError("用户名不能为空")
    if role not in VALID_ROLES:
        raise ValueError(f"角色不合法，可选值：{'、'.join(VALID_ROLES)}")
    if get_user_by_username(db, name) is not None:
        raise ValueError(f"用户名已存在：{name}")
    address = normalize_email(email)
    if address is not None and get_user_by_email(db, address) is not None:
        raise ValueError(f"邮箱已被注册：{address}")
    if require_verification is None:
        require_verification = bool(address) and settings.email_verification_enabled
    user = User(
        username=name,
        password_hash=hash_password(password),
        role=role,
        email=address,
        email_verified=not require_verification,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---------- 个人空间：简介 / 作品收藏 / 浏览记录 ----------

# 简介长度上限：与用户名同级的常规约束，超限在路由层回显错误，不落库。
MAX_BIO_LENGTH = 500

# 每个用户的浏览记录上限：写入时按 viewed_at 倒序裁剪，保留最近 N 条，
# 防止单账号记录无限增长；收藏不设上限（收藏是用户主动行为，量级可控）。
VIEW_HISTORY_LIMIT = 100

# 作品标注：登录用户对作品的显式标记，与收藏互相独立。
CONTENT_STATUS_LABELS = {
    "seen": "看过",
    "unseen": "未看",
    "want": "想看",
}

# 词条申请：登录用户提交新增 / 修改提案，管理员审核通过后才写入 contents。
CONTENT_TYPES = ("anime", "comic", "game", "novel")

SUBMISSION_KIND_CREATE = "create"
SUBMISSION_KIND_UPDATE = "update"
SUBMISSION_KINDS = (SUBMISSION_KIND_CREATE, SUBMISSION_KIND_UPDATE)
SUBMISSION_KIND_LABELS = {
    SUBMISSION_KIND_CREATE: "新增词条",
    SUBMISSION_KIND_UPDATE: "修改词条",
}

SUBMISSION_STATUS_PENDING = "pending"
SUBMISSION_STATUS_APPROVED = "approved"
SUBMISSION_STATUS_REJECTED = "rejected"
SUBMISSION_STATUSES = (
    SUBMISSION_STATUS_PENDING,
    SUBMISSION_STATUS_APPROVED,
    SUBMISSION_STATUS_REJECTED,
)
SUBMISSION_STATUS_LABELS = {
    SUBMISSION_STATUS_PENDING: "待审核",
    SUBMISSION_STATUS_APPROVED: "已通过",
    SUBMISSION_STATUS_REJECTED: "已驳回",
}

SUBMISSION_MAX_TITLE = 200
SUBMISSION_MAX_SUMMARY = 2000


def set_user_bio(db: Session, user: User, bio: str | None) -> User:
    """更新个人简介：去首尾空白，空串按未填写（None）处理；超长抛 ValueError。"""
    text = (bio or "").strip()
    if len(text) > MAX_BIO_LENGTH:
        raise ValueError(f"简介长度不能超过 {MAX_BIO_LENGTH} 个字符")
    user.bio = text or None
    db.commit()
    db.refresh(user)
    return user


def count_favorites(
    db: Session, user_id: int, content_type: str | None = None
) -> int:
    """收藏作品数（个人空间统计卡）；指定 content_type 时只数该分类。"""
    if content_type is None:
        return (
            db.scalar(
                select(func.count())
                .select_from(ContentFavorite)
                .where(ContentFavorite.user_id == user_id)
            )
            or 0
        )
    return (
        db.scalar(
            select(func.count())
            .select_from(ContentFavorite)
            .join(Content, Content.id == ContentFavorite.content_id)
            .where(
                ContentFavorite.user_id == user_id,
                Content.type == content_type,
            )
        )
        or 0
    )


def is_favorite(db: Session, user_id: int, content_id: int) -> bool:
    """是否已收藏该作品（详情页按钮状态）。"""
    return (
        db.scalar(
            select(ContentFavorite.id).where(
                ContentFavorite.user_id == user_id,
                ContentFavorite.content_id == content_id,
            )
        )
        is not None
    )


def add_favorite(db: Session, user_id: int, content_id: int) -> bool:
    """收藏作品；已收藏时返回 False（幂等，不产生重复行）。"""
    if is_favorite(db, user_id, content_id):
        return False
    db.add(ContentFavorite(user_id=user_id, content_id=content_id))
    db.commit()
    return True


def remove_favorite(db: Session, user_id: int, content_id: int) -> bool:
    """取消收藏；返回是否真的删除了一行（幂等）。"""
    result = db.execute(
        sa_delete(ContentFavorite).where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.content_id == content_id,
        )
    )
    db.commit()
    return bool(result.rowcount)


def clear_favorites(
    db: Session, user_id: int, content_type: str | None = None
) -> int:
    """清空收藏，返回删除行数；指定 content_type 时只清空该分类。"""
    stmt = sa_delete(ContentFavorite).where(ContentFavorite.user_id == user_id)
    if content_type is not None:
        stmt = stmt.where(
            ContentFavorite.content_id.in_(
                select(Content.id).where(Content.type == content_type)
            )
        )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount or 0


def list_favorites(
    db: Session,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    content_type: str | None = None,
) -> tuple[int, list[Content]]:
    """收藏作品分页（按收藏时间倒序）：内连接 contents，作品已删则不展示。

    content_type 不为空时按内容类型筛选，对应个人空间「追番 / 追漫 / 追书 / 追游」分类。
    """
    base = (
        select(Content)
        .join(ContentFavorite, ContentFavorite.content_id == Content.id)
        .where(ContentFavorite.user_id == user_id)
    )
    if content_type is not None:
        base = base.where(Content.type == content_type)
    total = (
        db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    )
    items = list(
        db.scalars(
            base.order_by(
                ContentFavorite.created_at.desc(),
                Content.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return total, items


def favorite_type_counts(db: Session, user_id: int) -> dict[str, int]:
    """按内容类型统计收藏数，供个人空间分类筛选条展示各分类条数。

    只统计仍能在 contents 中找到的作品（与列表内连接语义一致）。
    """
    rows = db.execute(
        select(Content.type, func.count())
        .select_from(ContentFavorite)
        .join(Content, Content.id == ContentFavorite.content_id)
        .where(ContentFavorite.user_id == user_id)
        .group_by(Content.type)
    ).all()
    return {content_type: count for content_type, count in rows}


def get_content_status(db: Session, user_id: int, content_id: int) -> str | None:
    """取用户对某作品的标注；未标注返回 None。"""
    return db.scalar(
        select(ContentStatus.status).where(
            ContentStatus.user_id == user_id,
            ContentStatus.content_id == content_id,
        )
    )


def get_content_statuses(
    db: Session, user_id: int, content_ids: list[int]
) -> dict[int, str]:
    """批量取多部作品的标注，供收藏列表展示徽标。"""
    if not content_ids:
        return {}
    rows = db.execute(
        select(ContentStatus.content_id, ContentStatus.status).where(
            ContentStatus.user_id == user_id,
            ContentStatus.content_id.in_(content_ids),
        )
    ).all()
    return {content_id: status for content_id, status in rows}


def set_content_status(
    db: Session, user_id: int, content_id: int, status: str
) -> str:
    """设置 / 更新作品标注（同一作品只保留一条），返回写入的标注值。

    status 非法时抛 ValueError，由路由层决定如何回显。
    """
    if status not in CONTENT_STATUS_LABELS:
        raise ValueError(f"不支持的标注：{status}")
    row = db.scalar(
        select(ContentStatus).where(
            ContentStatus.user_id == user_id,
            ContentStatus.content_id == content_id,
        )
    )
    if row is None:
        db.add(ContentStatus(user_id=user_id, content_id=content_id, status=status))
    else:
        row.status = status
        row.updated_at = datetime.now()
    db.commit()
    return status


def list_content_status(
    db: Session,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
) -> tuple[int, list[tuple[Content, str, datetime]]]:
    """标注作品分页（按更新时间倒序）：内连接 contents，作品已删则不展示。

    返回 (总数, [(内容, 标注, 更新时间)])；status 非空时按标注值筛选。
    """
    base = (
        select(Content, ContentStatus.status, ContentStatus.updated_at)
        .join(ContentStatus, ContentStatus.content_id == Content.id)
        .where(ContentStatus.user_id == user_id)
    )
    if status is not None:
        base = base.where(ContentStatus.status == status)
    total = (
        db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    )
    rows = db.execute(
        base.order_by(ContentStatus.updated_at.desc(), Content.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return total, [(row[0], row[1], row[2]) for row in rows]


def count_content_status(
    db: Session, user_id: int, status: str | None = None
) -> int:
    """标注作品数；指定 status 时只数该标注。"""
    stmt = (
        select(func.count())
        .select_from(ContentStatus)
        .join(Content, Content.id == ContentStatus.content_id)
        .where(ContentStatus.user_id == user_id)
    )
    if status is not None:
        stmt = stmt.where(ContentStatus.status == status)
    return db.scalar(stmt) or 0


def content_status_counts(db: Session, user_id: int) -> dict[str, int]:
    """按标注值统计条数（看过 / 未看 / 想看），供个人空间统计卡。"""
    rows = db.execute(
        select(ContentStatus.status, func.count())
        .select_from(ContentStatus)
        .join(Content, Content.id == ContentStatus.content_id)
        .where(ContentStatus.user_id == user_id)
        .group_by(ContentStatus.status)
    ).all()
    return {status: count for status, count in rows}


# ---------- 词条申请 ----------


def _normalize_content_fields(
    title: str, content_type: str, summary: str, rating
) -> tuple[dict | None, str | None]:
    """校验并规范化词条字段；返回 (规范化字段, 错误信息)。"""
    title = (title or "").strip()
    content_type = (content_type or "").strip().lower()
    summary = (summary or "").strip()
    if not title:
        return None, "标题不能为空"
    if len(title) > SUBMISSION_MAX_TITLE:
        return None, f"标题不能超过 {SUBMISSION_MAX_TITLE} 个字符"
    if content_type not in CONTENT_TYPES:
        return None, "类型不合法"
    if not summary:
        return None, "简介不能为空"
    if len(summary) > SUBMISSION_MAX_SUMMARY:
        return None, f"简介不能超过 {SUBMISSION_MAX_SUMMARY} 个字符"
    try:
        rating_value = float(rating)
    except (TypeError, ValueError):
        return None, "评分必须是数字"
    if not 0 <= rating_value <= 10:
        return None, "评分需在 0 到 10 之间"
    return {
        "title": title,
        "content_type": content_type,
        "summary": summary,
        "rating": rating_value,
    }, None


def submit_content(
    db: Session,
    user_id: int,
    kind: str,
    title: str,
    content_type: str,
    summary: str,
    rating,
    content_id: int | None = None,
) -> ContentSubmission:
    """创建一条待审核的词条申请（新增 / 修改），返回申请对象。

    kind / 字段不合法或目标作品不存在时抛 ValueError，由路由层回显。
    """
    if kind not in SUBMISSION_KINDS:
        raise ValueError("申请类型不合法")
    fields, error = _normalize_content_fields(title, content_type, summary, rating)
    if error:
        raise ValueError(error)
    target_id = None
    if kind == SUBMISSION_KIND_UPDATE:
        if content_id is None:
            raise ValueError("缺少要修改的作品")
        if get_content(db, content_id) is None:
            raise ValueError("要修改的作品不存在")
        target_id = content_id
    row = ContentSubmission(
        user_id=user_id,
        kind=kind,
        content_id=target_id,
        status=SUBMISSION_STATUS_PENDING,
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_content_submission(
    db: Session, submission_id: int
) -> ContentSubmission | None:
    """按主键取词条申请，不存在返回 None。"""
    return db.get(ContentSubmission, submission_id)


def list_content_submissions(
    db: Session,
    *,
    user_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[ContentSubmission]]:
    """词条申请分页（按提交时间倒序）；可按提交人与状态筛选。"""
    base = select(ContentSubmission)
    if user_id is not None:
        base = base.where(ContentSubmission.user_id == user_id)
    if status is not None:
        base = base.where(ContentSubmission.status == status)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = list(
        db.scalars(
            base.order_by(
                ContentSubmission.created_at.desc(),
                ContentSubmission.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return total, items


def count_content_submissions(
    db: Session,
    *,
    user_id: int | None = None,
    status: str | None = None,
) -> int:
    """词条申请条数；可按提交人与状态筛选。"""
    stmt = select(func.count()).select_from(ContentSubmission)
    if user_id is not None:
        stmt = stmt.where(ContentSubmission.user_id == user_id)
    if status is not None:
        stmt = stmt.where(ContentSubmission.status == status)
    return db.scalar(stmt) or 0


def decide_content_submission(
    db: Session,
    submission_id: int,
    *,
    approve: bool,
    reviewer_id: int,
    note: str = "",
) -> tuple[ContentSubmission | None, str | None]:
    """审核词条申请：approve=True 写入 / 更新 contents，False 驳回。

    返回 (申请, 错误信息)；只有 pending 可处理，重复审核返回错误，
    保证同一申请不会被写入两次。
    """
    row = get_content_submission(db, submission_id)
    if row is None:
        return None, "申请不存在"
    if row.status != SUBMISSION_STATUS_PENDING:
        return None, "该申请已处理"
    if approve:
        if row.kind == SUBMISSION_KIND_CREATE:
            content = Content(
                title=row.title,
                type=row.content_type,
                summary=row.summary,
                rating=row.rating,
            )
            db.add(content)
            db.flush()
            row.applied_content_id = content.id
        else:
            content = (
                get_content(db, row.content_id) if row.content_id is not None else None
            )
            if content is None:
                return None, "目标作品已不存在，无法通过"
            content.title = row.title
            content.type = row.content_type
            content.summary = row.summary
            content.rating = row.rating
            row.applied_content_id = content.id
        row.status = SUBMISSION_STATUS_APPROVED
    else:
        row.status = SUBMISSION_STATUS_REJECTED
    row.reviewer_id = reviewer_id
    row.reviewed_at = datetime.now()
    row.review_note = (note or "").strip() or None
    db.commit()
    db.refresh(row)
    return row, None


def count_view_history(db: Session, user_id: int) -> int:
    """浏览记录条数（个人空间统计卡）。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(ContentViewHistory)
            .where(ContentViewHistory.user_id == user_id)
        )
        or 0
    )


def record_content_view(db: Session, user_id: int, content_id: int) -> None:
    """记录一次浏览：已浏览过则只更新 viewed_at（不堆叠行）。

    写入后按 viewed_at 倒序裁剪，每人最多保留 VIEW_HISTORY_LIMIT 条。
    新写入/更新的记录一定是最新的，不会被误裁。
    """
    now = datetime.now()
    row = db.scalar(
        select(ContentViewHistory).where(
            ContentViewHistory.user_id == user_id,
            ContentViewHistory.content_id == content_id,
        )
    )
    if row is None:
        db.add(
            ContentViewHistory(
                user_id=user_id, content_id=content_id, viewed_at=now
            )
        )
    else:
        row.viewed_at = now
    db.commit()

    stale_ids = db.scalars(
        select(ContentViewHistory.id)
        .where(ContentViewHistory.user_id == user_id)
        .order_by(ContentViewHistory.viewed_at.desc(), ContentViewHistory.id.desc())
        .offset(VIEW_HISTORY_LIMIT)
    ).all()
    if stale_ids:
        db.execute(
            sa_delete(ContentViewHistory).where(ContentViewHistory.id.in_(stale_ids))
        )
        db.commit()


def remove_view_history(db: Session, user_id: int, content_id: int) -> bool:
    """删除一条浏览记录；返回是否真的删除了一行（幂等）。"""
    result = db.execute(
        sa_delete(ContentViewHistory).where(
            ContentViewHistory.user_id == user_id,
            ContentViewHistory.content_id == content_id,
        )
    )
    db.commit()
    return bool(result.rowcount)


def clear_view_history(db: Session, user_id: int) -> int:
    """清空浏览记录，返回删除行数。"""
    result = db.execute(
        sa_delete(ContentViewHistory).where(ContentViewHistory.user_id == user_id)
    )
    db.commit()
    return result.rowcount or 0


def list_view_history(
    db: Session,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[tuple[Content, datetime]]]:
    """浏览记录分页（按浏览时间倒序）：内连接 contents，作品已删则不展示。

    返回 (总数, [(Content, viewed_at), ...])，模板按作品字段 + 浏览时间渲染。
    """
    base = (
        select(Content, ContentViewHistory.viewed_at)
        .join(ContentViewHistory, ContentViewHistory.content_id == Content.id)
        .where(ContentViewHistory.user_id == user_id)
    )
    total = (
        db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    )
    rows = db.execute(
        base.order_by(
            ContentViewHistory.viewed_at.desc(),
            ContentViewHistory.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return total, [(row.Content, row.viewed_at) for row in rows]


# ---------- 邮箱验证 ----------


def list_unverified_users(db: Session) -> list[User]:
    """待验证账号（有邮箱且未验证），按注册时间倒序。"""
    return list(
        db.scalars(
            select(User)
            .where(User.email.is_not(None), User.email_verified.is_(False))
            .order_by(User.created_at.desc(), User.id.desc())
        )
    )


# ---------- 一次性令牌（邮箱验证 / 找回密码共用） ----------
# 两类令牌结构完全一致：库中只存明文令牌的 SHA-256，明文只出现在邮件或页面链接里。
# 这里抽成通用函数，避免「验证」与「重置」两套逻辑各写一遍后行为跑偏。


def _issue_token(db: Session, model, user_id: int, ttl_seconds: int) -> str:
    """签发一次性令牌：先作废该用户旧的未消费令牌，返回只出现一次的明文。"""
    now = datetime.now()
    for row in db.scalars(
        select(model).where(model.user_id == user_id, model.consumed_at.is_(None))
    ):
        row.consumed_at = now
    token = secrets.token_urlsafe(32)
    db.add(
        model(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=now + timedelta(seconds=ttl_seconds),
            created_at=now,
        )
    )
    db.commit()
    return token


def _lookup_token(db: Session, model, token: str):
    """按哈希取回「未消费且未过期」的令牌记录；不存在或不可用返回 None。"""
    candidate = (token or "").strip()
    if not candidate:
        return None
    record = db.scalar(select(model).where(model.token_hash == hash_token(candidate)))
    if record is None or record.consumed_at is not None:
        return None
    if record.expires_at < datetime.now():
        return None
    return record


def _latest_token_created_at(db: Session, model, user_id: int) -> datetime | None:
    """该账号最近一次签发该类令牌的时间（用于重发冷却判断）。"""
    return db.scalar(
        select(func.max(model.created_at)).where(model.user_id == user_id)
    )


def issue_email_token(db: Session, user: User) -> str:
    """为账号签发新的验证令牌，明文只在本返回值中出现一次。

    同一账号此前未消费的令牌会被作废，避免多链接并存；过期时间由
    ``settings.email_verification_token_ttl_seconds`` 决定。
    """
    return _issue_token(
        db,
        EmailVerificationToken,
        user.id,
        settings.email_verification_token_ttl_seconds,
    )


def latest_token_created_at(db: Session, user_id: int) -> datetime | None:
    """该账号最近一次签发验证令牌的时间（用于重发冷却判断）。"""
    return _latest_token_created_at(db, EmailVerificationToken, user_id)


def consume_email_token(db: Session, token: str) -> User | None:
    """校验并消费验证令牌：成功返回账号并标记邮箱已验证，失败返回 None。

    库中只比对哈希；令牌不存在、已消费或已过期一律返回 None。
    """
    record = _lookup_token(db, EmailVerificationToken, token)
    if record is None:
        return None
    user = db.get(User, record.user_id)
    if user is None:
        return None
    record.consumed_at = datetime.now()
    return mark_email_verified(db, user)


def issue_password_reset_token(db: Session, user: User) -> str:
    """签发找回密码令牌（有效期比邮箱验证更短），明文只出现一次。"""
    return _issue_token(
        db, PasswordResetToken, user.id, settings.password_reset_token_ttl_seconds
    )


def latest_password_reset_created_at(db: Session, user_id: int) -> datetime | None:
    """该账号最近一次签发重置令牌的时间（用于重发冷却判断）。"""
    return _latest_token_created_at(db, PasswordResetToken, user_id)


def mark_email_verified(db: Session, user: User) -> User:
    """将账号标记为邮箱已验证（找回密码成功时顺带完成验证）。"""
    now = datetime.now()
    user.email_verified = True
    user.email_verified_at = now
    db.commit()
    db.refresh(user)
    return user


def verify_password_reset_token(db: Session, token: str) -> User | None:
    """只校验重置令牌，不消费：用于渲染改密表单时先确认链接有效。

    令牌不存在、已消费或已过期返回 None。真正改密时由 ``set_user_password``
    统一消费，避免「打开表单」就把链接用掉。
    """
    record = _lookup_token(db, PasswordResetToken, token)
    if record is None:
        return None
    return db.get(User, record.user_id)


def set_user_password(db: Session, user: User, password: str, token: str) -> bool:
    """消费重置令牌并改密；成功后让该用户此前签发的会话全部失效。

    返回 False 表示令牌无效/已过期，此时不改动任何数据。改密成功后同时
    作废该用户所有未消费的重置令牌，防止同一链接被重复使用。
    """
    record = _lookup_token(db, PasswordResetToken, token)
    if record is None or record.user_id != user.id:
        return False
    now = datetime.now()
    user.password_hash = hash_password(password)
    user.password_changed_at = now
    for row in db.scalars(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        )
    ):
        row.consumed_at = now
    db.commit()
    return True


def mark_email_verified(db: Session, user: User) -> User:
    """管理员手动放行：直接把账号标记为已验证。"""
    user.email_verified = True
    user.email_verified_at = datetime.now()
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
