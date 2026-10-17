"""旧地址兼容：把历史上属于「测试平台 → 数据管理」的 URL 跳到新位置。

2026-10-18 把内容数据管理迁回内容平台，路径从 /admin/data* 改为 /manage*，
同步历史从 /schedule/runs 改为 /manage/sync/runs。旧书签、外部链接、旧测试
仍可能指向旧地址，这里统一跳转，不承载任何业务逻辑。

GET 用 302（临时跳转，便于日后调整），POST 用 307（保留方法与表单体，
避免旧页面提交的删除 / 同步请求变成 GET）。
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["legacy"], include_in_schema=False)


def _redirect(request: Request, target: str, *, keep_method: bool) -> RedirectResponse:
    """带上原查询串跳转；POST 用 307 保留方法，其余用 302。"""
    query = request.url.query
    location = f"{target}?{query}" if query else target
    return RedirectResponse(location, status_code=307 if keep_method else 302)


@router.api_route("/admin/data", methods=["GET", "POST"])
def legacy_data_root(request: Request):
    """旧的数据管理总览 → /manage。"""
    return _redirect(request, "/manage", keep_method=request.method != "GET")


@router.api_route("/admin/data/{rest:path}", methods=["GET", "POST"])
def legacy_data_path(request: Request, rest: str):
    """旧的数据管理子页面 / 表单提交 → /manage/...。"""
    return _redirect(request, f"/manage/{rest}", keep_method=request.method != "GET")


@router.get("/schedule/runs")
def legacy_schedule_runs(request: Request):
    """旧的同步历史页 → /manage/sync/runs。"""
    return _redirect(request, "/manage/sync/runs", keep_method=False)
