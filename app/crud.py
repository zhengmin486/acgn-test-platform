"""数据库访问层：内容表查询、搜索与初始化播种。

当前 API 只读；建/改/删由测试用独立临时 SQLite 覆盖，
MySQL 接入与 Alembic 迁移尚未落地。
"""

import secrets
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import (
    Engine,
    delete as sa_delete,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.config import settings
from app.database import Base
from app.fake_data import FAKE_CONTENTS
from app.tagging import canonical_genre, expand_genre_names, split_tags
from app.models import (
    AnimeSchedule,
    Article,
    ArticleContentLink,
    ArticleTag,
    BangumiCollection,
    CASE_SOURCE_MANUAL,
    Content,
    ContentEntityLink,
    ContentFavorite,
    ContentCast,
    ContentCredit,
    ContentFact,
    ContentStudio,
    ContentSubmission,
    ContentTag,
    ContentViewHistory,
    EmailVerificationToken,
    Entity,
    ExecutionBatch,
    ExecutionRecord,
    InviteCode,
    PasswordResetToken,
    PersonaReport,
    ScheduleSyncRun,
    Series,
    SyncTaskRun,
    Tag,
    UserSiteImport,
    TestCase,
    User,
)
from app.schemas import TestCaseCreate
from app.security import hash_password, hash_token


def list_contents(
    db: Session,
    content_type: str | None = None,
    tag: str | None = None,
    tags: list[str] | None = None,
) -> list[Content]:
    """查询内容列表，可按类型与标签筛选，按 id 排序。

    ``tag`` 为单个标签名（精确匹配、区分大小写）；``tags`` 为多个标签名，
    走 AND 语义（同时命中全部标签）。两者可叠加（内部合并去重），
    与类型筛选可叠加。
    """
    stmt = select(Content)

    if content_type is not None:
        stmt = stmt.where(Content.type == content_type)
    wanted = normalize_tag_names(list(tags or []) + ([tag] if tag else []))
    if wanted:
        for name in wanted:
            link = aliased(ContentTag)
            tag_row = aliased(Tag)
            stmt = (
                stmt.join(link, link.content_id == Content.id)
                .join(tag_row, tag_row.id == link.tag_id)
                .where(tag_row.name == name)
            )
        stmt = stmt.distinct()

    stmt = stmt.order_by(Content.id)
    return list(db.scalars(stmt))


def count_contents_by_type(db: Session) -> dict[str, int]:
    """按类型统计内容条数（首页分类浏览卡片）；空表返回空 dict。"""
    rows = db.execute(
        select(Content.type, func.count()).group_by(Content.type)
    ).all()
    return {content_type: count for content_type, count in rows}


def count_articles(db: Session, published_only: bool = True) -> int:
    """资讯总数（首页 hero 数据行）；默认只数已发布。"""
    stmt = select(func.count()).select_from(Article)
    if published_only:
        stmt = stmt.where(Article.is_published.is_(True))
    return int(db.execute(stmt).scalar_one())


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
    """按主键批量取作品，供作品审核页展示「修改目标」；空列表返回空。"""
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

# 单篇资讯最多保留的识别标签数：与作品标签上限一致，防止标签刷屏。
MAX_ARTICLE_TAGS = 5

# 多标签筛选上限：URL 里最多同时按几个标签取交集。再多结果必然为空，
# 反而失去「缩小范围」的意义，也拖慢查询（每个标签一次连接）。
MAX_TAG_FILTERS = 5


def parse_tag_query(
    tags: str | None, tag: str | None = None, *, limit: int = MAX_TAG_FILTERS
) -> list[str]:
    """把 ``?tags=a,b``（逗号分隔，兼容中文逗号/顿号）与旧 ``?tag=`` 合并成列表。

    去重且保持原顺序（先写的权重更高），最多 ``limit`` 个。
    单标签 ``?tag=`` 保留兼容：详情页等旧链接仍可跳转，语义不变。
    """
    names = [
        name.strip()
        for name in (tags or "").replace("，", ",").replace("、", ",").split(",")
        if name.strip()
    ]
    single = (tag or "").strip()
    if single and single not in names:
        names.append(single)
    return names[:limit]


def normalize_tag_names(tag_names: list[str]) -> list[str]:
    """标签名卫生规则（作品标签与资讯识别标签共用，避免两套实现漂移）：

    - 去首尾空白、丢弃空名与超长名（TAG_NAME_MAX_LENGTH，不截断）；
    - 去重且保持原顺序（先出现的排名更靠前）。
    """
    names: list[str] = []
    for raw in tag_names:
        name = (raw or "").strip()
        if not name or len(name) > TAG_NAME_MAX_LENGTH or name in names:
            continue
        names.append(name)
    return names


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

    - 名称走 normalize_tag_names 卫生规则；
    - 最多保留前 MAX_CONTENT_TAGS 个（导入侧已按标注数截断，这里兜底）；
    - 先删旧关联再建新关联：重复导入同一作品不会堆叠关联行，
      标签被源站移除后本地同步移除（标签是派生命名，不是本地资产）。
    """
    names = normalize_tag_names(tag_names)[:MAX_CONTENT_TAGS]

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


def list_content_tags_map(db: Session, content_ids) -> dict[int, list[str]]:
    """批量取多部作品的标签：{content_id: [标签名]}（一次查询，避免列表页 N+1）。

    传入的 id 全部出现在结果里（无标签的作品对应空列表），顺序稳定；
    传入空列表直接返回空 dict，不查库。
    """
    cleaned = sorted({int(item) for item in content_ids or []})
    if not cleaned:
        return {}
    result: dict[int, list[str]] = {content_id: [] for content_id in cleaned}
    rows = db.execute(
        select(ContentTag.content_id, Tag.name)
        .join(Tag, Tag.id == ContentTag.tag_id)
        .where(ContentTag.content_id.in_(cleaned))
        .order_by(Tag.id)
    )
    for content_id, name in rows:
        result[int(content_id)].append(name)
    return result


def replace_content_studios(
    db: Session, content_id: int, names: list[str]
) -> list[str]:
    """整体替换某作品的制作公司（保序去重，最多 80 字符），返回最终列表。"""
    cleaned: list[str] = []
    for name in names or []:
        text_value = str(name).strip()
        if text_value and len(text_value) <= 80 and text_value not in cleaned:
            cleaned.append(text_value)

    db.execute(sa_delete(ContentStudio).where(ContentStudio.content_id == content_id))
    for name in cleaned:
        db.add(ContentStudio(content_id=content_id, name=name))
    db.commit()
    return cleaned


def list_content_studios(db: Session, content_id: int) -> list[str]:
    """某作品的制作公司列表（按写入顺序）。"""
    return list(
        db.scalars(
            select(ContentStudio.name)
            .where(ContentStudio.content_id == content_id)
            .order_by(ContentStudio.id)
        )
    )


def replace_content_credits(
    db: Session, content_id: int, credits: list[dict]
) -> int:
    """整体替换某作品的制作人员（幂等），返回写入条数。"""
    rows: list[ContentCredit] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(credits or []):
        role = str(item.get("role") or "").strip()[:30]
        name = str(item.get("name") or "").strip()[:100]
        if not role or not name:
            continue
        key = (role, name)
        if key in seen:
            continue
        seen.add(key)
        detail = str(item.get("detail") or "").strip()[:200] or None
        person_id = item.get("person_id")
        tier = str(item.get("tier") or "main")
        if tier not in ("main", "long", "candidate"):
            tier = "main"
        rows.append(
            ContentCredit(
                content_id=content_id,
                person_id=int(person_id) if person_id else None,
                role=role,
                name=name,
                detail=detail,
                sort_order=int(item.get("sort_order") or index),
                tier=tier,
            )
        )

    db.execute(sa_delete(ContentCredit).where(ContentCredit.content_id == content_id))
    for row in rows:
        db.add(row)
    db.commit()
    return len(rows)


def promote_staff_candidates(
    db: Session,
    *,
    names: list[str] | None = None,
    threshold: int = 10,
) -> int:
    """长尾职员转正：本站累计 ≥ threshold 部作品的候选升级为 long（展示）。

    - 「作品数」按 ``distinct content_id`` 统计，同一作品多职位只算一部；
    - names 给定时只检查这些人（同步单部作品后的增量检查），为空则全表扫；
    - 不维护「知名名单」：数据越多越准，名单不会过时。
    """
    stmt = (
        select(ContentCredit.name, func.count(func.distinct(ContentCredit.content_id)))
        .where(ContentCredit.tier.in_(("candidate", "long")))
        .group_by(ContentCredit.name)
        .having(func.count(func.distinct(ContentCredit.content_id)) >= threshold)
    )
    if names:
        stmt = stmt.where(ContentCredit.name.in_(names))
    eligible = [str(name) for name, _count in db.execute(stmt)]
    if not eligible:
        return 0

    result = db.execute(
        update(ContentCredit)
        .where(ContentCredit.tier == "candidate", ContentCredit.name.in_(eligible))
        .values(tier="long")
    )
    db.commit()
    return result.rowcount or 0


def list_content_credits(
    db: Session, content_id: int, tier: str | None = None
) -> list[ContentCredit]:
    """某作品的制作人员（按 sort_order 稳定排序）。"""
    stmt = select(ContentCredit).where(ContentCredit.content_id == content_id)
    if tier:
        stmt = stmt.where(ContentCredit.tier == tier)
    return list(db.scalars(stmt.order_by(ContentCredit.sort_order, ContentCredit.id)))


def replace_content_cast(db: Session, content_id: int, cast: list[dict]) -> int:
    """整体替换角色-声优（只存原版配音），返回写入条数。"""
    rows: list[ContentCast] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(cast or []):
        character = str(item.get("character") or "").strip()[:100]
        actor = str(item.get("actor") or "").strip()[:100]
        if not character or not actor:
            continue
        key = (character, actor)
        if key in seen:
            continue
        seen.add(key)
        character_id = item.get("character_id")
        person_id = item.get("person_id")
        rows.append(
            ContentCast(
                content_id=content_id,
                character=character,
                character_id=int(character_id) if character_id else None,
                person_id=int(person_id) if person_id else None,
                actor=actor,
                sort_order=int(item.get("sort_order") or index),
            )
        )

    db.execute(sa_delete(ContentCast).where(ContentCast.content_id == content_id))
    for row in rows:
        db.add(row)
    db.commit()
    return len(rows)


def list_content_cast(
    db: Session, content_id: int, limit: int | None = None
) -> list[ContentCast]:
    """某作品的角色-声优（按出场顺序）。"""
    stmt = (
        select(ContentCast)
        .where(ContentCast.content_id == content_id)
        .order_by(ContentCast.sort_order, ContentCast.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def replace_content_facts(db: Session, content_id: int, facts: dict) -> int:
    """整体替换客观信息键值（幂等），返回写入条数。"""
    items = []
    for key, value in (facts or {}).items():
        name = str(key).strip()[:50]
        text_value = str(value).strip()[:500]
        if name and text_value:
            items.append((name, text_value))

    db.execute(sa_delete(ContentFact).where(ContentFact.content_id == content_id))
    for name, text_value in items:
        db.add(ContentFact(content_id=content_id, key=name, value=text_value))
    db.commit()
    return len(items)


def list_content_facts(db: Session, content_id: int) -> list[tuple[str, str]]:
    """某作品的客观信息（按写入顺序）。"""
    rows = db.execute(
        select(ContentFact.key, ContentFact.value)
        .where(ContentFact.content_id == content_id)
        .order_by(ContentFact.id)
    ).all()
    return [(str(key), str(value)) for key, value in rows]


def recommend_contents(
    db: Session,
    *,
    positive_tags: list[str],
    avoided_tags: list[str],
    exclude_bangumi_ids: set[int] | None = None,
    limit: int = 6,
) -> list[dict]:
    """按口味从站内作品库挑推荐：本命题材命中打分，避雷题材一票否决。

    - 匹配两侧都过题材归一：口味词（画像里的 Bangumi 原始标签 / 用户自然语言）
      经 ``tagging.expand_genre_names`` 展开成站内真实标签名，作品标签也归一到
      规范名——「日常系」「致郁系」「体育竞技」不再精确对撞落空（避雷静默失效）；
    - 只推有题材标签的作品（无标签无法证明命中）；
    - 命中数为主排序，评分做同分加权；已收藏（bangumi_id 命中快照）的剔除；
    - 避雷标签出现在作品标签里就直接跳过——宁可少推，不推雷。
    """
    exclude = {int(value) for value in (exclude_bangumi_ids or [])}

    rows = db.execute(
        select(Content, Tag.name)
        .join(ContentTag, ContentTag.content_id == Content.id)
        .join(Tag, Tag.id == ContentTag.tag_id)
        .order_by(Content.id, Tag.id)
    ).all()
    content_by_id: dict[int, Content] = {}
    tags_by_content: dict[int, set[str]] = {}
    site_tags: set[str] = set()
    for content, tag_name in rows:
        content_by_id[content.id] = content
        name = str(tag_name)
        # 站内标签也归一到规范名：未洗过的长尾写法（「日常系」）能对上口味词
        canonical = canonical_genre(name) or name
        site_tags.update((name, canonical))
        tags_by_content.setdefault(content.id, set()).add(canonical)

    positive = expand_genre_names(positive_tags or [], site_tags)
    avoided = set(expand_genre_names(avoided_tags or [], site_tags))

    scored: list[dict] = []
    for content_id, tags in tags_by_content.items():
        content = content_by_id[content_id]
        if content.bangumi_id is not None and int(content.bangumi_id) in exclude:
            continue
        if tags & avoided:
            continue
        matched = [name for name in positive if name in tags]
        if not matched:
            continue
        scored.append(
            {
                "content_id": content.id,
                "title": content.title,
                "type": content.type,
                "year": content.year,
                "rating": float(content.rating or 0),
                "matched": matched[:3],
                "score": len(matched) * 10 + float(content.rating or 0),
            }
        )

    scored.sort(key=lambda item: (-item["score"], -item["rating"], item["content_id"]))
    results = []
    for item in scored[: max(0, limit)]:
        results.append(
            {
                "content_id": item["content_id"],
                "title": item["title"],
                "type": item["type"],
                "year": item["year"],
                "rating": item["rating"],
                "matched": item["matched"],
                "reason": f"命中本命题材：{'、'.join(item['matched'])}",
            }
        )
    return results


def list_engaged_content_ids(db: Session) -> set[int]:
    """有互动的作品 id：被收藏 / 标注 / 浏览过（多用户并集，补全与封面共用）。

    标注已并入收藏行（content_favorites），所以只需扫收藏与浏览记录两张表。
    """
    ids: set[int] = set()
    for model in (ContentFavorite, ContentViewHistory):
        ids.update(int(value) for value in db.scalars(select(model.content_id).distinct()))
    return ids


def list_person_works(db: Session, person_id: int) -> dict:
    """按 Bangumi 人物 id 聚合作品：{credits: [(内容, 职位)], cast: [(内容, 角色)]}。"""
    credit_rows = db.execute(
        select(Content, ContentCredit.role)
        .join(ContentCredit, ContentCredit.content_id == Content.id)
        .where(ContentCredit.person_id == person_id)
        .order_by(Content.rating.desc(), Content.id.desc())
    ).all()
    cast_rows = db.execute(
        select(Content, ContentCast.character)
        .join(ContentCast, ContentCast.content_id == Content.id)
        .where(ContentCast.person_id == person_id)
        .order_by(Content.rating.desc(), Content.id.desc())
    ).all()
    return {
        "credits": [(content, str(role)) for content, role in credit_rows],
        "cast": [(content, str(character)) for content, character in cast_rows],
    }


def person_name(db: Session, person_id: int) -> str:
    """从已有记录里取人物名（声优或制作人员），取不到返回空串。"""
    name = db.scalar(
        select(ContentCredit.name).where(ContentCredit.person_id == person_id).limit(1)
    ) or db.scalar(
        select(ContentCast.actor).where(ContentCast.person_id == person_id).limit(1)
    )
    return str(name or "")


def list_contents_by_studio(db: Session, name: str) -> list[Content]:
    """某制作公司的作品（按评分倒序）。"""
    return list(
        db.scalars(
            select(Content)
            .join(ContentStudio, ContentStudio.content_id == Content.id)
            .where(ContentStudio.name == name)
            .order_by(Content.rating.desc(), Content.id.desc())
        )
    )


def list_tags_with_counts(
    db: Session,
    limit: int | None = 30,
    content_type: str | None = None,
) -> list[tuple[str, int]]:
    """标签及关联作品数，按作品数从多到少（筛选栏用）；空库返回空列表。

    count 统计的是 content_tags 关联行数；作品被删除后关联行仍在，
    计数可能略高于可见作品数——标签筛选走内连接，结果不受影响。
    content_type 传入时只统计该类型作品上的标签：动画的「机战」「BL」、
    游戏的「PC」「RPG」不跨类型混入筛选栏（四类内容各用各的标签集）。
    """
    stmt = (
        select(Tag.name, func.count(ContentTag.id))
        .join(ContentTag, ContentTag.tag_id == Tag.id)
        .join(Content, Content.id == ContentTag.content_id)
    )
    if content_type:
        stmt = stmt.where(Content.type == content_type)
    stmt = (
        stmt.group_by(Tag.id, Tag.name)
        .order_by(func.count(ContentTag.id).desc(), Tag.name)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(name, count) for name, count in db.execute(stmt).all()]


def list_all_tag_names(db: Session) -> list[str]:
    """全部标签名（识别词表用，按 id 稳定排序）。

    只返回名字，不做计数过滤：孤儿标签（已无作品关联）也保留在词表里——
    它们仍是有效的题材词，历史资讯可能提到。
    """
    return list(db.scalars(select(Tag.name).order_by(Tag.id)))


def list_tags_page(
    db: Session,
    *,
    keyword: str = "",
    page: int = 1,
    page_size: int = 50,
) -> tuple[int, list[tuple[Tag, int]]]:
    """管理端标签分页：[(Tag, 关联作品数)]，按作品数降序、同数按名称。

    与面向读者的 ``list_tags_with_counts`` 的区别：左连接把孤儿标签
    （计数 0）也列出来——管理页要能看见并清理它们；支持关键词过滤。
    """
    stmt = (
        select(Tag, func.count(ContentTag.id))
        .join(ContentTag, ContentTag.tag_id == Tag.id, isouter=True)
        .group_by(Tag.id, Tag.name)
    )
    if keyword:
        stmt = stmt.where(Tag.name.like(f"%{keyword}%"))
    total_stmt = select(func.count()).select_from(Tag)
    if keyword:
        total_stmt = total_stmt.where(Tag.name.like(f"%{keyword}%"))
    total = int(db.scalar(total_stmt) or 0)
    rows = db.execute(
        stmt.order_by(func.count(ContentTag.id).desc(), Tag.name)
        .offset((max(page, 1) - 1) * page_size)
        .limit(page_size)
    ).all()
    return total, [(tag, count) for tag, count in rows]


def tag_health(db: Session) -> dict:
    """标签体系健康度（管理端看板用，一次查询出齐）。

    返回：
    - tag_total / used / orphan：标签行总数、被引用的、无人引用的（孤儿）；
    - content_total / without_tags：作品总数、一部题材标签都没有的作品数；
    - by_type：{类型: (作品数, 有题材标签的作品数)}——分区题材覆盖度；
    - tags_per_content：有标签作品的平均 / 最大标签数。
    """
    tag_total = int(db.scalar(select(func.count()).select_from(Tag)) or 0)
    used = int(
        db.scalar(select(func.count(func.distinct(ContentTag.tag_id)))) or 0
    )
    content_total = int(db.scalar(select(func.count()).select_from(Content)) or 0)
    without_tags = int(
        db.scalar(
            select(func.count())
            .select_from(Content)
            .where(Content.id.not_in(select(ContentTag.content_id)))
        )
        or 0
    )
    tagged_by_type = {
        content_type: count
        for content_type, count in db.execute(
            select(Content.type, func.count(func.distinct(Content.id)))
            .join(ContentTag, ContentTag.content_id == Content.id)
            .group_by(Content.type)
        )
    }
    total_by_type = count_contents_by_type(db)
    # 平均 / 最大标签数：先按作品分组计数，再对计数取聚合（不能对 count 直接 avg）
    per_content = (
        select(func.count(ContentTag.tag_id).label("tag_count"))
        .group_by(ContentTag.content_id)
        .subquery()
    )
    avg_tags, max_tags = db.execute(
        select(func.avg(per_content.c.tag_count), func.max(per_content.c.tag_count))
    ).one()
    return {
        "tag_total": tag_total,
        "used": used,
        "orphan": max(tag_total - used, 0),
        "content_total": content_total,
        "without_tags": without_tags,
        "by_type": {
            content_type: (total, tagged_by_type.get(content_type, 0))
            for content_type, total in total_by_type.items()
        },
        "avg_tags": round(float(avg_tags), 1) if avg_tags else 0.0,
        "max_tags": int(max_tags or 0),
    }


def rename_tag(db: Session, name: str, new_name: str) -> tuple[bool, str]:
    """改规范名；目标名已存在时拒绝（那种情况应该用合并）。"""
    tag = db.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        return False, f"标签「{name}」不存在"
    cleaned = (new_name or "").strip()
    if not cleaned or len(cleaned) > TAG_NAME_MAX_LENGTH:
        return False, "新名称不能为空或超过长度上限"
    if cleaned == name:
        return False, "名称没有变化"
    if db.scalar(select(Tag.id).where(Tag.name == cleaned)) is not None:
        return False, f"「{cleaned}」已存在；要合并两个标签请用合并功能"
    tag.name = cleaned
    db.commit()
    return True, f"已将「{name}」改名为「{cleaned}」"


def merge_tags(db: Session, source_name: str, target_name: str) -> tuple[bool, str]:
    """把源标签的全部关联并入目标标签，然后删除源标签行。

    - 目标不存在时退化为改名（源标签直接改名成目标名）；
    - 作品已同时带有两个标签时，删掉源关联（联合唯一约束下不能有两行）；
    - 返回 (是否成功, 页面回显消息)。
    """
    source = db.scalar(select(Tag).where(Tag.name == source_name))
    if source is None:
        return False, f"源标签「{source_name}」不存在"
    if source_name == target_name:
        return False, "源标签与目标标签相同"
    target = db.scalar(select(Tag).where(Tag.name == target_name))
    if target is None:
        source.name = target_name
        db.commit()
        return True, f"「{target_name}」不存在，已将「{source_name}」改名为它"

    rows = list(db.scalars(select(ContentTag).where(ContentTag.tag_id == source.id)))
    moved = 0
    for row in rows:
        clash = db.scalar(
            select(ContentTag.id).where(
                ContentTag.content_id == row.content_id,
                ContentTag.tag_id == target.id,
            )
        )
        if clash is not None:
            db.delete(row)  # 已有目标标签：这条重复关联直接去掉
        else:
            row.tag_id = target.id
            moved += 1
    db.delete(source)
    db.commit()
    return True, f"已把 {moved} 条关联并入「{target_name}」，并删除「{source_name}」"


def delete_orphan_tags(db: Session) -> int:
    """删除没有任何作品引用的标签行，返回删除行数（管理端一键清理）。"""
    orphan_ids = [
        int(tag_id)
        for tag_id in db.scalars(
            select(Tag.id).where(Tag.id.not_in(select(ContentTag.tag_id).distinct()))
        )
    ]
    if not orphan_ids:
        return 0
    db.execute(sa_delete(Tag).where(Tag.id.in_(orphan_ids)))
    db.commit()
    return len(orphan_ids)


def list_content_titles(db: Session) -> list[tuple[int, str]]:
    """全部作品的 (id, 标题)（关联识别的匹配词表，按 id 排序）。"""
    return [(row[0], row[1]) for row in db.execute(select(Content.id, Content.title)).all()]


# ---------- 资讯自动识别（标签 / 关联作品，写入见 app/article_tags.py） ----------


def replace_article_tags(db: Session, article_id: int, tag_names: list[str]) -> list[str]:
    """替换某篇资讯的识别标签：先删旧关联再按 names 建新关联，返回最终标签名。

    与 replace_content_tags 同一套卫生规则（normalize_tag_names + 前
    MAX_ARTICLE_TAGS 个截断）；传空列表 = 清空该资讯的标签。

    与 replace_content_tags 的差别：本函数**不自行 commit**——调用方
    （导入批次 / 回填 CLI）按自己的事务边界统一提交，失败可整体回滚。
    """
    names = normalize_tag_names(tag_names)[:MAX_ARTICLE_TAGS]
    db.execute(sa_delete(ArticleTag).where(ArticleTag.article_id == article_id))
    for name in names:
        tag = get_or_create_tag(db, name)
        db.add(ArticleTag(article_id=article_id, tag_id=tag.id))
    return names


def list_article_tags(db: Session, article_id: int) -> list[str]:
    """某篇资讯的识别标签名列表（按关联行入库顺序，即识别得分序）。"""
    return list(
        db.scalars(
            select(Tag.name)
            .join(ArticleTag, ArticleTag.tag_id == Tag.id)
            .where(ArticleTag.article_id == article_id)
            .order_by(ArticleTag.id)
        )
    )


def article_tags_map(db: Session, article_ids: list[int]) -> dict[int, list[str]]:
    """批量取多篇资讯的识别标签（列表页用，一次查询，避免 N+1）。

    返回 {article_id: [标签名...]}；没有标签的资讯不出现在字典里（模板按
    ``map.get(article.id)`` 取用，缺失即空态）。组内顺序同 list_article_tags
    （关联行入库顺序，即识别得分序）。
    """
    if not article_ids:
        return {}
    rows = db.execute(
        select(ArticleTag.article_id, Tag.name)
        .join(Tag, Tag.id == ArticleTag.tag_id)
        .where(ArticleTag.article_id.in_(article_ids))
        .order_by(ArticleTag.article_id, ArticleTag.id)
    ).all()
    result: dict[int, list[str]] = {}
    for article_id, name in rows:
        result.setdefault(article_id, []).append(name)
    return result


def replace_article_content_links(db: Session, article_id: int, titles: list[str]) -> list[Content]:
    """替换某篇资讯的关联作品：按标题匹配 contents，先删旧关联再建新关联。

    titles 为识别出的作品标题（调用方已按得分排序）：同标题作品会全部
    关联——同名条目很少见，全部关联比任选其一更忠实；返回的作品列表保持
    传入标题的顺序（得分序），供页面按相关度渲染。空列表 = 清空关联。

    与 replace_article_tags 一样不自行 commit（调用方统一事务边界）。
    """
    unique = list(dict.fromkeys(title.strip() for title in titles if title and title.strip()))
    db.execute(sa_delete(ArticleContentLink).where(ArticleContentLink.article_id == article_id))
    if not unique:
        return []
    by_title: dict[str, list[Content]] = {}
    for content in db.scalars(select(Content).where(Content.title.in_(unique))):
        by_title.setdefault(content.title, []).append(content)
    ordered = [content for title in unique for content in by_title.get(title, [])]
    for content in ordered:
        db.add(ArticleContentLink(article_id=article_id, content_id=content.id))
    return ordered


def list_article_contents(db: Session, article_id: int, limit: int | None = None) -> list[Content]:
    """某篇资讯关联的作品列表（按关联行入库顺序，即识别得分序；limit 限制条数）。"""
    stmt = (
        select(Content)
        .join(ArticleContentLink, ArticleContentLink.content_id == Content.id)
        .where(ArticleContentLink.article_id == article_id)
        .order_by(ArticleContentLink.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def count_article_content_links(db: Session, article_id: int) -> int:
    """某篇资讯的关联作品数（CLI 汇总与去重跳过判断用）。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(ArticleContentLink)
            .where(ArticleContentLink.article_id == article_id)
        )
        or 0
    )


# ---------- 作品关系：作者 / 公司花名册 + 系列（识别见 app/entity_links.py） ----------

# 实体类型三分（与内容类型 person/company/association 的展示标签配套）。
ENTITY_TYPE_PERSON = "person"
ENTITY_TYPE_COMPANY = "company"
ENTITY_TYPE_ASSOCIATION = "association"
ENTITY_TYPES = (ENTITY_TYPE_PERSON, ENTITY_TYPE_COMPANY, ENTITY_TYPE_ASSOCIATION)
ENTITY_TYPE_LABELS = {
    ENTITY_TYPE_PERSON: "个人",
    ENTITY_TYPE_COMPANY: "公司",
    ENTITY_TYPE_ASSOCIATION: "协会",
}

# 名称 / 角色长度上限：与 Entity.name String(120)、role String(40) 对齐，
# 超长直接丢弃不截断——截断人名会产生错误的关联。
ENTITY_NAME_MAX_LENGTH = 120
ENTITY_ROLE_MAX_LENGTH = 40
SERIES_NAME_MAX_LENGTH = 200

# 单部作品最多保留的作者/公司关联数：一部作品通常原作+作画+动画制作+音乐
# 几个角色，12 个足够容纳且防止信息卡里的杂项刷屏。
MAX_CONTENT_ENTITY_LINKS = 12


def normalize_entity_name(name: str | None) -> str:
    """实体名卫生规则：NFKC 归一 + 去首尾空白（不做大小写合并）。

    Bangumi 信息卡的键值可能带全角字符或首尾空白；名字本身的大小写是
    身份的一部分（A-1 Pictures 与 a-1 pictures 不是同一家公司），故只做
    NFKC 与 strip。
    """
    if not name:
        return ""
    return unicodedata.normalize("NFKC", str(name)).strip()


def _clean_entity_link(link) -> tuple[str, str, str] | None:
    """把 (name, entity_type, role) 三元组清洗成规范形态，不合格返回 None。

    三项全部去空白；name 上限 ENTITY_NAME_MAX_LENGTH、role 上限
    ENTITY_ROLE_MAX_LENGTH；entity_type 不在枚举内回落为 person。
    """
    name = normalize_entity_name(link[0] if link else "")
    role = (link[2] if link and len(link) > 2 else "") or ""
    role = unicodedata.normalize("NFKC", str(role)).strip()[:ENTITY_ROLE_MAX_LENGTH]
    entity_type = (link[1] if link and len(link) > 1 else "") or ENTITY_TYPE_PERSON
    entity_type = entity_type if entity_type in ENTITY_TYPES else ENTITY_TYPE_PERSON
    if not name or len(name) > ENTITY_NAME_MAX_LENGTH or not role:
        return None
    return name, entity_type, role


def get_or_create_entity(
    db: Session,
    name: str,
    entity_type: str = ENTITY_TYPE_PERSON,
) -> Entity:
    """按名称取实体，不存在则创建；调用方负责把名称归一化到长度上限内。

    与 get_or_create_tag 同模式：flush 拿到 id 供同批关联行引用，commit 由
    调用方（replace_content_entity_links 或导入批次）统一决定。
    """
    entity = db.scalar(select(Entity).where(Entity.name == name))
    if entity is None:
        entity = Entity(name=name, entity_type=entity_type)
        db.add(entity)
        db.flush()
    return entity


def replace_content_entity_links(
    db: Session,
    content_id: int,
    links: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """整体替换某作品的作者/公司关联，返回最终生效的 (name, type, role)。

    - links 每项为 (名称, 类型, 角色)，走 _clean_entity_link 清洗，
      同 (名称, 角色) 去重（类型以首次出现的为准）；
    - 最多保留前 MAX_CONTENT_ENTITY_LINKS 项；
    - 先删旧关联再建新关联：重复导入同一作品不堆叠行，信息卡更新后本地
      同步更新（与 replace_content_tags 同语义）；
    - 与 replace_content_tags 一样自行 commit（导入侧每步独立提交）。
    """
    cleaned: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in links or []:
        item = _clean_entity_link(link)
        if item is None:
            continue
        name, _entity_type, role = item
        if (name, role) in seen:
            continue
        seen.add((name, role))
        cleaned.append(item)
        if len(cleaned) >= MAX_CONTENT_ENTITY_LINKS:
            break

    db.execute(sa_delete(ContentEntityLink).where(ContentEntityLink.content_id == content_id))
    for name, entity_type, role in cleaned:
        entity = get_or_create_entity(db, name, entity_type)
        db.add(ContentEntityLink(content_id=content_id, entity_id=entity.id, role=role))
    db.commit()
    return cleaned


def list_content_entity_links(
    db: Session,
    content_id: int,
) -> list[tuple[int, str, str]]:
    """某作品的关联明细 (link_id, role, name)（管理端删除用，按关联行 id 序）。"""
    rows = db.execute(
        select(ContentEntityLink.id, ContentEntityLink.role, Entity.name)
        .join(Entity, Entity.id == ContentEntityLink.entity_id)
        .where(ContentEntityLink.content_id == content_id)
        .order_by(ContentEntityLink.id)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def list_content_entity_roles(
    db: Session,
    content_id: int,
) -> dict[str, list[str]]:
    """某作品的作者/公司按角色分组：{角色: [名称...]}（详情页 chips 用）。

    组内顺序即关联行入库顺序（识别得分序）；角色之间保持首次出现顺序。
    """
    grouped: dict[str, list[str]] = {}
    for _link_id, role, name in list_content_entity_links(db, content_id):
        grouped.setdefault(role, []).append(name)
    return grouped


def count_content_entity_links(db: Session, content_id: int) -> int:
    """某作品的作者/公司关联数（CLI 汇总与去重跳过判断用）。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(ContentEntityLink)
            .where(ContentEntityLink.content_id == content_id)
        )
        or 0
    )


def delete_content_entity_link(db: Session, link_id: int) -> bool:
    """删除单条作品-实体关联（管理端手工纠错），返回是否删除。"""
    result = db.execute(sa_delete(ContentEntityLink).where(ContentEntityLink.id == link_id))
    db.commit()
    return bool(result.rowcount)


def add_content_entity_link(
    db: Session,
    content_id: int,
    name: str,
    role: str,
    entity_type: str = ENTITY_TYPE_PERSON,
) -> tuple[bool, str]:
    """管理端手工补一条作品-实体关联，返回 (是否成功, 说明)。

    已存在的同 (作品, 实体, 角色) 组合视为成功并提示「已存在」——手工补录
    重复点击不应报错。角色为空或名称超长则拒绝。
    """
    item = _clean_entity_link((name, entity_type, role))
    if item is None:
        return False, "名称或角色为空（或名称超长）"
    clean_name, clean_type, clean_role = item
    entity = get_or_create_entity(db, clean_name, clean_type)
    link = ContentEntityLink(content_id=content_id, entity_id=entity.id, role=clean_role)
    db.add(link)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return True, f"已存在：{clean_name}（{clean_role}）"
    return True, f"已添加：{clean_name}（{clean_role}）"


def list_entities_with_counts(
    db: Session,
    entity_type: str | None = None,
    limit: int | None = None,
) -> list[tuple[Entity, int]]:
    """实体花名册及关联作品数，按作品数从多到少（索引页用）。

    entity_type 给定时只返回该类型；作品数统计关联行数（弱关联，作品被删
    后关联行仍在，计数可能略高于可见作品数，页面内连接查不到即不展示）。
    """
    stmt = (
        select(Entity, func.count(ContentEntityLink.id))
        .join(ContentEntityLink, ContentEntityLink.entity_id == Entity.id)
        .group_by(Entity.id, Entity.name, Entity.entity_type)
        .order_by(func.count(ContentEntityLink.id).desc(), Entity.name)
    )
    if entity_type in ENTITY_TYPES:
        stmt = stmt.where(Entity.entity_type == entity_type)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(entity, count) for entity, count in db.execute(stmt).all()]


def get_entity_by_name(db: Session, name: str) -> Entity | None:
    """按名称取实体（名称需先归一化）；索引页跳转与筛选反查共用。"""
    return db.scalar(select(Entity).where(Entity.name == normalize_entity_name(name)))


def list_entity_contents(
    db: Session,
    entity_id: int,
    limit: int | None = None,
) -> list[Content]:
    """某实体的全部作品（评分降序、同分按 id；limit 限制条数）。

    内连接：作品被删后关联行查不到即不出现。同一作品以多个角色关联同一
    实体时只出现一次（distinct）。
    """
    stmt = (
        select(Content)
        .join(ContentEntityLink, ContentEntityLink.content_id == Content.id)
        .where(ContentEntityLink.entity_id == entity_id)
        .distinct()
        .order_by(Content.rating.desc(), Content.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def get_or_create_series(db: Session, name: str) -> Series:
    """按名称取系列，不存在则创建；名称需先归一化到长度上限内。

    flush 不 commit：系列设置跟随导入/回填批次的事务边界（与
    get_or_create_entity 的差别是后者在替换函数内统一提交）。
    """
    series = db.scalar(select(Series).where(Series.name == name))
    if series is None:
        series = Series(name=name)
        db.add(series)
        db.flush()
    return series


def set_content_series(db: Session, content_id: int, series_name: str | None) -> str | None:
    """设置（或清除）某作品的所属系列，返回最终系列名（None = 已清除）。

    空名 = 清除系列；系列名超长（SERIES_NAME_MAX_LENGTH）直接拒绝并返回
    None。自行 commit（管理端单次操作与导入批次都按这一语义）。
    """
    name = normalize_entity_name(series_name)[:SERIES_NAME_MAX_LENGTH]
    content = db.get(Content, content_id)
    if content is None:
        return None
    if not name:
        content.series_id = None
        db.commit()
        return None
    series = get_or_create_series(db, name)
    content.series_id = series.id
    db.commit()
    return series.name


def get_series(db: Session, series_id: int) -> Series | None:
    """按 ID 取系列。"""
    return db.get(Series, series_id)


def get_series_by_name(db: Session, name: str) -> Series | None:
    """按名称取系列（名称需先归一化）；/creators/{name} 解析与筛选反查共用。"""
    return db.scalar(select(Series).where(Series.name == normalize_entity_name(name)))


def list_series_with_counts(
    db: Session,
    limit: int | None = None,
) -> list[tuple[Series, int]]:
    """全部系列及作品数，按作品数从多到少（索引页用）。"""
    stmt = (
        select(Series, func.count(Content.id))
        .join(Content, Content.series_id == Series.id)
        .group_by(Series.id, Series.name)
        .order_by(func.count(Content.id).desc(), Series.name)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(series, count) for series, count in db.execute(stmt).all()]


def list_series_contents(
    db: Session,
    series_id: int,
    exclude_content_id: int | None = None,
    limit: int | None = None,
) -> list[Content]:
    """同系列的作品（评分降序；exclude_content_id 排除「当前作品」自身）。"""
    stmt = (
        select(Content)
        .where(Content.series_id == series_id)
        .order_by(Content.rating.desc(), Content.id)
    )
    if exclude_content_id is not None:
        stmt = stmt.where(Content.id != exclude_content_id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def has_content_entity_recognition(db: Session, content_id: int) -> bool:
    """某作品是否已有关系数据（作者/公司关联或所属系列）。

    回填 CLI 的跳过判断：已识别的作品默认不再重算（--force 才重算），
    避免每次全量回填都重复请求 Bangumi。
    """
    if count_content_entity_links(db, content_id) > 0:
        return True
    content = db.get(Content, content_id)
    return bool(content and content.series_id is not None)


def list_articles(
    db: Session,
    category: str | None = None,
    published_only: bool = True,
    limit: int | None = None,
    q: str | None = None,
) -> list[Article]:
    """查询资讯列表，可按分类筛选与关键词搜索，默认只显示已发布文章；limit 限制返回条数。

    q 为标题/摘要/正文的关键词包含匹配（不区分大小写），与 search_articles_page 同语义。
    """
    stmt = select(Article)

    if published_only:
        stmt = stmt.where(Article.is_published.is_(True))

    if category:
        stmt = stmt.where(Article.category == category)

    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Article.title).like(pattern),
                func.lower(Article.summary).like(pattern),
                func.lower(Article.body).like(pattern),
            )
        )

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

def _content_search_stmt(
    keyword: str,
    content_type: str | None,
    tag: str | None = None,
    creator: str | None = None,
    tags: list[str] | None = None,
):
    """内容搜索的共享查询：标题/简介不区分大小写包含匹配，可叠加类型、标签与作者筛选。

    匹配语义保持原样（lower 后包含匹配）；分页与不分页的调用方共用本构造，
    避免两套搜索实现漂移。筛选走连接下推到 SQL，不在应用层过滤：

    - 标签筛选走 content_tags/tags 连接；多标签（``tags``）为 **AND** 语义——
      每个标签各做一次别名连接，同时命中全部标签的作品才出现（联合唯一
      约束保证同一作品命中同一标签只算一次）；
    - 作者/公司筛选走 content_entity_links/entities 连接按名称等值匹配
      （索引页「查看全部作品」与列表页 ?creator= 共用）。

    tag 与 creator 同时给定时连接会放大行数（一部作品命中标签又有多个
    角色的同一实体），故任一筛选生效时统一 distinct，保证一部作品在结果
    里只出现一次；单筛选时 distinct 不改变语义。
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
    wanted = normalize_tag_names(list(tags or []) + ([tag] if tag else []))
    for name in wanted:
        link = aliased(ContentTag)
        tag_row = aliased(Tag)
        stmt = (
            stmt.join(link, link.content_id == Content.id)
            .join(tag_row, tag_row.id == link.tag_id)
            .where(tag_row.name == name)
        )
    if creator:
        stmt = (
            stmt.join(ContentEntityLink, ContentEntityLink.content_id == Content.id)
            .join(Entity, Entity.id == ContentEntityLink.entity_id)
            .where(Entity.name == creator)
        )
    if wanted or creator:
        stmt = stmt.distinct()
    return stmt.order_by(Content.id)


def search_contents(
    db: Session,
    keyword: str,
    tag: str | None = None,
    creator: str | None = None,
    tags: list[str] | None = None,
) -> list[Content]:
    """标题/简介不区分大小写的包含匹配，与分页版本语义一致。

    不分页、按 id 排序返回全部匹配；分页需求用 search_contents_page。
    tags 为多标签 AND 筛选（与 tag 可叠加）。
    """
    return list(db.scalars(_content_search_stmt(keyword, None, tag, creator, tags)))


def search_contents_page(
    db: Session,
    *,
    keyword: str = "",
    content_type: str | None = None,
    tag: str | None = None,
    creator: str | None = None,
    tags: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[Content]]:
    """分页搜索内容：关键词（标题/简介）、类型、标签与作者筛选可叠加，返回 (总数, 当前页)。

    四类条件均可缺省：只给类型即按类型浏览，只给关键词即全类型搜索，
    只给标签即按标签归类浏览（多标签 AND），只给作者即按作者/公司浏览。
    """
    stmt = _content_search_stmt(keyword, content_type, tag, creator, tags)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = list(
        db.scalars(stmt.offset((page - 1) * page_size).limit(page_size))
    )
    return total, items


def seed_if_empty(db: Session) -> int:
    """表为空且开关打开时播种开发/测试数据，返回写入行数；表已有数据则不动（幂等）。

    ``SEED_DEMO_DATA=false`` 时直接返回 0：上线用干净库，真实数据由后台
    「数据同步」灌入（见 README 数据管理一节）。
    """
    if not settings.seed_demo_data:
        return 0
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
    ("avatar_url", "VARCHAR(500)"),
)


def ensure_user_profile_columns(engine: Engine) -> list[str]:
    """为已存在的 users 表补齐个人空间列（简介 bio / 头像 avatar_url），幂等。

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


# 启动时按需补齐的 contents 列（封面图）。项目没有 Alembic，历史库只能幂等
# ALTER；不回填任何值——没有封面的条目继续走渐变占位，由导入/回填脚本落图。
_CONTENT_IMAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("image_url", "VARCHAR(500)"),
)

# 作品发行年（来自 Bangumi date）：标签归一时单独成列，避免年份混进内容标签。
_CONTENT_YEAR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("year", "INTEGER"),
)

# 客观信息列（媒介 / 原作载体 / 话数）与元数据补全时间，同样走幂等 ALTER。
_CONTENT_META_COLUMNS: tuple[tuple[str, str], ...] = (
    ("media", "VARCHAR(20)"),
    ("source_material", "VARCHAR(20)"),
    ("episodes", "INTEGER"),
    ("meta_fetched_at", "DATETIME"),
)


def ensure_test_case_source_column(engine: Engine) -> list[str]:
    """为已存在的 test_cases 表补齐 source 列（manual / auto），幂等。

    用例自动生成需要区分「人工录入」与「自动生成」：前者永不被自动改动，
    后者可被更新期望值或下线。历史库不会自动得到这一列，需在这里补。
    """
    inspector = inspect(engine)
    if "test_cases" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("test_cases")}
    if "source" in existing:
        return []
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE test_cases ADD COLUMN source VARCHAR(10)"))
        # 既有行统一视为人工录入：它们在自动生成功能之前就存在。
        connection.execute(
            text(f"UPDATE test_cases SET source = '{CASE_SOURCE_MANUAL}' WHERE source IS NULL")
        )
    return ["source"]


def ensure_content_image_column(engine: Engine) -> list[str]:
    """为已存在的 contents 表补齐封面列（image_url），幂等，返回新增列名。

    与 ensure_user_profile_columns 同一模式：create_all 只建新表、不给已有表
    加列，封面功能上线前创建的库需要在启动时补列，否则读取 image_url 直接报错。
    """
    inspector = inspect(engine)
    if "contents" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("contents")}
    added: list[str] = []
    for name, ddl in _CONTENT_IMAGE_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE contents ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


def ensure_content_year_column(engine: Engine) -> list[str]:
    """为已存在的 contents 表补齐 year 列，幂等，返回新增列名。

    历史库的行留 NULL，由 scripts/clean_content_tags.py 或下一次同步回填。
    """
    inspector = inspect(engine)
    if "contents" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("contents")}
    added: list[str] = []
    for name, ddl in _CONTENT_YEAR_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE contents ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


# 封面按需下载：低热作品先只记远端地址，被访问时再下载回填 image_url。
_CONTENT_REMOTE_IMAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("remote_image_url", "VARCHAR(500)"),
)


def ensure_content_remote_image_column(engine: Engine) -> list[str]:
    """为已存在的 contents 表补齐 remote_image_url 列，幂等，返回新增列名。

    历史库行留 NULL，表示「没记远端地址」，按需下载直接跳过、不报错。
    """
    inspector = inspect(engine)
    if "contents" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("contents")}
    added: list[str] = []
    for name, ddl in _CONTENT_REMOTE_IMAGE_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE contents ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


# 所属系列（作品关系功能）：同系列的多季 / 剧场版指向 series 表同一行。
_CONTENT_SERIES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("series_id", "INTEGER"),
)


def ensure_content_series_column(engine: Engine) -> list[str]:
    """为已存在的 contents 表补齐 series_id 列，幂等，返回新增列名。

    历史库的行留 NULL（未归系列），由 scripts/link_entities.py 回填。
    """
    inspector = inspect(engine)
    if "contents" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("contents")}
    added: list[str] = []
    for name, ddl in _CONTENT_SERIES_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE contents ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


# 收藏行新增的「名单标志 + 标注」列（历史库只能幂等 ALTER）。
_CONTENT_FAVORITE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("favorited", "BOOLEAN NOT NULL DEFAULT 1"),
    ("status", "VARCHAR(20)"),
    ("status_updated_at", "DATETIME"),
)


def ensure_content_book_kind_column(engine: Engine) -> list[str]:
    """为已存在的 contents 表补齐 book_kind 列，幂等，返回新增列名。

    历史库的行留 NULL（未判定），由 scripts/import_bangumi 新导入时写入，
    存量书籍用 scripts/mark_book_kinds.py 回填。
    """
    inspector = inspect(engine)
    if "contents" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("contents")}
    if "book_kind" in existing:
        return []
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE contents ADD COLUMN book_kind VARCHAR(20)"))
    return ["book_kind"]


def ensure_favorite_status_columns(engine: Engine) -> list[str]:
    """为收藏行补齐 favorited / status / status_updated_at，并把旧标注表搬进来。

    历史库的 content_favorites 只有 (user_id, content_id)，标注单独存在
    content_status 表里。这里补列后一次性搬迁：已有收藏行只补状态列，
    没有收藏行则插入一行（favorited=1——标注过的作品自动成为收藏），
    搬完删掉旧表。旧表不存在（全新库 / 已迁移过）就只补列，天然幂等。
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "content_favorites" not in tables:
        return []

    existing = {column["name"] for column in inspector.get_columns("content_favorites")}
    added: list[str] = []
    for name, ddl in _CONTENT_FAVORITE_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE content_favorites ADD COLUMN {name} {ddl}")
            )
        added.append(name)

    if "content_status" not in tables:
        return added

    with engine.begin() as connection:
        # 1) 只有标注、没有收藏行的作品：整行搬过来（标注过的自动成为收藏）
        connection.execute(
            text(
                "INSERT INTO content_favorites "
                "(user_id, content_id, favorited, status, status_updated_at, created_at) "
                "SELECT cs.user_id, cs.content_id, 1, cs.status, cs.updated_at, cs.updated_at "
                "FROM content_status cs "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM content_favorites cf "
                "WHERE cf.user_id = cs.user_id AND cf.content_id = cs.content_id)"
            )
        )
        # 2) 收藏与标注都有的作品：把标注值补进已有行（收藏时间保留原值）
        connection.execute(
            text(
                "UPDATE content_favorites SET "
                "status = (SELECT cs.status FROM content_status cs "
                "WHERE cs.user_id = content_favorites.user_id "
                "AND cs.content_id = content_favorites.content_id), "
                "status_updated_at = (SELECT cs.updated_at FROM content_status cs "
                "WHERE cs.user_id = content_favorites.user_id "
                "AND cs.content_id = content_favorites.content_id) "
                "WHERE EXISTS (SELECT 1 FROM content_status cs "
                "WHERE cs.user_id = content_favorites.user_id "
                "AND cs.content_id = content_favorites.content_id)"
            )
        )
        connection.execute(text("DROP TABLE content_status"))
    return added


def ensure_content_meta_columns(engine: Engine) -> list[str]:
    """为已存在的 contents 表补齐客观信息列，幂等，返回新增列名。"""
    inspector = inspect(engine)
    if "contents" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("contents")}
    added: list[str] = []
    for name, ddl in _CONTENT_META_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE contents ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


# 画像报告的自定义语气 / 追加提示词列（历史库同样只能幂等 ALTER）。
_PERSONA_REPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tone_prompt", "TEXT"),
    ("extra_prompt", "TEXT"),
)


def ensure_persona_report_columns(engine: Engine) -> list[str]:
    """为已存在的 persona_reports 表补齐自定义语气 / 追加提示词列，幂等。"""
    inspector = inspect(engine)
    if "persona_reports" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("persona_reports")}
    added: list[str] = []
    for name, ddl in _PERSONA_REPORT_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE persona_reports ADD COLUMN {name} {ddl}")
            )
        added.append(name)
    return added


def ensure_article_image_column(engine: Engine) -> list[str]:
    """为已存在的 articles 表补齐头图列（image_url），幂等，返回新增列名。

    资讯图片功能上线前创建的库没有这一列，读取 image_url 会直接报错；
    历史行的 NULL 由 scripts/backfill_article_images.py 回填。
    """
    inspector = inspect(engine)
    if "articles" not in inspector.get_table_names():
        return []

    existing = {column["name"] for column in inspector.get_columns("articles")}
    if "image_url" in existing:
        return []
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE articles ADD COLUMN image_url VARCHAR(500)"))
    return ["image_url"]


# 申请的客观信息列：与 contents 同名列一一对应，让申请能覆盖详情页的结构化信息，
# 而不是只能改标题 / 简介 / 评分四项。
_CONTENT_SUBMISSION_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("year", "INTEGER"),
    ("media", "VARCHAR(20)"),
    ("episodes", "INTEGER"),
    ("source_material", "VARCHAR(20)"),
)


def ensure_content_submission_extra_columns(engine: Engine) -> list[str]:
    """为已存在的 content_submissions 表补齐客观信息列，幂等，返回新增列名。

    历史申请行留 NULL，语义是「当时没填 / 不改动原值」，审核时按此处理。
    """
    inspector = inspect(engine)
    if "content_submissions" not in inspector.get_table_names():
        return []

    existing = {
        column["name"] for column in inspector.get_columns("content_submissions")
    }
    added: list[str] = []
    for name, ddl in _CONTENT_SUBMISSION_EXTRA_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE content_submissions ADD COLUMN {name} {ddl}")
            )
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
    ensure_content_image_column(engine)
    ensure_content_year_column(engine)
    ensure_content_remote_image_column(engine)
    ensure_content_series_column(engine)
    ensure_content_book_kind_column(engine)
    ensure_favorite_status_columns(engine)
    ensure_content_meta_columns(engine)
    ensure_persona_report_columns(engine)
    ensure_test_case_source_column(engine)
    ensure_article_image_column(engine)
    ensure_content_submission_extra_columns(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        seeded = seed_if_empty(db)
        ensure_default_users(db)
        return seeded
    finally:
        db.close()


# ---------- 账号与认证 ----------

# 角色取值（三级，由低到高）：
# - user  普通用户：只能使用内容平台（含个人空间）；
# - admin 管理员：额外可使用测试管理平台（用例 / 执行 / 批次 / 数据管理）；
# - owner 站长：最高级，具备管理员全部能力，并独占角色管理等站点级操作。
ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"
VALID_ROLES = (ROLE_USER, ROLE_ADMIN, ROLE_OWNER)

# 角色展示名：导航徽标、账号列表、个人空间统一使用这一套。
ROLE_LABELS = {
    ROLE_OWNER: "站长",
    ROLE_ADMIN: "管理员",
    ROLE_USER: "普通用户",
}

# 角色等级：数值越大权限越高，用于「至少达到某角色」的判断。
ROLE_RANK = {ROLE_USER: 1, ROLE_ADMIN: 2, ROLE_OWNER: 3}


def role_rank(role: str) -> int:
    """角色等级；未知角色返回 0（最低），不会误判为有权限。"""
    return ROLE_RANK.get(role, 0)


def has_role(role: str, minimum: str) -> bool:
    """role 是否达到 minimum 等级（含同级）；未知角色一律 False。"""
    if minimum not in ROLE_RANK:
        return False
    return role_rank(role) >= role_rank(minimum)


def count_users(db: Session) -> int:
    """账号总数（用于判断是否需要创建初始账号）。"""
    return db.scalar(select(func.count()).select_from(User)) or 0


def list_users(db: Session) -> list[User]:
    """全部账号（按 id 升序），供站长的角色管理页展示。"""
    return list(db.scalars(select(User).order_by(User.id)))


def highest_staff_account(db: Session) -> User | None:
    """权限最高的 staff 账号（站长优先于管理员），没有则返回 None。

    用例执行与自动生成都以这个账号的身份发请求——测试工具是管理员用的，
    匿名身份只能测到一堆 401/303，测不到后台接口本身。
    """
    staff = [user for user in list_users(db) if has_role(user.role, ROLE_ADMIN)]
    if not staff:
        return None
    return max(staff, key=lambda user: ROLE_RANK.get(user.role, 0))


def set_user_role(db: Session, user: User, role: str) -> User:
    """修改账号角色；角色非法时抛 ValueError（由路由层回显）。"""
    if role not in VALID_ROLES:
        raise ValueError(f"角色不合法，可选值：{'、'.join(VALID_ROLES)}")
    if user.role == role:
        return user
    user.role = role
    db.commit()
    db.refresh(user)
    return user


def count_owners(db: Session) -> int:
    """站长账号数量；用于阻止把最后一个站长降级。"""
    return (
        db.scalar(select(func.count()).select_from(User).where(User.role == ROLE_OWNER))
        or 0
    )


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

# 作品标注：登录用户对作品的显式标记，与收藏共处一行（见 models.ContentFavorite）。
# 标注会自动把作品加入收藏；取消收藏不会清掉标注。
CONTENT_STATUS_LABELS = {
    "seen": "看过",
    "unseen": "未看",
    "want": "想看",
}

# 列表筛选用：status=NULL 表示「在名单里但还没标注」，与三个标注值并列成筛选项。
STATUS_FILTER_UNMARKED = "unmarked"

# ACGN 内容类型全集。注意「文学」不是这里的一员——它只是小说条目上的一个
# 标注（contents.book_kind），见 app/page_common.BOOK_KIND_LABELS。
CONTENT_TYPES = ("anime", "comic", "game", "novel")

SUBMISSION_KIND_CREATE = "create"
SUBMISSION_KIND_UPDATE = "update"
SUBMISSION_KINDS = (SUBMISSION_KIND_CREATE, SUBMISSION_KIND_UPDATE)
SUBMISSION_KIND_LABELS = {
    SUBMISSION_KIND_CREATE: "新增作品",
    SUBMISSION_KIND_UPDATE: "修改作品",
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

# 申请可填的客观信息取值：与 app/bangumi_meta.py 的入库口径保持一致，
# 避免用户手填出详情页展示不出来的第三种写法。
SUBMISSION_MEDIA_CHOICES = ("TV", "剧场版", "OVA", "Web", "游戏", "小说", "漫画")
SUBMISSION_SOURCE_MATERIAL_CHOICES = (
    "漫画原作",
    "小说原作",
    "游戏原作",
    "原创",
)
SUBMISSION_YEAR_MIN = 1900
SUBMISSION_YEAR_MAX = 2100
SUBMISSION_EPISODES_MAX = 10000


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
    """收藏作品数（个人空间统计卡）；指定 content_type 时只数该分类。

    只数 favorited 行：取消过收藏但标注还在的作品不算在名单里。
    """
    stmt = (
        select(func.count())
        .select_from(ContentFavorite)
        .where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.favorited.is_(True),
        )
    )
    if content_type is not None:
        stmt = stmt.join(Content, Content.id == ContentFavorite.content_id).where(
            Content.type == content_type
        )
    return db.scalar(stmt) or 0


def _get_favorite_row(
    db: Session, user_id: int, content_id: int
) -> ContentFavorite | None:
    """取用户与作品的关系行（不论是否仍在收藏名单里）。"""
    return db.scalar(
        select(ContentFavorite).where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.content_id == content_id,
        )
    )


def is_favorite(db: Session, user_id: int, content_id: int) -> bool:
    """是否已收藏该作品（详情页按钮状态）。"""
    return (
        db.scalar(
            select(ContentFavorite.id).where(
                ContentFavorite.user_id == user_id,
                ContentFavorite.content_id == content_id,
                ContentFavorite.favorited.is_(True),
            )
        )
        is not None
    )


def add_favorite(db: Session, user_id: int, content_id: int) -> bool:
    """收藏作品；已收藏时返回 False（幂等，不产生重复行）。

    取消过收藏但标注还在的作品直接复用原行：把 favorited 翻回 True，
    标注原样恢复可见。
    """
    row = _get_favorite_row(db, user_id, content_id)
    if row is None:
        db.add(
            ContentFavorite(user_id=user_id, content_id=content_id, favorited=True)
        )
        db.commit()
        return True
    if row.favorited:
        return False
    row.favorited = True
    db.commit()
    return True


def remove_favorite(db: Session, user_id: int, content_id: int) -> bool:
    """取消收藏；返回是否真的取消了（幂等）。

    **不动标注**：只把 favorited 置 False。收藏了但没看是名单里的常态，
    看过 / 想看是耐久事实，移出名单不该把它抹掉；行里两个字段都空了才删行。
    """
    row = _get_favorite_row(db, user_id, content_id)
    if row is None or not row.favorited:
        return False
    row.favorited = False
    if row.status is None:
        db.delete(row)
    db.commit()
    return True


def clear_favorites(
    db: Session, user_id: int, content_type: str | None = None
) -> int:
    """清空收藏，返回取消收藏的条数；指定 content_type 时只清空该分类。

    语义与单条取消一致：标注保留（只是不再出现在名单里），空壳行顺手删掉。
    """
    stmt = update(ContentFavorite).where(
        ContentFavorite.user_id == user_id,
        ContentFavorite.favorited.is_(True),
    )
    if content_type is not None:
        stmt = stmt.where(
            ContentFavorite.content_id.in_(
                select(Content.id).where(Content.type == content_type)
            )
        )
    result = db.execute(stmt.values(favorited=False))
    db.execute(
        sa_delete(ContentFavorite).where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.favorited.is_(False),
            ContentFavorite.status.is_(None),
        )
    )
    db.commit()
    return result.rowcount or 0


def list_favorites(
    db: Session,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    content_type: str | None = None,
    status: str | None = None,
) -> tuple[int, list[Content]]:
    """收藏作品分页（按收藏时间倒序）：内连接 contents，作品已删则不展示。

    - content_type 非空时按内容类型筛选（追番 / 漫画 / 游戏 / 轻小说）；
    - status 非空时按标注筛选；传 ``STATUS_FILTER_UNMARKED`` 筛「未标注」。
    """
    base = (
        select(Content)
        .join(ContentFavorite, ContentFavorite.content_id == Content.id)
        .where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.favorited.is_(True),
        )
    )
    if content_type is not None:
        base = base.where(Content.type == content_type)
    if status == STATUS_FILTER_UNMARKED:
        base = base.where(ContentFavorite.status.is_(None))
    elif status is not None:
        base = base.where(ContentFavorite.status == status)
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

    只统计仍能在 contents 中找到、且仍在名单里的作品（与列表语义一致）。
    """
    rows = db.execute(
        select(Content.type, func.count())
        .select_from(ContentFavorite)
        .join(Content, Content.id == ContentFavorite.content_id)
        .where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.favorited.is_(True),
        )
        .group_by(Content.type)
    ).all()
    return {content_type: count for content_type, count in rows}


def get_content_status(db: Session, user_id: int, content_id: int) -> str | None:
    """取用户对某作品的标注；未标注返回 None。

    不看 favorited：取消收藏后标注仍然有效（详情页照样显示已标注）。
    """
    return db.scalar(
        select(ContentFavorite.status).where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.content_id == content_id,
        )
    )


def get_content_statuses(
    db: Session, user_id: int, content_ids: list[int]
) -> dict[int, str]:
    """批量取多部作品的标注，供收藏列表展示徽标。"""
    if not content_ids:
        return {}
    rows = db.execute(
        select(ContentFavorite.content_id, ContentFavorite.status).where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.content_id.in_(content_ids),
            ContentFavorite.status.is_not(None),
        )
    ).all()
    return {content_id: status for content_id, status in rows}


def set_content_status(
    db: Session, user_id: int, content_id: int, status: str
) -> str:
    """写作品标注（看过 / 未看 / 想看），同一作品只保留一条，**并自动收藏**。

    没有关系行就建一行（favorited=True）；有行就更新标注并把 favorited 置回 True
    ——标注过的作品天然是名单成员。返回写入的标注值；非法值抛 ValueError。
    """
    if status not in CONTENT_STATUS_LABELS:
        raise ValueError(f"不支持的标注：{status}")
    row = _get_favorite_row(db, user_id, content_id)
    now = datetime.now()
    if row is None:
        db.add(
            ContentFavorite(
                user_id=user_id,
                content_id=content_id,
                favorited=True,
                status=status,
                status_updated_at=now,
            )
        )
    else:
        row.status = status
        row.status_updated_at = now
        row.favorited = True
    db.commit()
    return status


def clear_content_status(db: Session, user_id: int, content_id: int) -> bool:
    """清除标注：收藏不受影响，返回是否真的清掉了标注（幂等）。

    行里两个字段都空了（本来就没收藏）才删行，不留空壳。
    """
    row = _get_favorite_row(db, user_id, content_id)
    if row is None or row.status is None:
        return False
    row.status = None
    row.status_updated_at = None
    if not row.favorited:
        db.delete(row)
    db.commit()
    return True


def list_content_status(
    db: Session,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
) -> tuple[int, list[tuple[Content, str, datetime]]]:
    """有标注的作品分页（按标注时间倒序），供御宅成分检测等场景使用。

    返回 (总数, [(内容, 标注, 标注时间)])；不论是否仍在收藏名单里。
    """
    base = (
        select(Content, ContentFavorite.status, ContentFavorite.status_updated_at)
        .join(ContentFavorite, ContentFavorite.content_id == Content.id)
        .where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.status.is_not(None),
        )
    )
    if status is not None:
        base = base.where(ContentFavorite.status == status)
    total = (
        db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0
    )
    rows = db.execute(
        base.order_by(ContentFavorite.status_updated_at.desc(), Content.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return total, [(row[0], row[1], row[2]) for row in rows]


def count_content_status(
    db: Session, user_id: int, status: str | None = None
) -> int:
    """有标注的作品数（不论是否仍在收藏名单里）；指定 status 时只数该标注。"""
    stmt = (
        select(func.count())
        .select_from(ContentFavorite)
        .join(Content, Content.id == ContentFavorite.content_id)
        .where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.status.is_not(None),
        )
    )
    if status is not None:
        stmt = stmt.where(ContentFavorite.status == status)
    return db.scalar(stmt) or 0


def content_status_counts(db: Session, user_id: int) -> dict[str, int]:
    """按标注值统计「收藏名单内」的条数（看过 / 未看 / 想看），供统计卡与筛选条。

    只数 favorited 行：取消收藏后标注虽然保留，但不在列表里，
    计数跟着列表走，避免数字对不上。
    """
    rows = db.execute(
        select(ContentFavorite.status, func.count())
        .select_from(ContentFavorite)
        .join(Content, Content.id == ContentFavorite.content_id)
        .where(
            ContentFavorite.user_id == user_id,
            ContentFavorite.favorited.is_(True),
            ContentFavorite.status.is_not(None),
        )
        .group_by(ContentFavorite.status)
    ).all()
    return {status: count for status, count in rows}


# ---------- 作品申请 ----------


def _normalize_content_fields(
    title: str,
    content_type: str,
    summary: str,
    *,
    year=None,
    media=None,
    episodes=None,
    source_material=None,
) -> tuple[dict | None, str | None]:
    """校验并规范化申请字段；返回 (规范化字段, 错误信息)。

    评分不走这个流程：contents.rating 是外部数据源（Bangumi 等）聚合出来的
    统计数据，只能由导入 / 补全链路写入，申请表单不接收用户自填评分，
    否则等于允许用户随手编一个分数并通过审核落进内容库。

    客观信息（年份 / 媒介 / 话数 / 原作载体）全部可选：留空返回 None，
    语义是「这条不改动原值」，因此只改简介的申请不会顺手把年份清空。
    """
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

    year_text = str(year or "").strip()
    year_value = None
    if year_text:
        if not year_text.isdigit():
            return None, "年份必须是数字"
        year_value = int(year_text)
        if not SUBMISSION_YEAR_MIN <= year_value <= SUBMISSION_YEAR_MAX:
            return None, f"年份需在 {SUBMISSION_YEAR_MIN} 到 {SUBMISSION_YEAR_MAX} 之间"

    episodes_text = str(episodes or "").strip()
    episodes_value = None
    if episodes_text:
        if not episodes_text.isdigit():
            return None, "话数必须是数字"
        episodes_value = int(episodes_text)
        if not 1 <= episodes_value <= SUBMISSION_EPISODES_MAX:
            return None, f"话数需在 1 到 {SUBMISSION_EPISODES_MAX} 之间"

    media_value = (str(media or "").strip() or None)
    if media_value and media_value not in SUBMISSION_MEDIA_CHOICES:
        return None, f"媒介只能是：{' / '.join(SUBMISSION_MEDIA_CHOICES)}"

    source_value = (str(source_material or "").strip() or None)
    if source_value and source_value not in SUBMISSION_SOURCE_MATERIAL_CHOICES:
        return None, f"原作载体只能是：{' / '.join(SUBMISSION_SOURCE_MATERIAL_CHOICES)}"

    return {
        "title": title,
        "content_type": content_type,
        "summary": summary,
        "year": year_value,
        "media": media_value,
        "episodes": episodes_value,
        "source_material": source_value,
    }, None


def find_content_by_title(db: Session, title: str) -> Content | None:
    """按标题精确查找作品（忽略大小写与首尾空白），供新增申请查重。

    用于拦住「库里明明有，用户又提一条新增」的重复条目——重复数据一旦
    通过审核就会分裂出两个详情页，事后合并比事前拦住麻烦得多。
    """
    keyword = (title or "").strip()
    if not keyword:
        return None
    return db.scalar(select(Content).where(func.lower(Content.title) == keyword.lower()))


def submit_content(
    db: Session,
    user_id: int,
    kind: str,
    title: str,
    content_type: str,
    summary: str,
    content_id: int | None = None,
    *,
    year=None,
    media=None,
    episodes=None,
    source_material=None,
) -> ContentSubmission:
    """创建一条待审核的申请（新增 / 修改），返回申请对象。

    kind / 字段不合法、目标作品不存在、或新增撞上同名作品时抛 ValueError，
    由路由层回显。

    评分不在申请范围内：row.rating 只存「提交时目标作品的当前评分」快照，
    供审核页展示原值；新增申请存 0.0（暂无评分）。审核通过时不会用它
    覆盖 contents.rating——评分由外部数据源链路维护。
    """
    if kind not in SUBMISSION_KINDS:
        raise ValueError("申请类型不合法")
    fields, error = _normalize_content_fields(
        title,
        content_type,
        summary,
        year=year,
        media=media,
        episodes=episodes,
        source_material=source_material,
    )
    if error:
        raise ValueError(error)
    target_id = None
    rating_snapshot = 0.0
    if kind == SUBMISSION_KIND_UPDATE:
        if content_id is None:
            raise ValueError("缺少要修改的作品")
        target = get_content(db, content_id)
        if target is None:
            raise ValueError("要修改的作品不存在")
        target_id = content_id
        rating_snapshot = float(target.rating or 0)
    else:
        duplicate = find_content_by_title(db, fields["title"])
        if duplicate is not None:
            raise ValueError(
                f"作品库已有同名作品「{duplicate.title}」，"
                f"请直接到该作品页申请修改（/contents/{duplicate.id}/edit）"
            )
    row = ContentSubmission(
        user_id=user_id,
        kind=kind,
        content_id=target_id,
        status=SUBMISSION_STATUS_PENDING,
        rating=rating_snapshot,
        **fields,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_usernames_by_ids(db: Session, user_ids: list[int]) -> dict[int, str]:
    """批量取账号名，供审核页显示「谁提的」而不是「用户 #3」。"""
    ids = [int(uid) for uid in user_ids if uid]
    if not ids:
        return {}
    rows = db.execute(
        select(User.id, User.username).where(User.id.in_(ids))
    ).all()
    return {row[0]: row[1] for row in rows}


# 修改申请的新旧对比字段：(申请列名, 作品列名, 展示名)。
# 评分不参与对比：它由外部数据源维护，申请流程不产生变化。
SUBMISSION_DIFF_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("title", "title", "标题"),
    ("content_type", "type", "类型"),
    ("summary", "summary", "简介"),
    ("year", "year", "年份"),
    ("media", "media", "媒介"),
    ("episodes", "episodes", "话数"),
    ("source_material", "source_material", "原作载体"),
)

# 这四个留空表示「不改动原值」，与必填的四项区别对待。
SUBMISSION_OPTIONAL_FIELDS = ("year", "media", "episodes", "source_material")

# 对比里长文本的截断长度：审核页是列表，简介全文铺开会把整行撑爆。
SUBMISSION_DIFF_TEXT_LIMIT = 80


def _diff_text(value, limit: int = SUBMISSION_DIFF_TEXT_LIMIT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def submission_diff(
    row: ContentSubmission,
    content: Content | None,
    *,
    type_labels: dict | None = None,
) -> list[dict]:
    """修改申请的新旧对比：返回 [{label, old, new, changed, kept}]。

    审核者不该被要求自己打开作品页逐字比对——这里把差异直接算出来：
    - changed：新旧不一致；
    - kept：客观信息留空，实际效果是保持原值（不是「改成空」）。
    """
    if content is None:
        return []
    labels = type_labels or {}
    result: list[dict] = []
    for submission_column, content_column, label in SUBMISSION_DIFF_FIELDS:
        new_value = getattr(row, submission_column)
        old_value = getattr(content, content_column)
        kept = (
            new_value is None
            and submission_column in SUBMISSION_OPTIONAL_FIELDS
        )
        if submission_column == "content_type":
            old_text = labels.get(old_value, old_value) if old_value else ""
            new_text = labels.get(new_value, new_value) if new_value else ""
        elif submission_column == "rating":
            old_text = f"{old_value:.1f}" if old_value is not None else ""
            new_text = f"{new_value:.1f}" if new_value is not None else ""
        elif submission_column == "summary":
            old_text = _diff_text(old_value)
            new_text = _diff_text(new_value)
        else:
            old_text = "" if old_value is None else str(old_value)
            new_text = "" if new_value is None else str(new_value)
        if kept:
            new_text = old_text
        result.append(
            {
                "label": label,
                "old": old_text,
                "new": new_text,
                "changed": old_text != new_text,
                "kept": kept,
            }
        )
    return result


def get_content_submission(
    db: Session, submission_id: int
) -> ContentSubmission | None:
    """按主键取作品申请，不存在返回 None。"""
    return db.get(ContentSubmission, submission_id)


def list_content_submissions(
    db: Session,
    *,
    user_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[ContentSubmission]]:
    """作品申请分页（按提交时间倒序）；可按提交人与状态筛选。"""
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
    """作品申请条数；可按提交人与状态筛选。"""
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
    """审核作品申请：approve=True 写入 / 更新 contents，False 驳回。

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
            # 新增作品没有可引用评分：先落 0.0（页面显示「暂无评分」），
            # 真实评分等 Bangumi 补全 / 导入链路写入。
            content = Content(
                title=row.title,
                type=row.content_type,
                summary=row.summary,
                rating=0.0,
                year=row.year,
                media=row.media,
                episodes=row.episodes,
                source_material=row.source_material,
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
            # 评分不在此写入：contents.rating 只由外部数据源（Bangumi 等）维护，
            # 用户提交的修改申请无权改动它。row.rating 只是提交时的原值快照。
            # 客观信息留空 = 不改动原值：只改简介的申请不该顺手清空年份。
            for column in ("year", "media", "episodes", "source_material"):
                value = getattr(row, column)
                if value is not None:
                    setattr(content, column, value)
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


# ---------- 御宅成分检测：Bangumi 收藏快照 / 报告 ----------


def replace_bangumi_collections(
    db: Session, user_id: int, items: list[dict]
) -> int:
    """用本次同步结果整体替换某账号的 Bangumi 收藏快照，返回写入条数。"""
    db.execute(
        sa_delete(BangumiCollection).where(BangumiCollection.user_id == user_id)
    )
    rows = [BangumiCollection(user_id=user_id, **item) for item in items]
    db.add_all(rows)
    db.commit()
    return len(rows)


def list_bangumi_collections(
    db: Session, user_id: int
) -> list[BangumiCollection]:
    """某账号的全部 Bangumi 收藏快照（按作品 id 升序，便于稳定分析）。"""
    return list(
        db.scalars(
            select(BangumiCollection)
            .where(BangumiCollection.user_id == user_id)
            .order_by(BangumiCollection.subject_id)
        )
    )


def count_bangumi_collections(db: Session, user_id: int) -> int:
    """某账号已同步的 Bangumi 收藏条数。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(BangumiCollection)
            .where(BangumiCollection.user_id == user_id)
        )
        or 0
    )


def clear_bangumi_collections(db: Session, user_id: int) -> int:
    """清空某账号的 Bangumi 收藏快照，返回删除行数。"""
    result = db.execute(
        sa_delete(BangumiCollection).where(BangumiCollection.user_id == user_id)
    )
    db.commit()
    return result.rowcount or 0


def get_persona_report(db: Session, user_id: int) -> PersonaReport | None:
    """取某账号的画像报告（每账号一份）。"""
    return db.scalar(select(PersonaReport).where(PersonaReport.user_id == user_id))


# 单次「同步到本站」最多新建多少部作品：收藏动辄上千条，一次点击不该无上限写库。
PERSONA_SITE_SYNC_LIMIT = 200

# Bangumi 收藏状态 → 站内标注：只在「看过 / 在看 / 想看」时补站内收藏与标注，
# 抛弃 / 搁置只补作品条目，不往个人空间塞负向记录。
SITE_SYNC_MARK_STATUS = {"seen": "seen", "want": "want"}

# SQLite 的参数上限较低（老版本 999），批量 in_ 查询按块拆开。
_ID_CHUNK = 400


def count_contents_by_bangumi(db: Session, bangumi_ids: list[int]) -> int:
    """给定 Bangumi 条目里有多少已在本站作品库（按 bangumi_id 精确匹配）。"""
    ids = [int(item) for item in bangumi_ids if item]
    found = 0
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        found += (
            db.scalar(
                select(func.count())
                .select_from(Content)
                .where(Content.bangumi_id.in_(chunk))
            )
            or 0
        )
    return found


def _bangumi_work_summary(work: dict) -> str:
    """为收藏同步进来的作品拼一段可读简介（快照里没有原始 summary）。"""
    parts = []
    year = work.get("year")
    type_label = str(work.get("type_label") or "")
    if year and type_label:
        parts.append(f"{year} 年的{type_label}")
    elif type_label:
        parts.append(type_label)
    score = work.get("score")
    if score:
        parts.append(f"Bangumi 评分 {float(score):.1f}")
    header = "，".join(parts)
    # 简介里的标签也只列题材，避免把年份/公司/声优写进正文
    tags = split_tags([str(tag) for tag in (work.get("tags") or [])])[0][:5]
    if tags:
        header += ("，标签：" if header else "标签：") + " / ".join(tags)
    note = f"由 Bangumi 收藏同步（bgm.tv/subject/{work.get('subject_id')}）"
    return f"{header}。{note}。" if header else f"{note}。"


def import_works_to_site(
    db: Session,
    user_id: int,
    works: list[dict],
    *,
    limit: int = PERSONA_SITE_SYNC_LIMIT,
) -> dict:
    """把 Bangumi 收藏里的作品补进站内作品库，并镜像收藏 / 标注。

    约定：
    - 以 ``bangumi_id`` 去重：站内已有只做关联，**不覆盖**人工维护的标题 / 简介 / 标签；
    - 新作品用收藏快照里的元数据生成简介，封面留空（页面走渐变占位）；
    - 只对「看过 / 在看 / 想看」补站内收藏与标注；已有手动标注不覆盖；
    - 单次最多新建 ``limit`` 部，返回统计供页面回显（``limited`` 表示还有剩余）。
    """
    stats = {
        "new": 0,
        "linked": 0,
        "skipped": 0,
        "favorited": 0,
        "marked": 0,
        "limited": False,
    }

    prepared: list[dict] = []
    seen: set[int] = set()
    for work in works:
        subject_id = work.get("subject_id")
        title = str(work.get("title") or "").strip()
        try:
            subject_id = int(subject_id)
        except (TypeError, ValueError):
            stats["skipped"] += 1
            continue
        if subject_id <= 0 or not title or subject_id in seen:
            stats["skipped"] += 1
            continue
        seen.add(subject_id)
        prepared.append(work)

    content_ids: dict[int, int] = {}
    ids = [int(work["subject_id"]) for work in prepared]
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        for bangumi_id, content_id in db.execute(
            select(Content.bangumi_id, Content.id).where(Content.bangumi_id.in_(chunk))
        ):
            if bangumi_id is not None:
                content_ids[int(bangumi_id)] = int(content_id)

    stats["linked"] = sum(
        1 for work in prepared if int(work["subject_id"]) in content_ids
    )

    pending = [
        work for work in prepared if int(work["subject_id"]) not in content_ids
    ]
    if limit > 0 and len(pending) > limit:
        pending = pending[:limit]
        stats["limited"] = True

    for work in pending:
        subject_id = int(work["subject_id"])
        raw_tags = [str(tag) for tag in (work.get("tags") or []) if str(tag).strip()]
        genres, studios, tag_year = split_tags(raw_tags)
        work_year = work.get("year") or tag_year
        content = Content(
            title=str(work["title"]).strip()[:200],
            type=str(work.get("type") or "anime"),
            summary=_bangumi_work_summary(work),
            rating=float(work.get("score") or work.get("rate") or 0),
            bangumi_id=subject_id,
            source_url=f"https://bgm.tv/subject/{subject_id}",
            year=int(work_year) if work_year else None,
        )
        # 并发兜底：两个请求同时插入同一 bangumi_id 会撞唯一索引，
        # 用 SAVEPOINT 只回滚这一条，再按「已在站内」处理，不让整批 500。
        try:
            with db.begin_nested():
                db.add(content)
                db.flush()  # 先拿 id，标签关联与收藏引用都要用
        except IntegrityError:
            try:
                db.expunge(content)
            except Exception:  # noqa: BLE001 - 对象可能已不在会话里
                pass
            existing_id = db.scalar(
                select(Content.id).where(Content.bangumi_id == subject_id)
            )
            if existing_id is not None:
                content_ids[subject_id] = int(existing_id)
                stats["linked"] += 1
            else:
                stats["skipped"] += 1
            continue

        # 标签只入题材；公司单独成表，年份已在 Content.year（见 app/tagging.py）
        if genres:
            replace_content_tags(db, content.id, genres)
        if studios:
            replace_content_studios(db, content.id, studios)
        db.add(UserSiteImport(user_id=user_id, content_id=content.id))
        content_ids[subject_id] = content.id
        stats["new"] += 1

    targets = []
    for work in prepared:
        content_id = content_ids.get(int(work["subject_id"]))
        site_status = SITE_SYNC_MARK_STATUS.get(str(work.get("status") or ""))
        if content_id and site_status:
            targets.append((content_id, site_status))

    if targets:
        # 同一作品只保留一条关系行：先按作品去重，再批量取已有行（避免 N+1）
        planned: dict[int, str] = {}
        for content_id, site_status in targets:
            planned.setdefault(content_id, site_status)
        target_ids = list(planned)
        rows: dict[int, ContentFavorite] = {}
        for start in range(0, len(target_ids), _ID_CHUNK):
            chunk = target_ids[start : start + _ID_CHUNK]
            for row in db.scalars(
                select(ContentFavorite).where(
                    ContentFavorite.user_id == user_id,
                    ContentFavorite.content_id.in_(chunk),
                )
            ):
                rows[int(row.content_id)] = row

        for content_id, site_status in planned.items():
            row = rows.get(content_id)
            now = datetime.now()
            if row is None:
                # 收藏与标注是同一行：标注过的作品天然进入名单
                db.add(
                    ContentFavorite(
                        user_id=user_id,
                        content_id=content_id,
                        favorited=True,
                        status=site_status,
                        status_updated_at=now,
                    )
                )
                stats["favorited"] += 1
                stats["marked"] += 1
                continue
            if not row.favorited:
                row.favorited = True
                stats["favorited"] += 1
            # 已有手动标注不覆盖（与「不覆盖人工维护数据」的约定一致）
            if row.status is None:
                row.status = site_status
                row.status_updated_at = now
                stats["marked"] += 1

    db.commit()
    return stats


def count_user_site_imports(db: Session, user_id: int) -> int:
    """某账号通过「同步到本站」新建了多少部作品。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(UserSiteImport)
            .where(UserSiteImport.user_id == user_id)
        )
        or 0
    )


def list_user_site_imports(
    db: Session, user_id: int, limit: int = 10
) -> list[tuple[Content, datetime]]:
    """我导入的作品（按导入时间倒序）：内连接 contents，已删的不展示。"""
    rows = db.execute(
        select(Content, UserSiteImport.created_at)
        .join(UserSiteImport, UserSiteImport.content_id == Content.id)
        .where(UserSiteImport.user_id == user_id)
        .order_by(UserSiteImport.created_at.desc(), UserSiteImport.id.desc())
        .limit(limit)
    ).all()
    return [(content, created_at) for content, created_at in rows]


def undo_site_imports(db: Session, user_id: int) -> dict:
    """撤销本账号导入的作品：没被别人使用的真删，被使用的保留并计数。

    「被使用」的判定：有其它账号的收藏 / 标注 / 浏览记录，或其它账号也导入过。
    删除时连同标签关联与自己的收藏 / 标注 / 浏览记录一起清掉，不留孤儿行。
    """
    rows = list(
        db.scalars(select(UserSiteImport).where(UserSiteImport.user_id == user_id))
    )
    stats = {"removed": 0, "kept": 0}
    for row in rows:
        content_id = int(row.content_id)
        content = db.get(Content, content_id)
        if content is None:
            db.delete(row)
            continue

        used_by_others = (
            db.scalar(
                select(func.count())
                .select_from(ContentFavorite)
                .where(
                    ContentFavorite.content_id == content_id,
                    ContentFavorite.user_id != user_id,
                )
            )
            or db.scalar(
                select(func.count())
                .select_from(ContentViewHistory)
                .where(
                    ContentViewHistory.content_id == content_id,
                    ContentViewHistory.user_id != user_id,
                )
            )
            or db.scalar(
                select(func.count())
                .select_from(UserSiteImport)
                .where(
                    UserSiteImport.content_id == content_id,
                    UserSiteImport.user_id != user_id,
                )
            )
        )
        if used_by_others:
            stats["kept"] += 1
            continue

        db.execute(sa_delete(ContentTag).where(ContentTag.content_id == content_id))
        db.execute(
            sa_delete(ContentFavorite).where(ContentFavorite.content_id == content_id)
        )
        db.execute(
            sa_delete(ContentViewHistory).where(
                ContentViewHistory.content_id == content_id
            )
        )
        db.delete(content)
        db.delete(row)
        stats["removed"] += 1

    db.commit()
    return stats


def set_persona_username(
    db: Session, user_id: int, username: str
) -> PersonaReport:
    """记录账号绑定的 Bangumi 用户名（同步时调用；无报告则先建空壳）。"""
    row = get_persona_report(db, user_id)
    if row is None:
        row = PersonaReport(user_id=user_id, bangumi_username=username, payload={})
        db.add(row)
    else:
        row.bangumi_username = username
    db.commit()
    db.refresh(row)
    return row


def save_persona_report(
    db: Session,
    user_id: int,
    *,
    bangumi_username: str | None,
    tone: str,
    payload: dict,
    narrative: str | None = None,
    source: str = "template",
    tone_prompt: str | None = None,
    extra_prompt: str | None = None,
) -> PersonaReport:
    """新建或覆盖某账号的画像报告（重新生成即覆盖）。"""
    row = get_persona_report(db, user_id)
    if row is None:
        row = PersonaReport(user_id=user_id)
        db.add(row)
    row.bangumi_username = bangumi_username
    row.tone = tone
    row.payload = payload
    row.narrative = narrative
    row.narrative_source = source
    row.tone_prompt = tone_prompt
    row.extra_prompt = extra_prompt
    row.generated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row


def set_persona_narrative(
    db: Session, user_id: int, text: str, source: str = "manual"
) -> PersonaReport | None:
    """保存用户贴回的 LLM 文案（没有报告时返回 None）。"""
    row = get_persona_report(db, user_id)
    if row is None:
        return None
    row.narrative = (text or "").strip() or None
    row.narrative_source = source
    db.commit()
    db.refresh(row)
    return row


def clear_persona(db: Session, user_id: int) -> None:
    """清除某账号的收藏快照与画像报告（隐私：一键删除）。"""
    db.execute(
        sa_delete(BangumiCollection).where(BangumiCollection.user_id == user_id)
    )
    db.execute(sa_delete(PersonaReport).where(PersonaReport.user_id == user_id))
    db.commit()


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


# ---------- 注册邀请码（公开注册关闭后的准入控制） ----------
#
# 数据模型借鉴 LunaTV 邀请码系统（过期时间 / 使用痕迹 / 追溯），按本站
# 「单表 + 弱关联」风格落地：code 明文存储（管理员要在列表页分发），
# 单次有效，created_by 不设外键。字符集去掉 0/O/1/I 等易混淆字符，
# 避免邀请码口头转抄时出错。

# 邀请码字符集：去掉易混淆字符（0/O、1/I），8 位，空间 32^8 足够。
INVITE_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
INVITE_CODE_LENGTH = 8
# 单次批量生成上限（管理页表单与 CLI 共用，防止一次生成太多不好分发）。
INVITE_CODE_MAX_BATCH = 20

INVITE_STATUS_AVAILABLE = "available"
INVITE_STATUS_USED = "used"
INVITE_STATUS_EXPIRED = "expired"
INVITE_STATUS_REVOKED = "revoked"
INVITE_STATUSES = (
    INVITE_STATUS_AVAILABLE,
    INVITE_STATUS_USED,
    INVITE_STATUS_EXPIRED,
    INVITE_STATUS_REVOKED,
)
INVITE_STATUS_LABELS = {
    INVITE_STATUS_AVAILABLE: "可用",
    INVITE_STATUS_USED: "已使用",
    INVITE_STATUS_EXPIRED: "已过期",
    INVITE_STATUS_REVOKED: "已作废",
}


def invite_code_status(row: InviteCode) -> str:
    """派生展示状态：已作废 > 已使用 > 已过期 > 可用（互斥，优先级递减）。"""
    if row.revoked_at is not None:
        return INVITE_STATUS_REVOKED
    if row.used_at is not None:
        return INVITE_STATUS_USED
    if row.expires_at is not None and row.expires_at < datetime.now():
        return INVITE_STATUS_EXPIRED
    return INVITE_STATUS_AVAILABLE


def create_invite_codes(
    db: Session,
    *,
    count: int = 1,
    expires_at: datetime | None = None,
    note: str = "",
    created_by: int | None = None,
) -> list[InviteCode]:
    """批量生成邀请码，返回明文码行列表（新的在前由调用方决定排序展示）。

    逐个提交 + 唯一索引冲突重试：单个码碰撞只影响该码，不影响同批已生成的；
    数量超限抛 ValueError，由路由层 / CLI 回显。
    """
    if count < 1 or count > INVITE_CODE_MAX_BATCH:
        raise ValueError(f"单次生成数量需在 1-{INVITE_CODE_MAX_BATCH} 之间")
    cleaned_note = (note or "").strip() or None
    rows: list[InviteCode] = []
    for _ in range(count):
        for _attempt in range(5):
            code = "".join(
                secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH)
            )
            row = InviteCode(
                code=code,
                note=cleaned_note,
                expires_at=expires_at,
                created_by=created_by,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:  # 碰撞：换一个码重试（不影响已提交的行）
                db.rollback()
                continue
            db.refresh(row)
            rows.append(row)
            break
        else:
            raise RuntimeError("邀请码生成多次碰撞，请重试")
    return rows


def get_invite_code(db: Session, code: str) -> InviteCode | None:
    """按明文码查询；空串 / 不存在返回 None。"""
    text_code = (code or "").strip()
    if not text_code:
        return None
    return db.scalar(select(InviteCode).where(InviteCode.code == text_code))


def list_invite_codes(db: Session, *, limit: int = 200) -> list[InviteCode]:
    """全部邀请码（新的在前），供管理页与 CLI 展示。"""
    return list(
        db.scalars(select(InviteCode).order_by(InviteCode.id.desc()).limit(limit))
    )


def check_invite_code(db: Session, code: str) -> tuple[InviteCode | None, str]:
    """校验邀请码是否可用；返回 ``(码行, 错误信息)``，错误信息为空串表示可用。

    与 consume_invite_code 分开两步：先做便宜的存在性 / 状态校验，把准确原因
    回显给注册者；真正消费时再用条件 UPDATE 兜并发（见 consume_invite_code）。
    """
    row = get_invite_code(db, code)
    if row is None:
        return None, "邀请码不存在，请向管理员索取有效邀请码"
    status = invite_code_status(row)
    if status == INVITE_STATUS_USED:
        return None, "邀请码已被使用"
    if status == INVITE_STATUS_EXPIRED:
        return None, "邀请码已过期"
    if status == INVITE_STATUS_REVOKED:
        return None, "邀请码已被管理员作废"
    return row, ""


def consume_invite_code(db: Session, code: str, user_id: int) -> tuple[bool, str]:
    """消费邀请码（落使用者），返回 ``(是否成功, 错误信息)``。

    条件 UPDATE（``WHERE used_at IS NULL``）兜住并发：两个请求同时提交同一个
    码时只有一个能改成功；失败方由注册流程删除刚建的账号并回显原因。
    """
    row = get_invite_code(db, code)
    if row is None:
        return False, "邀请码不存在，请向管理员索取有效邀请码"
    result = db.execute(
        update(InviteCode)
        .where(InviteCode.id == row.id, InviteCode.used_at.is_(None))
        .values(used_by=user_id, used_at=datetime.now())
    )
    db.commit()
    if not result.rowcount:
        return False, "邀请码已被使用"
    return True, ""


def revoke_invite_code(db: Session, invite_id: int) -> tuple[bool, str]:
    """作废邀请码（仅未使用的可作废）；返回 ``(是否成功, 错误信息)``。"""
    row = db.get(InviteCode, invite_id)
    if row is None:
        return False, "邀请码不存在"
    if row.used_at is not None:
        return False, "邀请码已被使用，无需作废"
    if row.revoked_at is not None:
        return False, "邀请码已作废"
    row.revoked_at = datetime.now()
    db.commit()
    return True, ""


def delete_user(db: Session, user: User) -> None:
    """删除账号（单表按主键，不做级联）。

    目前唯一调用方是注册流程的并发兜底：邀请码条件消费失败时，把刚创建的
    账号删掉，保证「一个邀请码只换一个账号」。
    """
    db.delete(user)
    db.commit()


# ---------- 测试用例管理 ----------


def list_cases(db: Session) -> list[TestCase]:
    """全部用例，按 id 排序。"""
    return list(db.scalars(select(TestCase).order_by(TestCase.id)))


def get_case(db: Session, case_id: int) -> TestCase | None:
    """按主键查询用例，不存在返回 None（由路由层转 404）。"""
    return db.get(TestCase, case_id)


def find_case(db: Session, method: str, path: str) -> TestCase | None:
    """按「方法 + 路径」查用例（自动生成用来判重，同路径不同方法是两条用例）。"""
    return db.scalars(
        select(TestCase).where(TestCase.method == method.upper(), TestCase.path == path)
    ).first()


def set_case_enabled(db: Session, case: TestCase, enabled: bool) -> TestCase:
    """启停用例：接口下线时只停用，不删除（历史执行记录靠 case_id 关联）。"""
    case.enabled = enabled
    db.commit()
    db.refresh(case)
    return case


def create_case(db: Session, data: TestCaseCreate, source: str = CASE_SOURCE_MANUAL) -> TestCase:
    """新建用例；字段已由 TestCaseCreate 完成校验。source 默认人工录入。"""
    case = TestCase(
        name=data.name,
        method=data.method,
        path=data.path,
        expected_status=data.expected_status,
        enabled=data.enabled,
        source=source,
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


# 作品被删除时要一起清理的弱关联表（都没有外键级联，见各模型注释）。
# 刻意不含 content_submissions：词条申请是独立生命周期的审核记录，
# 它的 content_id 是「通过后写入的作品」，删作品不该吞掉审核历史。
_CONTENT_WEAK_ASSOCIATIONS = (
    ContentTag,
    ContentStudio,
    ContentFact,
    ContentCast,
    ContentCredit,
    ContentEntityLink,
    ContentFavorite,
    ContentViewHistory,
    UserSiteImport,
    ArticleContentLink,
)


def delete_contents(db: Session, ids) -> int:
    """删除作品条目及其弱关联（数据管理 → 作品数据）。返回删除行数。

    弱关联（标签 / 公司 / 信息 / 声优 / 制作 / 作者关联 / 收藏 / 浏览记录 /
    同步记录 / 资讯关联）一并清掉：否则标签计数会长期虚高（线上曾出现点 `TV`
    出 2974 条、可见作品远少于此），「按标签反查」也会列出空壳。

    ids 先去重再删除，避免同一 id 重复提交导致行数统计偏差。
    """
    cleaned = sorted({int(item) for item in ids})
    if not cleaned:
        return 0
    for model in _CONTENT_WEAK_ASSOCIATIONS:
        db.execute(sa_delete(model).where(model.content_id.in_(cleaned)))
    result = db.execute(sa_delete(Content).where(Content.id.in_(cleaned)))
    db.commit()
    return result.rowcount or 0


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
