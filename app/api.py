"""面向自动化测试的 JSON API 路由（数据来源：SQLAlchemy 数据库访问层）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import crud
from app.auth import require_admin_api
from app.database import get_db

# 复用的依赖注解：模块级定义一次，各端点签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/api", tags=["api"])

# ACGN 内容类型全集：列表筛选与搜索的类型校验共用同一份定义。
CONTENT_TYPES = ("anime", "comic", "game", "novel")


@router.get("/health")
def health() -> dict:
    """健康检查：供监控脚本和接口冒烟测试使用。"""
    return {"status": "ok", "service": "acgn-test-platform"}

@router.get("/contents")
def list_contents(
    db: DbSession,
    content_type: str | None = Query(default=None, alias="type"),
) -> dict:
    """返回内容列表；可按 ACGN 类型筛选。"""
    if content_type is not None:
        content_type = content_type.strip().lower()
        if content_type not in CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail="类型不合法，可选值：anime、comic、game、novel",
            )

    items = crud.list_contents(db, content_type=content_type)
    return {"total": len(items), "items": [item.as_dict() for item in items]}

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


@router.get("/contents/{content_id}")
def get_content(content_id: int, db: DbSession) -> dict:
    """按 id 获取内容详情；不存在时返回 404。"""
    item = crud.get_content(db, content_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"内容不存在: id={content_id}")
    return item.as_dict()


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
