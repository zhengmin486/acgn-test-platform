"""业务页面路由：ACGN 全部作品/详情、资讯与新番档期（数据来源：数据库访问层）。

这里只放对读者开放的内容页面；内容管理（/manage/*）与站点设置（/admin/*）
各自独立成模块，测试平台页面见 app.testing_pages。
"""

import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from urllib.parse import quote, urlencode

from app import article_body, bangumi_meta, crud, home_posters, media, persona
from app.auth import LoginRequired, safe_next_url
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

# 首页「推荐作品」模块：手机端折叠初始条数 + 「展开」后的上限。
# 折叠分界跟「最新资讯」侧栏组件一致：资讯栏用 col-12 col-lg-3，仅 lg（≥992）
# 并排主列，<992 移到主列下方（下移）。所以 PC（≥992，资讯栏并排）不折叠、
# 直接铺开全部（上限 HOME_RECOMMEND_MAX），原展开按钮位置改为「更多作品」入口；
# 手机（<992，资讯栏下移）折叠到前 HOME_RECOMMEND_INITIAL_MOBILE 部，超出收起、
# 「展开更多推荐」点开。
# 上限 100（用户拍板）：100 只整除 2/4/5，展开态在 3/6 列宽度下末行可能不满一行
# （已知取舍；要严格满行须把上限抬到 120）。
HOME_RECOMMEND_INITIAL_MOBILE = 10
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
VIEW_LABELS = {"grid": "网格", "bili": "卡片", "list": "列表"}


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
    # 取满 HOME_RECOMMEND_MAX，PC（≥992，资讯栏并排）由 CSS 直接铺开全部、不折叠；
    # 手机（<992，资讯栏下移）模板只铺前 HOME_RECOMMEND_INITIAL_MOBILE 部，其余带
    # home-top-extra 收起，「展开更多推荐」按钮点开（无 JS 时前 10 部照常可见）。
    current_user = getattr(request.state, "current_user", None)
    top_contents = None
    if current_user is not None:
        top_contents = persona.build_user_home_picks(
            db, current_user.id, limit=HOME_RECOMMEND_MAX
        )
    if not top_contents:
        top_contents = crud.list_home_recommendations(db, limit=HOME_RECOMMEND_MAX)
    # 首页海报轮播（hero 右侧 Coverflow）：优先服务端全站生效名单；
    # 名单为空时（默认「关闭自动更新」且未保存过名单）回落当季新番实时候选，
    # 不落库、不改任何全站配置——默认状态下首页也不会是死的。
    coverflow_items = home_posters.list_active_posters(db)
    if not coverflow_items:
        coverflow_items = home_posters.list_candidates(db)[: home_posters.COVERFLOW_SIZE]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "type_labels": CONTENT_TYPE_LABELS,
            "type_counts": type_counts,
            "top_contents": top_contents,
            "home_rec_mobile_initial": HOME_RECOMMEND_INITIAL_MOBILE,
            # 首页资讯：30 条软配额，按 priority_score 排序、兼顾类别覆盖、
            # 折叠官方 PR 源（PR 仍可在资讯频道按来源浏览）。
            "latest_articles": crud.list_home_feed(
                db, limit=HOME_NEWS_LIMIT, exclude_pr=True
            ),
            "hide_adult": settings.news_hide_adult_content,
            "spotlight": spotlight,
            # 首页海报轮播数据（服务端名单 / 当季回落），见 app/home_posters.py
            "coverflow_items": coverflow_items,
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
    默认视图是网格（``view`` 不写参数 = 网格）：紧凑竖版海报 + 标题 + 元信息行，
    元信息随排序自适应（评分/收藏/年份哪个排序哪个前置）；``view=bili`` 是卡片视图
    （列更少、卡身留白更大，保留题材 chips 与摘要），``view=list`` 是横向行式列表。
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

    # 视图切换：网格（默认，不写参数）/ 卡片（view=bili）/ 列表（view=list）。
    # 网格是默认视图，所以它的链接刻意不带 view 参数——未选不写入 URL，
    # 既有分享链接与测试断言仍按旧形状（q/type/tags/creator/year）成立；
    # ``view=grid`` 依旧被接受（老链接仍落在网格视图上）。
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
        view=None,
    )
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
        view="bili",
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
            "code": "grid",
            "label": VIEW_LABELS["grid"],
            "selected": view in (None, "grid"),
            "url": grid_url,
        },
        {
            "code": "bili",
            "label": VIEW_LABELS["bili"],
            "selected": view == "bili",
            "url": bili_url,
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
            # view 是原始参数（None=网格默认，不写 URL）；active_view 供模板分支
            "view": view,
            "active_view": view or "grid",
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

# Staff 折叠区（执行 / 助理类职位）最多展示条数：infobox 里这类职位动辄三四十条，
# 全铺开会把「查看全部」变成一堵墙；够看、且不丢主创。
STAFF_EXTRA_LIMIT = 24


@dataclass(frozen=True)
class StaffRow:
    """Staff 表里的一行：既可能是 content_credits 的行，也可能是实体关系兜底出来的。

    模板只读 role / name / tier，用轻量结构就不用为了兜底写一个没入库的 ORM 对象。
    """

    role: str
    name: str
    tier: str = "main"
    sort_order: int = 999


def merge_entity_people_into_staff(credits, entity_roles) -> list:
    """Staff 兜底：credits 里没有的人，用人物实体关系补上。

    两层数据覆盖面不同：实体关联（导演 / 系列构成 / 音乐 / 角色设计 / 原作）
    是更早写入的一层，老作品未必重刷过 infobox，但至少能在 Staff 表里露头；
    重刷后 credits 的职位更精确，所以排前面、同名只留一条（按人名去重）。
    """
    rows = list(credits)
    known = {row.name for row in rows}
    for role, pairs in (entity_roles or {}).items():
        for name, entity_type in pairs:
            if entity_type != crud.ENTITY_TYPE_PERSON or name in known:
                continue
            known.add(name)
            rows.append(StaffRow(role=role, name=name))
    return rows


def split_staff_credits(credits, *, limit: int = STAFF_EXTRA_LIMIT):
    """Staff 分两段：主创（main）全部展示，执行 / 助理类（long）截断到 limit。

    候选（candidate，来自 persons 接口的原画长尾）不展示——它们在本站还没有
    足够作品积累，只留在库里等 promote_staff_candidates 转正。
    """
    main = [row for row in credits if row.tier == "main"]
    extra = [row for row in credits if row.tier == "long"]
    return main, extra[:limit]


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

    # 人物 / 公司分流（2026-10-22）：
    # 「基本信息·关系」以前把导演 / 系列构成 / 音乐 / 角色设计这些人名也塞进来，
    # 与右侧 Staff 表重复，且 Staff 表只剩副导演 / 脚本这类边角职位、常年为空。
    # 现在人一律进 Staff 表（姓名仍是 /creators/{name} 链接，标签反查能力保留），
    # 关系区只留公司 / 协会；作品制作公司另有「制作」行，这里再去重一次。
    entity_roles_typed = crud.list_content_entity_roles_typed(db, content_id)
    studios = crud.list_content_studios(db, content_id)
    studio_names = set(studios)
    content_org_roles: dict[str, list[str]] = {}
    for role, pairs in entity_roles_typed.items():
        # 「动画制作」已由「制作」行展示；只有在制作公司为空时才退回关系区显示
        if role == "动画制作" and studio_names:
            continue
        names = [name for name, entity_type in pairs if entity_type != crud.ENTITY_TYPE_PERSON]
        names = [name for name in names if name not in studio_names]
        if names:
            content_org_roles[role] = names
    # Staff 里哪些人名有实体页可跳（其余渲染成纯文本，不制造 404 链接）
    content_people_entities = {
        name
        for pairs in entity_roles_typed.values()
        for name, entity_type in pairs
        if entity_type == crud.ENTITY_TYPE_PERSON
    }
    # Staff 表：职位序在 bangumi_meta.STAFF_ROLE_RANK（导演 → 系列构成 → … → 执行类），
    # 历史数据的 sort_order 是旧的 4 职位序，必须再按职位名排一次才对。
    # credits 之外再用人物实体兜底：没重刷过 infobox 的老作品也不至于空。
    staff_credits = [
        row
        for row in crud.list_content_credits(db, content_id)
        if row.tier in ("main", "long")
    ]
    staff_credits = merge_entity_people_into_staff(staff_credits, entity_roles_typed)
    staff_rank = bangumi_meta.STAFF_ROLE_RANK
    staff_credits.sort(
        key=lambda row: (
            0 if row.tier == "main" else 1,
            staff_rank.get(row.role, len(staff_rank)),
            row.sort_order,
            getattr(row, "id", 0),
        )
    )
    staff_shown, staff_extra = split_staff_credits(staff_credits)

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
            "content_studios": studios,
            "content_staff": staff_credits,
            "content_staff_main": staff_shown,
            "content_staff_long": staff_extra,
            "content_cast": crud.list_content_cast(db, content_id, limit=60),
            "content_facts": crud.list_content_facts(db, content_id),
            "content_org_roles": content_org_roles,
            "content_people_entities": content_people_entities,
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
    # 每部作品附带 persona 行数据：配音角色（cast）+ 制作职责（credits）。
    # 卡片宏里 persona 存在即替代摘要——人物页里「他演了谁」比通用简介重要。
    for entry in rows:
        entry["persona"] = [
            *([("配音", entry["characters"])] if entry["characters"] else []),
            *([("职责", entry["roles"])] if entry["roles"] else []),
        ]
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
        # [(作品, [职责…])]：实体页每张卡显示该实体在这部作品里的职责
        contents = crud.list_entity_contents_with_roles(
            db, entity.id, limit=CREATOR_WORKS_LIMIT
        )
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
    # 第一页第一条单独做成头条位，剩下的才进网格。若只取 page_size 条，网格就是
    # page_size - 1 条 → 桌面 4 列时最后一行空一格（用户 2026-10-23 报「第一页少一个作品」）。
    # 所以这个视图每页多取 1 条：第一页是「1 条头条 + 满一页网格」，后续页按 page_size 顺延，
    # 总页数与偏移量都按同一个 extra 算（不能按当前页算，否则第 2 页口径会变）。
    # 搜索 / 非动画分类筛选没有头条位，按原样分页。
    featured_view = not q and (not category or category == "动画")
    extra = 1 if featured_view else 0
    total, articles = crud.list_articles_page(
        db,
        q=q or None,
        category=category or None,
        page=page,
        page_size=page_size,
        first_page_extra=extra,
    )
    total_pages = crud.articles_page_total_pages(total, page_size, extra)
    if total and page > total_pages:
        # 筛选后页码越界（如从第 5 页切到只有 1 页的分类）：夹回最后一页。
        page = total_pages
        total, articles = crud.list_articles_page(
            db,
            q=q or None,
            category=category or None,
            page=page,
            page_size=page_size,
            first_page_extra=extra,
        )
        total_pages = crud.articles_page_total_pages(total, page_size, extra)
    has_featured = featured_view and page == 1

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
            "has_featured": has_featured,
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


# 资讯主体 -> 分类色（颜色只做辅助识别，不为每篇文章单独配色）
_ARTICLE_CATEGORY_KEYS = (
    ("动画", "is-anime"),
    ("漫画", "is-comic"),
    ("轻小说", "is-novel"),
    ("游戏", "is-game"),
)


def article_category_class(category: str | None) -> str:
    """分类徽标的配色修饰类；认不出的分类走中性色（不猜、不硬套）。"""
    text = (category or "").strip()
    for keyword, modifier in _ARTICLE_CATEGORY_KEYS:
        if keyword and keyword in text:
            return modifier
    return "is-general"


def _drop_duplicate_lead_image(blocks: list[dict], hero_url: str) -> list[dict]:
    """去掉「头图字段」与「正文首图」指向同一张图的重复输出。

    导入时头图优先取列表页缩略图，正文首图常常就是同一张 keyvisual——
    页面先渲染封面、正文再渲染一次，读者看到的是同一张图连着两遍。
    只处理正文里的**第一张**图：图集、对比图里刻意重复的同图属于原文结构，
    不能按 URL 一把全删。
    """
    hero_key = article_body.image_key(hero_url)
    if not hero_key:
        return blocks
    for index, block in enumerate(blocks):
        if block.get("kind") != article_body.KIND_IMAGE:
            continue
        if article_body.image_key(block.get("url", "")) != hero_key:
            return blocks
        return blocks[:index] + blocks[index + 1 :]
    return blocks



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

    # 正文块：纯文本 / HTML 富文本都解析成同一套块结构（见 app/article_body）。
    # base_url 传原文地址，HTML 正文里的相对链接与图片才能补全成可跳转地址。
    hero_image = (article.image_url or "").strip()
    blocks = article_body.parse_blocks(
        article.body, base_url=article.original_url or ""
    )
    if hero_image:
        blocks = _drop_duplicate_lead_image(blocks, hero_image)

    # 收藏状态：资讯详情页是公开页（不要求登录），游客一律按未收藏渲染，
    # 点了按钮才弹登录窗（模板里的 data-login-gate）。
    current_user = getattr(request.state, "current_user", None)
    is_article_favorited = (
        crud.is_article_favorite(db, current_user.id, article_id)
        if current_user is not None
        else False
    )

    return templates.TemplateResponse(
        request=request,
        name="article_detail.html",
        context={
            "article": article,
            "lead": lead,
            "is_article_favorited": is_article_favorited,
            # 正文块：文本段 / 标题 / 列表 / 引用 / 表格 / 图片 / 媒体
            "body_blocks": blocks,
            "hero_image": hero_image,
            "category_class": article_category_class(article.category),
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


@router.post("/articles/{article_id}/favorite")
def toggle_article_favorite(
    request: Request,
    article_id: int,
    db: DbSession,
    next: str = Form(default=""),
):
    """资讯收藏 / 取消收藏（切换）；资讯不存在走统一 404。

    与作品收藏同一个套路（POST + PRG 303 + ``?fav=`` 回显），区别只有两点：
    资讯详情页是公开页，所以登录判断放在函数里而不是路由级守卫
    （游客点按钮由 ``LoginRequired`` 带 ``next`` 跳登录，登录后回本页）；
    资讯没有「标注」概念，取消收藏就是删行，不存在留空壳行的分支。
    """
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise LoginRequired(next_url=f"/articles/{article_id}")
    if crud.get_article(db, article_id) is None:
        return render_missing(request, "资讯", article_id)
    if crud.is_article_favorite(db, current_user.id, article_id):
        crud.remove_article_favorite(db, current_user.id, article_id)
        result = "removed"
    else:
        crud.add_article_favorite(db, current_user.id, article_id)
        result = "added"
    # next 经 safe_next_url 校验（只允许站内相对路径），拼 fav 回参后 303 回跳。
    target = safe_next_url(next) or f"/articles/{article_id}"
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}fav={result}", status_code=303)

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


# ---------- 特殊分类：按播出/上映日期分组（时间线） ----------

# 既无 air_date 也无播出说明的条目归入这一组，永远排在最后。
DATE_UNKNOWN_GROUP = ""
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
ISO_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

# 剧场类特殊分类按月分组（用户 2026-10-24 拍板）：一部电影一个上映日，按日分会
# 碎成大量单条组——线上实测 cn_movie 76 部 71 组、ova_sp 363 部 238 组，时间线
# 稀碎看不出「这个月上了哪些片」。按月归拢后组数降到个位数~十几条，当月片单一
# 目了然。未上线新番（unreleased）不在此列：倒计时到天对追番有意义，保持按日。
MONTH_GROUP_CATEGORIES = frozenset({"ova_sp", "cn_movie"})


def _date_group_name(key: str, by_month: bool = False) -> str:
    """分组键转展示名。

    按日：ISO 日期 → 「2026年12月11日」；按月：ISO 月键 → 「2026年11月」。
    原文（如 2027冬、TV放送：9/2-9/9）两种粒度都保留；空键 → 「时间待定」。
    """
    if by_month:
        match = ISO_MONTH_RE.match(key)
        if match:
            return f"{int(match.group(1))}年{int(match.group(2))}月"
        return key or "时间待定"
    match = ISO_DATE_RE.match(key)
    if match:
        return f"{int(match.group(1))}年{int(match.group(2))}月{int(match.group(3))}日"
    return key or "时间待定"


def _date_countdown(iso: str) -> str | None:
    """距今天数文案：未来「还有 N 天」、过去「N 天前」、当天「今天」。"""
    try:
        target = date.fromisoformat(iso)
    except ValueError:
        return None
    delta = (target - date.today()).days
    if delta == 0:
        return "今天"
    if delta > 0:
        return f"还有 {delta} 天"
    return f"{abs(delta)} 天前"


def _group_by_date(works: list, by_month: bool = False) -> list[dict]:
    """把特殊分类作品按播出/上映日期分组：新的日期在前，无日期的按原文归组。

    ``by_month=True``（剧场类 ova_sp / cn_movie，见 MONTH_GROUP_CATEGORIES）时
    分组键取到月：air_date 前 7 位 → 「2026年11月」。电影一部一个上映日，按日
    分组会碎成大量单条组，按月归拢才看得出「这个月上了哪些片」。月组不派生
    周几与距今天数——月份不是时间点，组头只留组名 + 条数（模板对两个字段判空，
    自动隐藏）。

    缺失 air_date 时回退 broadcast_note 原文（如「2027冬」只描述季度、
    「TV放送：9/2-9/9」是源站的特殊说明）——不猜具体日期，源站怎么说就怎么归组。
    「时间待定」组（既无日期也无说明）固定排最后。
    组内顺序沿用查询排序（air_date 倒序 + 主标题），不做二次调整。
    """
    buckets: dict[str, list] = {}
    for item in works:
        air = (item.air_date or "").strip()
        if by_month and ISO_DATE_RE.match(air):
            key = air[:7]  # 2026-11-27 → 2026-11（月粒度）
        else:
            key = air or (item.broadcast_note or "").strip()
        key = key or DATE_UNKNOWN_GROUP
        buckets.setdefault(key, []).append(item)

    groups: list[dict] = []
    # 字符串倒序天然即「日期新的在前」：air_date 是定长 ISO 前缀，季度描述
    # （2027冬）以年份开头同规则参与比较；空键（时间待定）最小，排最后。
    for key in sorted(buckets, reverse=True):
        members = buckets[key]
        weekday_name = None
        countdown = None
        match = None if by_month else ISO_DATE_RE.match(key)
        if match:
            try:
                weekday_index = date(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))
                ).weekday() + 1  # 周一=1 … 周日=7
            except ValueError:
                weekday_index = None
            if weekday_index in WEEKDAY_NAMES:
                weekday_name = WEEKDAY_NAMES[weekday_index]
            countdown = _date_countdown(key)
        groups.append(
            {
                "key": key,
                "name": _date_group_name(key, by_month=by_month),
                "weekday_name": weekday_name,
                "countdown": countdown,
                # 键名用 works 而非 items：dict.items 是内建方法，模板属性访问会撞名
                "works": members,
            }
        )
    return groups


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
    """新番档期页：季度导航 / 特殊分类 tab、按星期/日期分组浏览与关键词/星期筛选。

    两种浏览维度互斥：给定 category（unreleased / ova_sp / cn_movie）时按特殊分类
    展示；否则按季度档期（season=YYYYMM）展示。两者内部都支持关键词/星期筛选。
    默认视图的分组维度随维度切换：季度档期按播出星期分组（周更时间表），
    特殊分类按播出/上映日期分组（时间线，新的在前）——未上线新番与剧场版
    没有星期概念，源站本身也按日期排版；其中剧场类（ova_sp / cn_movie）分组
    到月（一部电影一个上映日，按日碎组，见 MONTH_GROUP_CATEGORIES），未上线
    新番仍按日（倒计时到天对追番有意义）。
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
    date_groups: list[dict] = []
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
            # 特殊分类默认视图：按播出/上映日期分组的时间线（不按星期——星期对
            # 未上线新番/剧场版没有意义，源站本身也是按日期排版）。
            # 分组粒度按分类走：剧场类（ova_sp / cn_movie）到月，未上线新番到日
            # （见 MONTH_GROUP_CATEGORIES 的由来）。
            works = crud.list_all_schedules(db, category=category)
            total = len(works)
            date_groups = _group_by_date(
                works, by_month=category in MONTH_GROUP_CATEGORIES
            )
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
            "date_groups": date_groups,
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
