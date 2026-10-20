"""面向自动化测试的 JSON API 路由（数据来源：SQLAlchemy 数据库访问层）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import crud, media
from app.auth import require_admin_api
from app.database import get_db

# 复用的依赖注解：模块级定义一次，各端点签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/api", tags=["api"])

# ACGN 内容类型全集：列表筛选与搜索的类型校验共用同一份定义。
# 「文学」不是类型，是小说条目上的标注（contents.book_kind），故不在全集里。
CONTENT_TYPES = ("anime", "comic", "game", "novel")

# /api/contents 未显式分页时的默认返回上限（DB 级 limit）。
# 此前不传 page/page_size 会一次性把筛选后的全量行物化成 ORM 对象并返回，
# 1.7 万部时单请求峰值内存与响应体都很大；默认限到一页，全量导出走显式 ?all=1。
API_LIST_DEFAULT_PAGE_SIZE = 500


@router.get("/health")
def health() -> dict:
    """健康检查：供监控脚本和接口冒烟测试使用。"""
    return {"status": "ok", "service": "acgn-test-platform"}

@router.get("/contents")
def list_contents(
    db: DbSession,
    content_type: str | None = Query(default=None, alias="type"),
    tag: str | None = Query(default=None),
    tags: str | None = Query(default=None, max_length=200),
    year: int | None = Query(default=None, alias="year", ge=1900, le=2100),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=500),
    all_rows: bool = Query(default=False, alias="all"),
) -> dict:
    """返回内容列表；可按 ACGN 类型、标签与年份筛选（三者可叠加）。

    ``tag`` 为单标签；``tags`` 为多标签（逗号分隔，AND 语义，见
    crud.parse_tag_query）。``year`` 命中 ``contents.year``（发行年），
    与题材标签相互独立。三者可叠加，内部合并去重。

    分页（DB 级，避免一次性物化全量行）：
    - ``page`` + ``page_size`` 都传 → 按该页返回，响应含 ``page`` /
      ``page_size`` / ``total_pages`` 回显，``total`` 为筛选后全量条数。
    - 都不传 → 默认按 ``API_LIST_DEFAULT_PAGE_SIZE`` 返回第一页（同样带
      分页回显），彻底消除「一次请求扛全量 ORM 对象」的内存尖峰。
    - ``all=1`` → 显式全量返回（不带分页键，响应结构为 ``{total, items}``），
      供管理端/备份等需要逐条遍历的场景；这是唯一会一次性返回全部的路径，属
      主动选择，不再是默认行为。

    每个 item 除原有 id/title/type/summary/rating 外，附加输出
    ``cover_url``（压缩全图地址，空串=无封面）、``tags``（题材标签名
    列表）、``year``、``bangumi_id``（Bangumi 溯源 id，封面文件名即
    cover_{bangumi_id}.jpg）——全部为新增字段，老字段取值不变。
    """
    if content_type is not None:
        content_type = content_type.strip().lower()
        if content_type not in CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail="类型不合法，可选值：anime、comic、game、novel",
            )

    parsed_tags = crud.parse_tag_query(tags, tag)
    total = crud.count_list_contents(
        db, content_type=content_type, tag=tag, tags=parsed_tags, year=year
    )

    if page is not None and page_size is not None:
        cur_page, cur_size = page, page_size
        paginated = True
    elif all_rows:
        paginated = False
    else:
        cur_page, cur_size = 1, API_LIST_DEFAULT_PAGE_SIZE
        paginated = True

    if paginated:
        items = crud.list_contents(
            db,
            content_type=content_type,
            tag=tag,
            tags=parsed_tags,
            year=year,
            skip=(cur_page - 1) * cur_size,
            limit=cur_size,
        )
    else:
        items = crud.list_contents(
            db,
            content_type=content_type,
            tag=tag,
            tags=parsed_tags,
            year=year,
        )

    tag_map = crud.list_content_tags_map(db, [item.id for item in items])
    response: dict = {
        "total": total,
        "items": [_content_payload(item, tag_map.get(item.id, [])) for item in items],
    }
    if paginated:
        response["page"] = cur_page
        response["page_size"] = cur_size
        response["total_pages"] = (total + cur_size - 1) // cur_size
    return response


def _content_payload(item, tag_names: list[str]) -> dict:
    """API 列表/详情共用的条目序列化：as_dict 契约字段 + 附加展示字段。"""
    payload = item.as_dict()
    payload["cover_url"] = media.full_src(item.image_url)
    payload["tags"] = tag_names
    payload["year"] = item.year
    payload["bangumi_id"] = item.bangumi_id
    return payload

@router.get("/contents/search")
def search_contents(
    db: DbSession,
    q: str = Query(default=""),
    content_type: str | None = Query(default=None, alias="type", max_length=20),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """按标题/简介模糊搜索（不区分大小写），可叠加类型筛选与分页。

    q 为空或纯空白 -> 400；无匹配 -> 200 + 空列表。
    type/page/page_size 为可选参数；响应中的 type/page/page_size/total_pages
    为回显与翻页信息，原有 total/items 契约不变。
    注意：本路由必须注册在 /contents/{content_id} 之前，
    否则 "search" 会被当作 path 参数匹配。
    """
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="搜索参数 q 不能为空")

    if content_type is not None:
        content_type = content_type.strip().lower()
        if content_type not in CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail="类型不合法，可选值：anime、comic、game、novel",
            )

    total, items = crud.search_contents_page(
        db,
        keyword=keyword,
        content_type=content_type,
        page=page,
        page_size=page_size,
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "type": content_type,
        "items": [item.as_dict() for item in items],
    }


# ---------- 标签字典（供外部同步方枚举标签名，避免逐个猜） ----------


@router.get("/tags")
def list_tags(
    db: DbSession,
    content_type: str | None = Query(default=None, alias="type", max_length=20),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict:
    """标签全集（名称 + 关联作品数），按作品数从多到少。

    ``type`` 限定某类内容（anime/comic/game/novel）；不传则跨类型汇总，
    此时同名标签的计数会合并，且动画/游戏的专属标签会一起出现——同步方
    按类型取更准。数据源是 ``content_tags``，含少量形式词（TV/漫画改等），
    属真实标签分布，不做清洗；调用方自行按需过滤。
    """
    if content_type is not None:
        content_type = content_type.strip().lower()
        if content_type not in CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail="类型不合法，可选值：anime、comic、game、novel",
            )
    rows = crud.list_tags_with_counts(db, limit=limit, content_type=content_type)
    return {
        "type": content_type,
        "total": len(rows),
        "items": [{"name": name, "count": count} for name, count in rows],
    }


@router.get("/contents/{content_id}")
def get_content(content_id: int, db: DbSession) -> dict:
    """按 id 获取内容详情；不存在时返回 404。

    在 as_dict 契约字段之外附加 cover_url / tags / year / bangumi_id
    （与列表端点一致，见 list_contents 的 docstring）。
    """
    item = crud.get_content(db, content_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"内容不存在: id={content_id}")
    return _content_payload(item, crud.list_content_tags(db, content_id))


# ---------- 新番档期（只读：数据来自 scripts/sync_yuc_schedule.py 同步） ----------

WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


@router.get("/schedule")
def list_schedule(
    db: DbSession,
    season: str | None = Query(default=None, max_length=6),
    q: str | None = Query(default=None, max_length=100),
    weekday: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """分页查询新番档期作品；可按档期、星期（1-7）、关键词筛选。

    不传 season 时返回全部档期数据；仅读取已同步数据，不实时抓取外部网站。
    """
    if weekday is not None and weekday not in WEEKDAY_NAMES:
        raise HTTPException(
            status_code=400, detail="星期不合法，可选值 1-7（1=周一 … 7=周日）"
        )
    keyword = (q or "").strip() or None
    total, items = crud.list_schedules(
        db,
        season=season,
        keyword=keyword,
        weekday=weekday,
        page=page,
        page_size=page_size,
    )
    return {
        "season": season,
        "keyword": keyword,
        "weekday": weekday,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "items": [item.as_dict() for item in items],
    }


@router.get("/schedule/seasons")
def list_schedule_seasons(db: DbSession) -> dict:
    """已同步的档期列表（含作品数与最近同步时间），按档期新到旧排序。"""
    seasons = crud.list_schedule_seasons(db)
    return {"total": len(seasons), "seasons": seasons}


@router.get("/schedule/runs", dependencies=[Depends(require_admin_api)])
def list_schedule_runs(
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """只读查询同步运行记录（最新在前）；dry-run 与 apply 记录同表区分。

    同步记录属于管理数据，仅管理员可访问（未登录 401 / 越权 403）。
    """
    total, runs = crud.list_sync_runs(db, limit=limit)
    return {"total": total, "limit": limit, "runs": [run.as_dict() for run in runs]}


@router.get("/schedule/{schedule_id}")
def get_schedule(schedule_id: int, db: DbSession) -> dict:
    """按 id 获取单条档期作品；不存在时返回 404。"""
    item = crud.get_schedule(db, schedule_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"档期作品不存在: id={schedule_id}"
        )
    return item.as_dict()
