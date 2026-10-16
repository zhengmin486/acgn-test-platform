"""业务页面路由：ACGN 全部作品/详情、资讯、新番档期与同步历史（数据来源：数据库访问层）。

其中「同步历史」属于管理性质功能，随测试管理平台一起仅对管理员开放；
其它内容页面（首页 / 资讯 / 新番档期 / 详情）对所有人开放。
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app import crud
from app.auth import require_admin_page
from app.page_common import DbSession, render_missing, templates

router = APIRouter(tags=["pages"])


# ---------- ACGN 内容展示（契约不变） ----------

# 内容搜索的类型选项（中文标签）与分页大小；与 /api/contents 的类型全集一致。
CONTENT_TYPE_LABELS = {"anime": "动画", "comic": "漫画", "game": "游戏", "novel": "轻小说"}
CONTENT_PAGE_SIZE = 20


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: DbSession):
    """首页（门户）：分类浏览 / 推荐作品 / 最新资讯。

    本页只做「发现」：分类入口、高分推荐与最新资讯；
    全量浏览与关键词/类型搜索统一落在「全部作品」页（/contents），
    首页不再承载全量列表（无分页的全量表不适合门户页）。
    """
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "type_labels": CONTENT_TYPE_LABELS,
            "type_counts": crud.count_contents_by_type(db),
            "top_contents": crud.list_top_contents(db, limit=6),
            "latest_articles": crud.list_articles(db, limit=5),
        },
    )


@router.get("/contents", response_class=HTMLResponse)
def contents_page(
    request: Request,
    db: DbSession,
    q: str = Query(default=""),
    content_type: str | None = Query(default=None, alias="type", max_length=20),
    page: int = Query(default=1, ge=1),
):
    """全部作品页：关键词/类型筛选 + 卡片网格 + 分页。

    与 /contents/{content_id}（详情页）是精确路径与路径参数的关系，互不冲突；
    非法类型页面侧宽容忽略（严格 400 校验由 API 负责），与旧首页行为一致。
    """
    keyword = q.strip()
    if content_type is not None:
        content_type = content_type.strip().lower() or None
        if content_type is not None and content_type not in CONTENT_TYPE_LABELS:
            content_type = None
    search_mode = bool(keyword) or content_type is not None

    total, contents = crud.search_contents_page(
        db,
        keyword=keyword,
        content_type=content_type,
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
        },
    )

@router.get("/contents/{content_id}", response_class=HTMLResponse)
def content_detail(request: Request, content_id: int, db: DbSession):
    """详情页；内容不存在时渲染 404 页面并返回 404 状态码。

    页面信息全部来自库内真实数据：类型/评分/来源取自条目本身；
    相关内容为同类型高分条目，相关资讯为文本命中作品标题的已发布文章，
    两类关联数据缺失时由模板展示空态，不编造数据。

    登录用户浏览时写入「浏览记录」（个人空间）：同一作品重复浏览只更新
    时间、不堆叠行；未登录游客不记录（浏览记录属于登录账号）。
    """
    item = crud.get_content(db, content_id)
    if item is None:
        return render_missing(request, "内容", content_id)
    current_user = getattr(request.state, "current_user", None)
    if current_user is not None:
        crud.record_content_view(db, current_user.id, content_id)
        favorited = crud.is_favorite(db, current_user.id, content_id)
    else:
        favorited = False
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "content": item,
            "type_label": CONTENT_TYPE_LABELS.get(item.type, item.type),
            "related_contents": crud.list_related_contents(db, item),
            "related_articles": crud.list_related_articles(db, item),
            "is_favorited": favorited,
        },
    )
@router.get("/articles", response_class=HTMLResponse)
def articles_page(request: Request, db: DbSession):
    """资讯列表页面。"""
    return templates.TemplateResponse(
        request=request,
        name="articles.html",
        context={"articles": crud.list_articles(db)},
    )


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail_page(
    request: Request,
    article_id: int,
    db: DbSession,
):
    """资讯详情页面。"""
    article = crud.get_article(db, article_id)

    if article is None:
        return render_missing(request, "资讯", article_id)

    return templates.TemplateResponse(
        request=request,
        name="article_detail.html",
        context={"article": article},
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


@router.get("/schedule/runs", response_class=HTMLResponse,
            dependencies=[Depends(require_admin_page)])
def schedule_runs_page(request: Request, db: DbSession):
    """同步历史页（管理功能，仅管理员）：每次同步（含 dry-run 预览）的运行结果。"""
    total, runs = crud.list_sync_runs(db, limit=100)
    return templates.TemplateResponse(
        request=request,
        name="schedule_runs.html",
        context={"runs": runs, "total": total},
    )
