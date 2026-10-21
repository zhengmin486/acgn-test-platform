"""业务页面路由：ACGN 全部作品/详情、资讯与新番档期（数据来源：数据库访问层）。

这里只放对读者开放的内容页面；内容管理（/manage/*）与站点设置（/admin/*）
各自独立成模块，测试平台页面见 app.testing_pages。
"""

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from urllib.parse import quote, urlencode

from app import article_body, bangumi_meta, crud, media, persona
from app.config import settings
from app.database import SessionLocal
from app.page_common import CONTENT_TYPE_LABELS, DbSession, render_missing, templates
from app.search_query import parse_query
from app.tagging import TYPE_SPECIFIC_TAGS, form_tag_names

router = APIRouter(tags=["pages"])

logger = logging.getLogger(__name__)


# ---------- 详情页的后台补全（不在请求内等外部接口） ----------

# 同一部作品同时只跑一个后台补全：热门页连点不会叠出多个线程去抢同一份数据。
_ASSET_INFLIGHT: set[int] = set()
_ASSET_LOCK = threading.Lock()


# ---------- ACGN 内容展示（契约不变） ----------

# 内容搜索的类型选项（中文标签）规范来源在 page_common（模板全局 + re-export）；
# 分页大小沿用本模块常量。
# 每页条数：与 style.css 里 .works-grid 的自动列数联动。
# 列数不再写死断点，而是由「每列最小宽 clamp(150px,15vw,220px)」+ 容器上限 1296px
# 共同决定，实际只可能是 2/3/4/5/6 列；60 是这五个数的最小公倍数，
# 所以任何窗口宽度下最后一排都是满的，不会空出格子。
# 改列宽 clamp 的两个端点时，必须回来确认新的列数集合仍能整除本值。
CONTENT_PAGE_SIZE = 60

# 首页「推荐作品」模块：自适应网格的初始条数 + 「展开」后的上限。
# 初始取 CONTENT_PAGE_SIZE：网格列数实测 1~6，60 是 LCM——初始视图在任何
# 窗口宽度下末行都是满的（与全部作品页同一契约）。
# 展开上限 100（用户拍板）：100 只整除 2/4/5，展开态在 3/6 列宽度下末行
# 可能不满一行（已知取舍；要严格满行须把上限抬到 120）。
HOME_RECOMMEND_INITIAL = CONTENT_PAGE_SIZE
HOME_RECOMMEND_MAX = 100

# 首页资讯软配额：核心内容优先、不强行填满（与每日入库上限同为 30）。
HOME_NEWS_LIMIT = 30

# 筛选栏标签 chips：默认只预览前 TAG_PREVIEW_LIMIT 个，超出给「展开全部标签」入口；
# 展开（?all_tags=1）才取全部题材，避免一行塞太多。
TAG_PREVIEW_LIMIT = 16

# 排序三档。目前没有点击量埋点，等有了再加第四档：往这里加一项 +
# crud._content_search_stmt 加一个分支即可，URL 形状不变（未选不写参数）。
SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("rating", "评分最高"),
    ("favorites", "收藏最多"),
    ("newest", "时间最新"),
)
SORT_LABELS = dict(SORT_OPTIONS)
# 视图三档：bili 是默认视图（未选不写 URL，保持既有链接形状）——
# B站式「封面 + 标题 + 元数据行」卡片；grid 保留摘要供有目的查找；list 高密度扫片。
VIEW_LABELS = {"bili": "卡片", "grid": "网格", "list": "列表"}


def _refresh_content_assets(content_id: int) -> None:
    """后台补齐封面与制作信息：独立 session，不占用请求连接。

    为什么搬到后台：旧实现把两件事放在详情页请求里同步做，Bangumi 不可达时
    要串行等「subject / characters / persons」三个接口各自重试（线上实测每页
    稳定 12.5 秒），而且失败不写任何状态，于是下一次访问照样重来一遍。
    现在页面只读库、立即返回，补全在这里做，失败记冷却窗口。
    """
    with _ASSET_LOCK:
        if content_id in _ASSET_INFLIGHT:
            return
        _ASSET_INFLIGHT.add(content_id)
    db = SessionLocal()
    try:
        item = crud.get_content(db, content_id)
        if item is None:
            return
        # 封面：导入时只留了远端地址的条目，第一次被打开时补下载并回填。
        if item.image_url is None and item.remote_image_url:
            if media.fetch_cover_on_demand(item):
                db.commit()
        # 制作 / 声优 / 客观信息：TTL 过期且不在失败冷却期才拉。
        if settings.bangumi_meta_on_demand and bangumi_meta.meta_is_stale(item):
            try:
                bangumi_meta.sync_content_meta(
                    db, item, timeout=settings.bangumi_meta_timeout, retries=0
                )
            except Exception:  # noqa: BLE001 - 外部接口故障不影响任何页面
                db.rollback()
                fresh = crud.get_content(db, content_id)
                if fresh is not None:
                    bangumi_meta.mark_meta_failed(db, fresh)
    except Exception:  # noqa: BLE001 - 后台任务的异常一律不外抛
        # 堆栈只进服务端日志（每次失败一条，不在循环里刷屏），前端无感知。
        logger.exception("详情页后台补全失败（content_id=%s）", content_id)
        db.rollback()
    finally:
        db.close()
        with _ASSET_LOCK:
            _ASSET_INFLIGHT.discard(content_id)



@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: DbSession):
    """首页（门户）：分类浏览 / 推荐作品 / 最新资讯。

    本页只做「发现」：分类入口、按站内口味推荐的精选与最新资讯；
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
    type_counts = crud.count_contents_by_type(db)
    # 推荐作品：登录用户按个人口味（成分画像 / 站内实时口径，见 persona.build_user_home_picks），
    # 无任何可用信号时回落站长口味；游客一律站长口味（与旧行为一致）。
    # 取满 HOME_RECOMMEND_MAX，模板默认铺前 HOME_RECOMMEND_INITIAL 部（网格适配、
    # 末行必满），其余收起，「展开更多推荐」按钮点开（无 JS 时前 60 部照常可见）。
    current_user = getattr(request.state, "current_user", None)
    top_contents = None
    if current_user is not None:
        top_contents = persona.build_user_home_picks(
            db, current_user.id, limit=HOME_RECOMMEND_MAX
        )
    if not top_contents:
        top_contents = crud.list_home_recommendations(db, limit=HOME_RECOMMEND_MAX)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "type_labels": CONTENT_TYPE_LABELS,
            "type_counts": type_counts,
            "top_contents": top_contents,
            "home_rec_initial": HOME_RECOMMEND_INITIAL,
            # 首页资讯：30 条软配额，按 priority_score 排序、兼顾类别覆盖、
            # 折叠官方 PR 源（PR 仍可在资讯频道按来源浏览）。
            "latest_articles": crud.list_home_feed(
                db, limit=HOME_NEWS_LIMIT, exclude_pr=True
            ),
            "hide_adult": settings.news_hide_adult_content,
            "spotlight": spotlight,
            # hero 数据行：收录作品数 / 本日新番 / 资讯篇数（全部真实统计，空库显示 0）
            "stats": {
                "works": sum(type_counts.values()),
                "today": len(spotlight),
                "articles": crud.count_articles(db),
            },
        },
    )


@router.get("/contents", response_class=HTMLResponse)
def contents_page(
    request: Request,
    db: DbSession,
    q: str = Query(default=""),
    content_type: str | None = Query(default=None, alias="type", max_length=20),
    tag: str | None = Query(default=None, max_length=80),
    tags: str | None = Query(default=None, max_length=200),
    creator: str | None = Query(default=None, max_length=120),
    year: int | None = Query(default=None, alias="year", ge=1900, le=2100),
    media: str | None = Query(default=None, max_length=20),
    source_material: str | None = Query(default=None, alias="source", max_length=20),
    sort: str | None = Query(default=None, max_length=20),
    view: str | None = Query(default=None, max_length=10),
    all_tags: bool = Query(default=False, alias="all_tags"),
    page: int = Query(default=1, ge=1),
):
    """全部作品页：关键词/类型/标签/形式/年份筛选 + 排序 + 三种视图 + 分页。

    与 /contents/{content_id}（详情页）是精确路径与路径参数的关系，互不冲突；
    非法类型页面侧宽容忽略（严格 400 校验由 API 负责），与旧首页行为一致。
    标签筛选来自 Bangumi 导入的作品标签（见 crud.list_tags_with_counts），
    与关键词/类型可叠加；多标签（``?tags=a,b``）为 AND 语义，旧单标签
    ``?tag=`` 仍兼容（见 crud.parse_tag_query）。
    作者/公司筛选（creator）来自信息卡识别（crud.list_entity_contents 同款
    连接），与其它条件可叠加，是索引页「查看全部作品」的落地参数。

    形式筛选（``media`` 放送形态 / ``source`` 原作载体）走 contents 的客观
    信息列，与题材标签完全解耦：选「TV」不会改变题材 chips 的计数范围。
    ``sort`` / ``view`` / ``media`` / ``source`` 一律「未选不写入 URL」——
    既有分享链接与测试断言都按旧参数形状（q/type/tags/creator/year）书写。
    默认视图是 B站式卡片（``view=bili`` 不写参数）：封面 + 标题 + 元数据行，
    元数据行随排序自适应（评分/收藏/年份哪个在前），grid 保留摘要、list 高密度。
    """
    keyword = q.strip()
    # 非法排序 / 视图值静默退回默认，不报错（页面侧宽容，与类型一致）
    if sort not in SORT_LABELS:
        sort = None
    if view not in VIEW_LABELS:
        view = None
    media_name = (media or "").strip() or None
    source_name = (source_material or "").strip() or None
    # 查询理解层：题材/类型/年份/季数意图识别（app/search_query，确定性规则）。
    # URL 显式筛选参数优先（用户点了 chips），解析结果只补空缺。
    parsed = parse_query(keyword)
    if content_type is None and parsed.content_type:
        content_type = parsed.content_type
    if year is None and parsed.year is not None:
        year = parsed.year
    search_genres = parsed.genres
    season_number = parsed.season_number
    if content_type is not None:
        content_type = content_type.strip().lower() or None
        if content_type is not None and content_type not in CONTENT_TYPE_LABELS:
            content_type = None
    selected_tags = crud.parse_tag_query(tags, tag)
    creator_name = (creator or "").strip() or None
    search_mode = (
        bool(keyword)
        or content_type is not None
        or bool(selected_tags)
        or creator_name is not None
        or year is not None
        or media_name is not None
        or source_name is not None
        or sort is not None
    )

    total, contents = crud.search_contents_page(
        db,
        keyword=parsed.keyword,
        content_type=content_type,
        tags=selected_tags,
        creator=creator_name,
        year=year,
        genres=search_genres,
        season_number=season_number,
        media=media_name,
        source_material=source_name,
        sort=sort,
        page=page,
        page_size=CONTENT_PAGE_SIZE,
    )
    total_pages = max(1, (total + CONTENT_PAGE_SIZE - 1) // CONTENT_PAGE_SIZE)

    # B站卡片元数据行：收藏人数（与「收藏最多」排序同源，一次查当前页 id，
    # 不逐作品查库）；「新」角标按当前日历年判定（年份来自作品信息）。
    favorite_counts = crud.list_favorite_counts(db, [item.id for item in contents])
    current_year = datetime.now().year

    # 题材 chips：排除形式载体（TV / 漫画改，已由 media/source 列承载）与
    # 当前类型的专属词（RPG / 视觉小说，单列一组）——排除必须发生在 limit
    # 之前，否则高频形式词会把前 N 个名额吃光，题材组反而空了。
    _type_specific = set(TYPE_SPECIFIC_TAGS.get(content_type or "", frozenset()))
    _exclude = set(form_tag_names()) | _type_specific
    if all_tags:
        _tag_rows = crud.list_tags_with_counts(
            db, content_type=content_type, limit=None, exclude=_exclude
        )
        has_more_tags = False
    else:
        _tag_rows = crud.list_tags_with_counts(
            db,
            content_type=content_type,
            limit=TAG_PREVIEW_LIMIT + 1,
            exclude=_exclude,
        )
        has_more_tags = len(_tag_rows) > TAG_PREVIEW_LIMIT
        _tag_rows = _tag_rows[:TAG_PREVIEW_LIMIT]
    tag_options = [
        {
            "name": name,
            "count": count,
            "selected": name in selected_tags,
            "url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=_toggle_tag(selected_tags, name),
                all_tags=all_tags,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
        }
        for name, count in _tag_rows
    ]

    # 类型专属 chips：只在选中类型时才有意义（给动画看「RPG」没意义），
    # 与题材组同源（content_tags），只是单独成组展示。
    type_specific_options = [
        {
            "name": name,
            "count": count,
            "selected": name in selected_tags,
            "url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=_toggle_tag(selected_tags, name),
                all_tags=all_tags,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
        }
        for name, count in (
            crud.list_type_specific_tags_with_counts(
                db, content_type, _type_specific, limit=12
            )
            if _type_specific and content_type
            else []
        )
    ]

    # 形式 chips：数据来自 contents.media / contents.source_material 两列
    # （清洗脚本从标签搬过来的客观信息），单选且点已选的=清除。
    media_options = [
        {
            "name": name,
            "count": count,
            "selected": media_name == name,
            "url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=all_tags,
                media=_toggle_value(media_name, name),
                source_material=source_name,
                sort=sort,
                view=view,
            ),
        }
        for name, count in crud.list_media_with_counts(db, content_type=content_type)
    ]
    source_options = [
        {
            "name": name,
            "count": count,
            "selected": source_name == name,
            "url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=all_tags,
                media=media_name,
                source_material=_toggle_value(source_name, name),
                sort=sort,
                view=view,
            ),
        }
        for name, count in crud.list_source_material_with_counts(
            db, content_type=content_type
        )
    ]

    # 年份 chips（B 站式「年份」检索分类）：与标签/类型/作者叠加，
    # 点已在选的年份=清除；按当前类型聚合，新→旧排列，带作品数。
    year_options = [
        {
            "year": y,
            "count": c,
            "selected": year is not None and y == year,
            "url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=(None if (year is not None and y == year) else y),
                tags=selected_tags,
                all_tags=all_tags,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
        }
        for y, c in crud.list_years_with_counts(db, content_type=content_type)
    ]

    # 排序 chips：三档互斥，点已选的=回到默认序（收藏人数由 content_favorites
    # 聚合，评分与年份走 contents 列）。
    sort_options = [
        {
            "code": code,
            "label": label,
            "selected": sort == code,
            "url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=all_tags,
                media=media_name,
                source_material=source_name,
                sort=(None if sort == code else code),
                view=view,
            ),
        }
        for code, label in SORT_OPTIONS
    ]

    # 类型 Tab：一级维度从下拉框提到页头，带真实作品数；「全部」= 不限类型。
    _type_counts = crud.count_contents_by_type(db)
    type_tabs = [
        {
            "code": "",
            "label": "全部",
            "count": sum(_type_counts.values()),
            "selected": content_type is None,
            "url": _contents_url(
                q=q,
                content_type=None,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=all_tags,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
        }
    ] + [
        {
            "code": code,
            "label": label,
            "count": _type_counts.get(code, 0),
            "selected": content_type == code,
            "url": _contents_url(
                q=q,
                content_type=code,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=all_tags,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
        }
        for code, label in CONTENT_TYPE_LABELS.items()
    ]

    # 已选条件条：每个条件都能单独摘掉（保留其余），末尾是「清除全部」。
    # 旧版只有一行灰色文字说明，筛多了根本看不出自己选了什么。
    active_filters = []
    if keyword:
        active_filters.append(
            {
                "label": f"关键词：{keyword}",
                "url": _contents_url(
                    q="",
                    content_type=content_type,
                    creator=creator_name,
                    year=year,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=media_name,
                    source_material=source_name,
                    sort=sort,
                    view=view,
                ),
            }
        )
    if content_type is not None:
        active_filters.append(
            {
                "label": CONTENT_TYPE_LABELS.get(content_type, content_type),
                "url": _contents_url(
                    q=q,
                    content_type=None,
                    creator=creator_name,
                    year=year,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=media_name,
                    source_material=source_name,
                    sort=sort,
                    view=view,
                ),
            }
        )
    for name in selected_tags:
        active_filters.append(
            {
                "label": name,
                "url": _contents_url(
                    q=q,
                    content_type=content_type,
                    creator=creator_name,
                    year=year,
                    tags=[item for item in selected_tags if item != name],
                    all_tags=all_tags,
                    media=media_name,
                    source_material=source_name,
                    sort=sort,
                    view=view,
                ),
            }
        )
    if year is not None:
        active_filters.append(
            {
                "label": f"{year} 年",
                "url": _contents_url(
                    q=q,
                    content_type=content_type,
                    creator=creator_name,
                    year=None,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=media_name,
                    source_material=source_name,
                    sort=sort,
                    view=view,
                ),
            }
        )
    if media_name:
        active_filters.append(
            {
                "label": media_name,
                "url": _contents_url(
                    q=q,
                    content_type=content_type,
                    creator=creator_name,
                    year=year,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=None,
                    source_material=source_name,
                    sort=sort,
                    view=view,
                ),
            }
        )
    if source_name:
        active_filters.append(
            {
                "label": source_name,
                "url": _contents_url(
                    q=q,
                    content_type=content_type,
                    creator=creator_name,
                    year=year,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=media_name,
                    source_material=None,
                    sort=sort,
                    view=view,
                ),
            }
        )
    if creator_name:
        active_filters.append(
            {
                "label": creator_name,
                "url": _contents_url(
                    q=q,
                    content_type=content_type,
                    creator=None,
                    year=year,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=media_name,
                    source_material=source_name,
                    sort=sort,
                    view=view,
                ),
            }
        )
    if sort is not None:
        active_filters.append(
            {
                "label": SORT_LABELS[sort],
                "url": _contents_url(
                    q=q,
                    content_type=content_type,
                    creator=creator_name,
                    year=year,
                    tags=selected_tags,
                    all_tags=all_tags,
                    media=media_name,
                    source_material=source_name,
                    sort=None,
                    view=view,
                ),
            }
        )

    # 视图切换：卡片（默认，不写参数）/ 网格（view=grid）/ 列表（view=list）。
    # 卡片是默认视图，所以它的链接刻意不带 view 参数——未选不写入 URL，
    # 既有分享链接与测试断言仍按旧形状（q/type/tags/creator/year）成立。
    bili_url = _contents_url(
        q=q,
        content_type=content_type,
        creator=creator_name,
        year=year,
        tags=selected_tags,
        all_tags=all_tags,
        media=media_name,
        source_material=source_name,
        sort=sort,
        view=None,
    )
    grid_url = _contents_url(
        q=q,
        content_type=content_type,
        creator=creator_name,
        year=year,
        tags=selected_tags,
        all_tags=all_tags,
        media=media_name,
        source_material=source_name,
        sort=sort,
        view="grid",
    )
    list_url = _contents_url(
        q=q,
        content_type=content_type,
        creator=creator_name,
        year=year,
        tags=selected_tags,
        all_tags=all_tags,
        media=media_name,
        source_material=source_name,
        sort=sort,
        view="list",
    )
    view_options = [
        {
            "code": "bili",
            "label": VIEW_LABELS["bili"],
            "selected": view is None,
            "url": bili_url,
        },
        {
            "code": "grid",
            "label": VIEW_LABELS["grid"],
            "selected": view == "grid",
            "url": grid_url,
        },
        {
            "code": "list",
            "label": VIEW_LABELS["list"],
            "selected": view == "list",
            "url": list_url,
        },
    ]

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
            "tags": selected_tags,
            "tag": selected_tags[0] if selected_tags else None,
            "creator": creator_name,
            "year": year,
            "tag_options": tag_options,
            "year_options": year_options,
            "show_all_tags": all_tags,
            "has_more_tags": has_more_tags,
            "expand_tags_url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=True,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
            "collapse_tags_url": _contents_url(
                q=q,
                content_type=content_type,
                creator=creator_name,
                year=year,
                tags=selected_tags,
                all_tags=False,
                media=media_name,
                source_material=source_name,
                sort=sort,
                view=view,
            ),
            # 卡片上的题材 chip：一次 group by 预取，避免逐作品查库（N+1）
            "tag_map": crud.list_content_tags_map(db, [item.id for item in contents]),
            "clear_tags_url": _contents_url(
                q=q, content_type=content_type, creator=creator_name, year=year, tags=[], all_tags=all_tags,
                media=media_name, source_material=source_name, sort=sort, view=view,
            ),
            "clear_year_url": _contents_url(
                q=q, content_type=content_type, creator=creator_name, year=None, tags=selected_tags, all_tags=all_tags,
                media=media_name, source_material=source_name, sort=sort, view=view,
            ),
            # 新增：类型 Tab / 形式 / 类型专属 / 排序 / 已选条件 / 视图
            "type_tabs": type_tabs,
            "media_options": media_options,
            "media": media_name,
            "source_options": source_options,
            "source_material": source_name,
            "type_specific_options": type_specific_options,
            "sort_options": sort_options,
            "sort": sort,
            "sort_label": SORT_LABELS.get(sort or "", ""),
            "active_filters": active_filters,
            # view 是原始参数（None=卡片默认，不写 URL）；active_view 供模板分支
            "view": view,
            "active_view": view or "bili",
            "view_options": view_options,
            "grid_url": grid_url,
            "list_url": list_url,
            # B站卡片：收藏人数 + 当前年（「新」角标判定）
            "favorite_counts": favorite_counts,
            "current_year": current_year,
            # hero 品牌带统计：总数 + 四类型分布（与类型 Tab 同源计数）
            "hero_total": sum(_type_counts.values()),
            "type_counts": _type_counts,
            "clear_all_url": "/contents",
        },
    )


def _toggle_tag(selected: list[str], name: str) -> list[str]:
    """多选切换：已在选中列表里则移除，否则追加到末尾。"""
    if name in selected:
        return [item for item in selected if item != name]
    return [*selected, name]


def _toggle_value(current: str | None, value: str) -> str | None:
    """单选切换：已选中的值再点一次=清除（媒介 / 原作载体共用）。"""
    return None if current == value else value


def _contents_url(
    *,
    q: str,
    content_type: str | None,
    creator: str | None,
    tags: list[str],
    year: int | None = None,
    all_tags: bool = False,
    media: str | None = None,
    source_material: str | None = None,
    sort: str | None = None,
    view: str | None = None,
) -> str:
    """拼 /contents 的筛选地址：结构稳定可分享。

    参数顺序固定为 q / type / tags / creator / year，其后才是新增的
    media / source / sort / view / all_tags，且**一律未选不写入**——既有
    分享链接与 tests/test_tags.py 的断言都按旧形状书写（例如
    ``?q=&type=&tags=&creator=``），新增参数只在用户真的选了时才出现。
    """
    params = {
        "q": q,
        "type": content_type or "",
        "tags": ",".join(tags),
        "creator": creator or "",
    }
    if year is not None:
        params["year"] = year
    if media:
        params["media"] = media
    if source_material:
        params["source"] = source_material
    if sort:
        params["sort"] = sort
    if view:
        params["view"] = view
    if all_tags:
        params["all_tags"] = 1
    return "/contents?" + urlencode(params)

@router.get("/contents/{content_id}", response_class=HTMLResponse)
def content_detail(
    request: Request,
    content_id: int,
    db: DbSession,
    background: BackgroundTasks,
):
    """详情页；内容不存在时渲染 404 页面并返回 404 状态码。

    页面信息全部来自库内真实数据：类型/评分/来源取自条目本身。

    三个关联区块职责分开（数据都在 crud 侧算好，这里只组装）：
    - 系列作品：真实关系（前作 / 续作 / 衍生作，content_relations）
      与同系列（series_id）的并集，去重后按关系远近 + 年份排序；
    - 相关内容推荐：按共享创作者 / 公司实体召回（原作 > 导演 > 制作公司 >
      出版社 > 音乐插画），题材 IDF 加权补位，默认排除系列区已展示的作品；
    - 相关资讯：作品识别关联 ∪ 标题命中，均为真实匹配。

    关联数据缺失时由模板展示空态，不编造数据。

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
    # 封面与制作信息交给后台任务补：请求路径只读库，不跟外部接口赛跑。
    # 旧实现在这里同步调用 Bangumi（三接口 × 重试 → 每页 12.5 秒），
    # 且失败不落状态，于是每次访问都重试一遍。
    needs_assets = (item.image_url is None and item.remote_image_url) or (
        settings.bangumi_meta_on_demand and bangumi_meta.meta_is_stale(item)
    )
    if needs_assets:
        background.add_task(_refresh_content_assets, item.id)

    # 系列区：真实关系（前作 / 续作 / 衍生作，来自 content_relations）
    # ∪ 同系列（series_id），两来源去重、关系标签优先。
    series_items = crud.list_series_section(db, item)
    series_contents = [row for row, _label in series_items]
    series_relation_labels = {row.id: label for row, label in series_items}
    # 推荐区默认排除系列区已展示的作品（两个区块职责分开，不重复占位）
    related_pairs = crud.list_related_contents_with_reasons(
        db,
        item,
        exclude_ids=[row.id for row in series_contents],
    )
    series = crud.get_series(db, item.series_id) if item.series_id else None
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "content": item,
            "type_label": CONTENT_TYPE_LABELS.get(item.type, item.type),
            "related_contents": [row for row, _reason in related_pairs],
            # 推荐理由（真实数据：同原作 / 同导演 / 同制作公司 / 同题材）
            "related_reasons": {
                row.id: reason for row, reason in related_pairs
            },
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
            "series_contents": series_contents,
            # 系列作品的关系标签（前作 / 续作 / 衍生作 / 同系列），键为作品 id
            "series_relation_labels": series_relation_labels,
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
# ACGN 分类的固定展示顺序（资讯列表页筛选条用）；未列出的分类按条数排在后面。
ACGN_CATEGORY_ORDER = ("动画", "漫画", "游戏", "轻小说", "综合动态")


def _ordered_category_counts(counts: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """筛选条顺序：先按 ACGN 类型固定序，再按条数降序（同类多条时稳定）。"""
    rank = {name: index for index, name in enumerate(ACGN_CATEGORY_ORDER)}
    return sorted(counts, key=lambda item: (rank.get(item[0], len(rank)), -item[1]))


@router.get("/articles", response_class=HTMLResponse)
def articles_page(
    request: Request,
    db: DbSession,
    q: str = Query(default="", max_length=80),
    category: str = Query(default="", max_length=40),
    page: int = Query(default=1, ge=1),
):
    """资讯列表页：**全部资讯**按发布时间从新到旧分页，支持 ACGN 分类筛选与关键词搜索。

    与首页精选的分工：首页 / 走 crud.list_home_feed（折叠官方 PR、按优先级、
    单类别软配额），那是「精选位」；本页是「全部资讯」的入口，不折叠、不配额，
    只做分页——否则官方 PR 与同类里靠后的资讯在全站就没有入口了。

    筛选条按 **ACGN 类型**（动画 / 漫画 / 游戏 / 轻小说）组织，分不出 ACGN 类型的
    资讯留在「综合动态」兜底桶（2026-10-21 用户定稿：来源不做筛选入口）。
    """
    page_size = crud.ARTICLES_PAGE_SIZE
    total, articles = crud.list_articles_page(
        db,
        q=q or None,
        category=category or None,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total and page > total_pages:
        # 筛选后页码越界（如从第 5 页切到只有 1 页的分类）：夹回最后一页。
        page = total_pages
        total, articles = crud.list_articles_page(
            db,
            q=q or None,
            category=category or None,
            page=page,
            page_size=page_size,
        )

    params = []
    if q:
        params.append(f"q={quote(q)}")
    if category:
        params.append(f"category={quote(category)}")
    pager_base = "/articles?" + ("&".join(params) + "&" if params else "")

    category_counts = crud.article_category_counts(db)
    total_all = sum(count for _, count in category_counts)

    return templates.TemplateResponse(
        request=request,
        name="articles.html",
        context={
            "articles": articles,
            "q": q,
            "category": category,
            "categories": _ordered_category_counts(category_counts),
            "total": total,
            "total_all": total_all,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "pager_base": pager_base,
            "hide_adult": settings.news_hide_adult_content,
            # 每篇资讯的识别标签（列表页 chips；一次查询，避免 N+1）
            "article_tags_map": crud.article_tags_map(db, [a.id for a in articles]),
        },
    )


def article_detail_lead(article) -> str:
    """详情页顶部摘要：正文已经覆盖摘要内容时返回空串（避免同一段出现两次）。

    导入时 summary 取的是正文前 200 字，中文源尤甚——详情页会把这段先当导语渲染、
    又在正文里再渲染一次。判定：整段包含，或前 30 字出现在正文开头 200 字内。
    """
    lead = (article.summary or "").strip()
    body_text = (article.body or "").strip()
    if not lead or not body_text:
        return lead
    if body_text.startswith(lead):
        return ""
    if len(lead) > 30 and lead[:30] in body_text[:200]:
        return ""
    return lead


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail_page(
    request: Request,
    article_id: int,
    db: DbSession,
):
    """资讯详情页面。

    除正文外展示「识别标签」「作品信息」与「关联作品」：三者都由自动识别
    写入（app/article_tags，导入时或 scripts/link_articles.py 回填）——
    识别标签是题材词（点击跳全部作品页按标签筛选）；作品信息是作者 /
    公司 / 系列 / 译名（点击进花名册页或作品页），同时是关联作品的反查
    依据；关联作品点击进作品详情页。
    """
    article = crud.get_article(db, article_id)

    if article is None:
        return render_missing(request, "资讯", article_id)

    # 摘要与正文开头往往是同一段（导入时 summary 就取 body 前 200 字）：
    # 详情页先渲染摘要再渲染正文会出现「同一段说两遍」。正文已覆盖摘要时不再单独展示。
    lead = article_detail_lead(article)

    return templates.TemplateResponse(
        request=request,
        name="article_detail.html",
        context={
            "article": article,
            "lead": lead,
            # 正文行级结构：文本段 / [图] / [媒体]，模板按块渲染（body 仍是纯文本）
            "body_blocks": article_body.parse_blocks(article.body),
            "article_tags": crud.list_article_tags(db, article_id),
            "article_entity_chips": _article_entity_chips(
                crud.list_article_entities(db, article_id)
            ),
            "linked_contents": crud.list_article_contents(db, article_id),
        },
    )


def _article_entity_chips(entities) -> list[dict]:
    """作品信息 chips 的展示数据：名称 + 来源标注 + 跳转目标。

    实体 / 系列进花名册页（/creators/{name}，反查该作者 / 公司 / 系列的
    全部作品）；译名指向单部作品，直接进作品详情页。
    """
    chips: list[dict] = []
    for item in entities:
        label = crud.WORK_INFO_KIND_LABELS.get(
            (item.kind, item.entity_type), "作品"
        )
        if item.kind == "alias" and item.content_id:
            url = f"/contents/{item.content_id}"
        else:
            url = f"/creators/{quote(item.name)}"
        chips.append({"name": item.name, "label": label, "url": url})
    return chips

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
    season: str | None = Query(default=None, max_length=20),
    category: str | None = Query(default=None, max_length=20),
    q: str = Query(default=""),
    weekday: str | None = Query(default=None, max_length=4),
    page: int = Query(default=1, ge=1),
):
    """新番档期页：季度导航 / 特殊分类 tab、按星期分组浏览与关键词/星期筛选。

    两种浏览维度互斥：给定 category（unreleased / ova_sp / cn_movie）时按特殊分类
    展示；否则按季度档期（season=YYYYMM）展示。两者内部都支持关键词/星期筛选。
    未指定任何维度时默认展示最新已同步季度档期；数据库无档期数据时显示引导提示。
    weekday 收字符串再归一：筛选表单的「全部」选项与分页链接在未选星期时
    都会发出 ``weekday=``（空值），严格 int 声明会 422——空串/非数字一律
    宽容忽略，与 contents 页对非法类型的处理同一原则。
    """
    categories = crud.list_schedule_categories(db)
    category = (category or "").strip() or None
    valid_categories = {c["code"] for c in categories}
    if category and category not in valid_categories:
        category = None

    seasons = crud.list_schedule_seasons(db)
    # categories 恒为三项（含 count=0），是否有数据以“季度档期或任一特殊分类有作品”为准，
    # 否则空库会渲染出一堆 0 计数的 tab 而非「整理中」引导态。
    has_schedule_data = bool(seasons) or any(c["count"] for c in categories)
    if weekday is not None:
        weekday = weekday.strip()
        weekday = int(weekday) if weekday.isdigit() else None
    if weekday is not None and weekday not in WEEKDAY_NAMES:
        weekday = None
    keyword = q.strip() or None
    filter_mode = bool(keyword) or weekday is not None

    current_season = None
    current_category = None
    current_label = None
    total = 0
    works: list = []
    total_pages = 1
    weekday_groups: list[dict] = []
    season_url = None

    if category:
        current_category = category
        current_label = next(
            (c["label"] for c in categories if c["code"] == category), category
        )
        if filter_mode:
            total, works = crud.list_schedules(
                db,
                category=category,
                keyword=keyword,
                weekday=weekday,
                page=page,
                page_size=SCHEDULE_PAGE_SIZE,
            )
            total_pages = max(1, (total + SCHEDULE_PAGE_SIZE - 1) // SCHEDULE_PAGE_SIZE)
        else:
            works = crud.list_all_schedules(db, category=category)
            total = len(works)
            weekday_groups = _group_by_weekday(works)
        season_url = works[0].source_url if works else None
    elif seasons:
        current_season = season or seasons[0]["season_code"]
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
            "categories": categories,
            "has_schedule_data": has_schedule_data,
            "current_season": current_season,
            "current_category": current_category,
            "current_label": current_label,
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
