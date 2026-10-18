"""业务页面路由：ACGN 全部作品/详情、资讯与新番档期（数据来源：数据库访问层）。

这里只放对读者开放的内容页面；内容管理（/manage/*）与站点设置（/admin/*）
各自独立成模块，测试平台页面见 app.testing_pages。
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from urllib.parse import quote

from sqlalchemy.exc import SQLAlchemyError

from app import bangumi_meta, crud, media
from app.config import settings
from app.page_common import CONTENT_TYPE_LABELS, DbSession, render_missing, templates

router = APIRouter(tags=["pages"])


# ---------- ACGN 内容展示（契约不变） ----------

# 内容搜索的类型选项（中文标签）规范来源在 page_common（模板全局 + re-export）；
# 分页大小沿用本模块常量。
CONTENT_PAGE_SIZE = 20


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: DbSession):
    """首页（门户）：分类浏览 / 推荐作品 / 最新资讯。

    本页只做「发现」：分类入口、高分推荐与最新资讯；
    全量浏览与关键词/类型搜索统一落在「全部作品」页（/contents），
    首页不再承载全量列表（无分页的全量表不适合门户页）。
    """
    # 首页「本日新番」：只取今天星期几（weekday 1=周一…7=周日）的档期作品，
    # 按播出时间（HH:MM，到分）升序取前 6 条（带真实封面，外链 B 站图床）。
    # 今天没有排播、或无档期数据时整块不渲染，不影响其它区块。
    seasons = crud.list_schedule_seasons(db)
    current_season = seasons[0]["season_code"] if seasons else None
    today_spotlight = (
        crud.list_all_schedules(db, season=current_season, weekday=datetime.now().isoweekday())
        if current_season
        else []
    )
    # 查询按星期/标题排序，这里改按播出时间排（无时间的排最后）；HH:MM 定长字符串可直接比较
    spotlight = sorted(
        today_spotlight,
        key=lambda s: (s.broadcast_time is None, s.broadcast_time or ""),
    )[:6]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "type_labels": CONTENT_TYPE_LABELS,
            "type_counts": crud.count_contents_by_type(db),
            "top_contents": crud.list_top_contents(db, limit=6),
            "latest_articles": crud.list_articles(db, limit=5),
            "spotlight": spotlight,
        },
    )


@router.get("/contents", response_class=HTMLResponse)
def contents_page(
    request: Request,
    db: DbSession,
    q: str = Query(default=""),
    content_type: str | None = Query(default=None, alias="type", max_length=20),
    tag: str | None = Query(default=None, max_length=80),
    creator: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
):
    """全部作品页：关键词/类型筛选 + 卡片网格 + 分页。

    与 /contents/{content_id}（详情页）是精确路径与路径参数的关系，互不冲突；
    非法类型页面侧宽容忽略（严格 400 校验由 API 负责），与旧首页行为一致。
    标签筛选来自 Bangumi 导入的作品标签（见 crud.list_tags_with_counts），
    与关键词/类型可叠加；筛选列表本身不带当前标签高亮由模板按 tag 判定。
    作者/公司筛选（creator）来自信息卡识别（crud.list_entity_contents 同款
    连接），与其它条件可叠加，是索引页「查看全部作品」的落地参数。
    """
    keyword = q.strip()
    if content_type is not None:
        content_type = content_type.strip().lower() or None
        if content_type is not None and content_type not in CONTENT_TYPE_LABELS:
            content_type = None
    tag_name = (tag or "").strip() or None
    creator_name = (creator or "").strip() or None
    search_mode = (
        bool(keyword) or content_type is not None or tag_name is not None or creator_name is not None
    )

    total, contents = crud.search_contents_page(
        db,
        keyword=keyword,
        content_type=content_type,
        tag=tag_name,
        creator=creator_name,
        page=page,
        page_size=CONTENT_PAGE_SIZE,
    )
    total_pages = max(1, (total + CONTENT_PAGE_SIZE - 1) // CONTENT_PAGE_SIZE)

    return templates.TemplateResponse(
        request=request,
        name="contents.html",
        context={
            "contents": contents,
            "q": q,
            "keyword": keyword,
            "content_type": content_type,
            "type_labels": CONTENT_TYPE_LABELS,
            "search_mode": search_mode,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "tag": tag_name,
            "creator": creator_name,
            "tag_options": crud.list_tags_with_counts(db),
        },
    )

@router.get("/contents/{content_id}", response_class=HTMLResponse)
def content_detail(request: Request, content_id: int, db: DbSession):
    """详情页；内容不存在时渲染 404 页面并返回 404 状态码。

    页面信息全部来自库内真实数据：类型/评分/来源取自条目本身；
    相关内容为同类型高分条目，相关资讯为文本命中作品标题的已发布文章，
    两类关联数据缺失时由模板展示空态，不编造数据。

    作者/公司关系（信息卡识别，见 app/entity_links.py）按角色分组展示，
    点击姓名进索引页反查其全部作品；有所属系列时额外展示同系列作品。

    登录用户浏览时写入「浏览记录」（个人空间）：同一作品重复浏览只更新
    时间、不堆叠行；未登录游客不记录（浏览记录属于登录账号）。
    """
    item = crud.get_content(db, content_id)
    if item is None:
        return render_missing(request, "内容", content_id)
    current_user = getattr(request.state, "current_user", None)
    favorited = False
    content_status = None
    if current_user is not None:
        crud.record_content_view(db, current_user.id, content_id)
        favorited = crud.is_favorite(db, current_user.id, content_id)
        content_status = crud.get_content_status(db, current_user.id, content_id)
    # 按需补全制作 / 声优 / 客观信息：首次访问（或超过 TTL）拉一次，
    # 失败静默——资料不全不该让详情页 500。
    if settings.bangumi_meta_on_demand and item.bangumi_id is not None:
        stale = item.meta_fetched_at is None or (
            datetime.now() - item.meta_fetched_at
        ).days >= settings.bangumi_meta_ttl_days
        if stale:
            try:
                bangumi_meta.sync_content_meta(db, item, timeout=6.0, retries=1)
            except Exception:  # noqa: BLE001 - 外部接口不可用不影响页面
                db.rollback()

    # 封面按需下载：低热作品导入时没批量下封面（只存了远端地址），
    # 第一次被访问时补齐。失败静默——封面取不到就回退渐变占位图。
    if item.image_url is None and item.remote_image_url:
        local_url = media.fetch_cover_on_demand(item)
        if local_url:
            try:
                db.commit()
            except SQLAlchemyError:  # noqa: N818 - 回填失败不影响页面渲染
                db.rollback()

    series = crud.get_series(db, item.series_id) if item.series_id else None
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "content": item,
            "type_label": CONTENT_TYPE_LABELS.get(item.type, item.type),
            "related_contents": crud.list_related_contents(db, item),
            "related_articles": crud.list_related_articles(db, item),
            "is_favorited": favorited,
            "content_status": content_status,
            "status_labels": crud.CONTENT_STATUS_LABELS,
            "content_tags": crud.list_content_tags(db, content_id),
            "content_studios": crud.list_content_studios(db, content_id),
            "content_credits": crud.list_content_credits(db, content_id),
            "content_cast": crud.list_content_cast(db, content_id, limit=12),
            "content_facts": crud.list_content_facts(db, content_id),
            "content_entity_roles": crud.list_content_entity_roles(db, content_id),
            "content_series": series,
            "series_contents": (
                crud.list_series_contents(db, series.id, exclude_content_id=content_id)
                if series is not None
                else []
            ),
        },
    )


# ---------- 作者 / 公司索引（作品关系，数据来自信息卡识别回填） ----------

# 单个作者/公司页展示的作品上限：按评分取前 N 部，更多作品走
# /contents?creator=... 的分页列表（页面给出入口）。
CREATOR_WORKS_LIMIT = 60


@router.get("/cast/{person_id}", response_class=HTMLResponse)
def cast_detail(request: Request, person_id: int, db: DbSession):
    """声优 / 制作人页：按 Bangumi 人物 id 反查其参与的全部作品。"""
    works = crud.list_person_works(db, person_id)
    name = crud.person_name(db, person_id)
    if not name and not works["credits"] and not works["cast"]:
        return render_missing(request, "人物", person_id)

    merged: dict[int, dict] = {}
    for content, character in works["cast"]:
        merged.setdefault(
            content.id,
            {"content": content, "roles": [], "characters": []},
        )["characters"].append(character)
    for content, role in works["credits"]:
        entry = merged.setdefault(
            content.id, {"content": content, "roles": [], "characters": []}
        )
        if role not in entry["roles"]:
            entry["roles"].append(role)

    rows = sorted(
        merged.values(), key=lambda item: (-item["content"].rating, -item["content"].id)
    )
    return templates.TemplateResponse(
        request=request,
        name="person.html",
        context={
            "person_name": name or f"人物 {person_id}",
            "person_id": person_id,
            "rows": rows,
            "cast_count": len(works["cast"]),
            "credit_count": len(works["credits"]),
        },
    )


@router.get("/studios/{name}", response_class=HTMLResponse)
def studio_detail(request: Request, name: str, db: DbSession):
    """制作公司页：该公司在库内的全部作品（按评分倒序）。"""
    contents = crud.list_contents_by_studio(db, name)
    if not contents:
        return render_missing(request, "制作公司", name)
    return templates.TemplateResponse(
        request=request,
        name="studio.html",
        context={
            "studio_name": name,
            "contents": contents,
            "type_labels": CONTENT_TYPE_LABELS,
        },
    )


@router.get("/creators", response_class=HTMLResponse)
def creators_index(request: Request, db: DbSession):
    """作者/公司索引页：实体花名册 + 系列列表（均按关联作品数排序）。

    数据来自 Bangumi 信息卡识别（app/entity_links.py，导入时或
    scripts/link_entities.py 回填写入）；库内没有任何关联时模板展示
    空态与回填指引，不编造条目。
    """
    return templates.TemplateResponse(
        request=request,
        name="creators.html",
        context={
            "view": "index",
            "type_labels": CONTENT_TYPE_LABELS,
            "entity_type_labels": crud.ENTITY_TYPE_LABELS,
            "entities": crud.list_entities_with_counts(db),
            "series_list": crud.list_series_with_counts(db),
        },
    )


@router.get("/creators/{name}", response_class=HTMLResponse)
def creator_detail(request: Request, name: str, db: DbSession):
    """作者/公司（或系列）详情页：先按实体名解析，再按系列名解析。

    实体名与系列名各自全局唯一，先实体后系列足以区分；两者都不命中时
    渲染 404（与详情页同一套机制）。作品列表按评分降序取前
    CREATOR_WORKS_LIMIT 部，完整列表走 /contents?creator=... 。
    """
    entity = crud.get_entity_by_name(db, name)
    if entity is not None:
        contents = crud.list_entity_contents(db, entity.id, limit=CREATOR_WORKS_LIMIT)
        return templates.TemplateResponse(
            request=request,
            name="creators.html",
            context={
                "view": "entity",
                "type_labels": CONTENT_TYPE_LABELS,
                "entity": entity,
                "entity_type_label": crud.ENTITY_TYPE_LABELS.get(
                    entity.entity_type, entity.entity_type
                ),
                "contents": contents,
                "filter_url": f"/contents?creator={quote(entity.name)}",
            },
        )

    series = crud.get_series_by_name(db, name)
    if series is not None:
        contents = crud.list_series_contents(db, series.id, limit=CREATOR_WORKS_LIMIT)
        return templates.TemplateResponse(
            request=request,
            name="creators.html",
            context={
                "view": "series",
                "type_labels": CONTENT_TYPE_LABELS,
                "series": series,
                "contents": contents,
            },
        )

    return render_missing(request, "作者/公司", name)
@router.get("/articles", response_class=HTMLResponse)
def articles_page(
    request: Request,
    db: DbSession,
    q: str = Query(default="", max_length=80),
):
    """资讯列表页：支持标题/摘要/正文关键词搜索。"""
    articles = crud.list_articles(db, q=q or None)
    return templates.TemplateResponse(
        request=request,
        name="articles.html",
        context={
            "articles": articles,
            "q": q,
            # 每篇资讯的识别标签（列表页 chips；一次查询，避免 N+1）
            "article_tags_map": crud.article_tags_map(db, [a.id for a in articles]),
        },
    )


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail_page(
    request: Request,
    article_id: int,
    db: DbSession,
):
    """资讯详情页面。

    除正文外展示「识别标签」与「关联作品」：两者由自动识别写入
    （app/article_tags，导入时或 scripts/link_articles.py 回填），
    点击标签跳全部作品页按该标签筛选，点击作品进作品详情页。
    """
    article = crud.get_article(db, article_id)

    if article is None:
        return render_missing(request, "资讯", article_id)

    return templates.TemplateResponse(
        request=request,
        name="article_detail.html",
        context={
            "article": article,
            "article_tags": crud.list_article_tags(db, article_id),
            "linked_contents": crud.list_article_contents(db, article_id),
        },
    )

# ---------- 新番档期展示（只读：数据来自同步脚本，不在请求时抓取外部网站） ----------

WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
SCHEDULE_PAGE_SIZE = 20


def _group_by_weekday(works: list) -> list[dict]:
    """把整季作品按播出星期分组：周一…周日顺序，无星期的归入“待定”组。

    只返回有作品的分组；分组数据全部来自库内真实记录，不做任何推测。
    """
    buckets: dict[int | None, list] = {}
    for item in works:
        key = item.weekday if item.weekday in WEEKDAY_NAMES else None
        buckets.setdefault(key, []).append(item)
    groups = [
        # 键名用 works 而非 items：dict.items 是内建方法，模板属性访问会撞名
        {"weekday": wd, "name": WEEKDAY_NAMES[wd], "works": buckets.get(wd, [])}
        for wd in WEEKDAY_NAMES
    ]
    if None in buckets:
        groups.append({"weekday": None, "name": "待定", "works": buckets[None]})
    return [group for group in groups if group["works"]]


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(
    request: Request,
    db: DbSession,
    season: str | None = Query(default=None, max_length=6),
    q: str = Query(default=""),
    weekday: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
):
    """新番档期页：季度导航、按星期分组浏览与关键词/星期筛选。

    默认（无 q/weekday）展示当前档期全部作品并按播出星期分组，便于逐日浏览；
    带 q 或 weekday 时进入筛选模式，沿用分页结果列表。
    未指定档期时默认展示最新已同步档期；数据库无任何档期数据时显示引导提示。
    """
    seasons = crud.list_schedule_seasons(db)
    current_season = season or (seasons[0]["season_code"] if seasons else None)
    if weekday is not None and weekday not in WEEKDAY_NAMES:
        weekday = None
    keyword = q.strip() or None
    filter_mode = bool(keyword) or weekday is not None

    total = 0
    works: list = []
    total_pages = 1
    weekday_groups: list[dict] = []
    season_url = None
    if current_season:
        if filter_mode:
            total, works = crud.list_schedules(
                db,
                season=current_season,
                keyword=keyword,
                weekday=weekday,
                page=page,
                page_size=SCHEDULE_PAGE_SIZE,
            )
            total_pages = max(1, (total + SCHEDULE_PAGE_SIZE - 1) // SCHEDULE_PAGE_SIZE)
        else:
            works = crud.list_all_schedules(db, season=current_season)
            total = len(works)
            weekday_groups = _group_by_weekday(works)
        season_url = works[0].source_url if works else None

    return templates.TemplateResponse(
        request=request,
        name="schedule.html",
        context={
            "seasons": seasons,
            "current_season": current_season,
            "season_url": season_url,
            "weekday_names": WEEKDAY_NAMES,
            "weekday": weekday,
            "q": q,
            "page": page,
            "page_size": SCHEDULE_PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "works": works,
            "weekday_groups": weekday_groups,
            "grouped_mode": not filter_mode,
        },
    )


# ---------- 封面图片（缩略图 / 压缩全图：按需生成 + 长缓存） ----------

# 派生图缓存有效期（30 天）：与部署侧 Nginx expires 30d 对齐，
# 朋友重复浏览同一张封面不再产生请求，也不占用服务器流量。
_VARIANT_CACHE_HEADERS = {"Cache-Control": "public, max-age=2592000"}


def _serve_cover_variant(kind: str, filename: str):
    """按需返回封面派生图（thumb=400px 缩略 / full=≤1200px 压缩全图）。

    命中磁盘缓存直接返回；未命中用 Pillow 现场生成并原子落盘；
    生成失败（源图损坏等）降级返回原图——封面可见性优先于体积优化；
    原图也没有才 404，由前端 onerror 回退渐变占位。
    """
    variant = media.ensure_variant(kind, filename)
    if variant is not None:
        return FileResponse(
            variant,
            media_type=media.VARIANT_MEDIA_TYPE,
            headers=dict(_VARIANT_CACHE_HEADERS),
        )
    name = media.safe_cover_name(filename)
    if name is not None:
        original = media.cover_root() / name
        if original.is_file():
            return FileResponse(
                original,
                media_type=media.guess_media_type(original),
                headers=dict(_VARIANT_CACHE_HEADERS),
            )
    raise HTTPException(status_code=404, detail="封面不存在")


@router.get("/media/thumb/{filename}")
def media_thumb(filename: str):
    """列表卡片缩略图（400px WebP）：万部作品的列表页流量命门。"""
    return _serve_cover_variant("thumb", filename)


@router.get("/media/full/{filename}")
def media_full(filename: str):
    """详情页全图（≤1200px / ≤400KB WebP）：比直接铺原图省一半以上流量。"""
    return _serve_cover_variant("full", filename)
