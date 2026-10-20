"""Bangumi 增量入库 API：本机抓取 → 服务器入库的唯一通道。

为什么需要它（2026-10-18 定型）：
    服务器（Lighthouse，ap-beijing-3）**没有境外出网**，api.bgm.tv / bgm.tv
    在服务器上直接 Network is unreachable（国内源如 yuc.wiki、news.aniplex.co.jp
    则可达，已实测）。所以作品数据这一类必须「本机有代理时抓取，服务器负责入库」。

设计要点：
- **传 payload 而不是传 id**：抓取端（本机）已把 Bangumi 原始 JSON 拿到手，直接 POST
  过来；服务器不再触网，只做映射、查重、标签归一、关系识别、入库——也就是复用
  ``scripts/import_bangumi._map_and_store`` 那条既有路径，不重写一套入库逻辑。
- **鉴权用 API Key 而不是 Cookie**：抓取端是脚本不是浏览器。``X-Ingest-Key``
  请求头 + ``INGEST_API_KEY`` 配置（未配置时端点直接 503，等于默认关闭）。
  错误不区分「未配置 / key 不对」以外的信息，避免被用来探测。
- **增量与幂等**：``_map_and_store`` 已实现「bangumi_id 命中即 duplicate 跳过」，
  所以重复推送安全；本模块只负责传输、限流、审计。
- **失败不阻塞整批**：单条失败照样返回 200 + 明细，由调用方决定是否重试，
  避免大 batch 因一条坏数据全废。

用法（服务器侧配置）：
    INGEST_API_KEY=<随机长串>   # 放生产 .env，chmod 600

用法（抓取端）：
    curl -X POST http://<host>:<port>/api/ingest/subjects \\
      -H "X-Ingest-Key: <key>" -H "Content-Type: application/json" \\
      -d @payloads.json
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import require_owner_api
from app.config import settings
from app.database import get_db
from scripts.import_bangumi import _map_and_store

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

DbSession = Annotated[Session, Depends(get_db)]

# 单次请求最多接收多少个条目：批量导入按几十条推，1000 足够且能挡住滥用。
MAX_PAYLOAD_ITEMS = 1000


def _configured_key() -> str:
    return (settings.ingest_api_key or "").strip()


def require_ingest_key(
    x_ingest_key: Annotated[str | None, Header(alias="X-Ingest-Key")] = None,
) -> str:
    """校验入库 API Key；未配置 503、缺失或不匹配 401。

    用 ``compare_digest`` 做定长比较，避免按键值长度泄露信息。
    """
    expected = _configured_key()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="入库通道未启用：服务器未配置 INGEST_API_KEY",
        )
    provided = (x_ingest_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="入库 API Key 不正确")
    return provided


@router.post("/subjects")
def ingest_subjects(
    payload: Annotated[Any, Body()],
    db: DbSession,
    _key: Annotated[str, Depends(require_ingest_key)],
    user: Annotated[Any, Depends(require_owner_api)],
) -> dict[str, Any]:
    """接收本机抓好的 Bangumi 条目 payload 并入库，返回逐条结果。

    请求体：``{"subjects": [ <Bangumi subject JSON>, ... ], "source": "local-fetch"}``

    - 只有站长能调（``require_owner_api``）；Key 校验先于角色，顺序即依赖声明顺序。
    - 请求体声明为 ``Any`` + ``Body()`` 而非 ``dict[str, Any]``：抓取端是脚本，可能混进
      非对象的坏数据，这类条目要能走到端点里被逐条跳过，而不是被 FastAPI 的
      类型校验整体挡成 422。
    - 逐条复用 ``_map_and_store``，命中既有 bangumi_id 记 duplicate 跳过，不覆盖。
    - 不在本端下载封面：封面 CDN 同样在境外，本端无出网。封面由抓取端下载后
      单独回填，避免在这里静默失败。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

    items = payload.get("subjects")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="请求体缺少 subjects 数组")
    if not items:
        return {"total": 0, "imported": 0, "results": []}
    if len(items) > MAX_PAYLOAD_ITEMS:
        raise HTTPException(
            status_code=413,
            detail=f"单次最多接收 {MAX_PAYLOAD_ITEMS} 个条目，本次 {len(items)} 个",
        )

    results: list[dict[str, Any]] = []
    # 逐条处理，复用 _map_and_store 自己的 commit / rollback 语义：
    # 单条失败不影响其余条目。
    for item in items:
        if not isinstance(item, dict):
            results.append(
                {
                    "bangumi_id": None,
                    "status": "skipped",
                    "title": "",
                    "reason": "不是合法的对象，已跳过",
                }
            )
            continue
        outcome = _map_and_store(db, item, apply=True, cover_downloader=None)
        results.append(outcome.as_dict())
        # 服务器没有境外出网，封面永远无法在服务器端下载；低热作品走「按需下载」：
        # 把 Bangumi 原图地址记进 remote_image_url（幂等补列，见 crud），详情页
        # 第一次被访问时由 media.fetch_cover_on_demand 补齐。服务器当前无外网时
        # 按需下载会静默失败回退占位图——封面文件由本机批量上传（backfill 脚本）。
        if outcome.status == "imported" and outcome.bangumi_id:
            try:
                _record_remote_cover(db, outcome.bangumi_id, item)
            except SQLAlchemyError:
                db.rollback()

    imported = sum(1 for r in results if r["status"] == "imported")
    actor = getattr(user, "username", None)
    return {
        "total": len(results),
        "imported": imported,
        "source": payload.get("source") or "local-fetch",
        "actor": actor,
        "results": results,
    }


def _record_remote_cover(db: Session, bangumi_id: int, item: dict) -> None:
    """把 Bangumi 原图地址记到 contents.remote_image_url（已有本地封面则跳过）。

    medium 优先（实测比 large 小 12–30%），与抓取端 COVER_SIZE_PREFERENCE 一致。
    失败只回滚本条记录，不影响批次里其余条目。
    """
    from sqlalchemy import select

    from app.models import Content

    content = db.scalar(select(Content).where(Content.bangumi_id == bangumi_id))
    if content is None or content.image_url:
        return
    images = item.get("images")
    remote = None
    if isinstance(images, dict):
        for size in ("medium", "common", "large"):
            url = images.get(size)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                remote = url
                break
    if remote:
        content.remote_image_url = remote
        db.commit()
