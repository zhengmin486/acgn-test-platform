"""封面上传路由（内容平台 → 内容管理，/manage/contents/{id}/cover，仅 staff）。

作品封面的「补图 / 换图」入口：详情页表单 POST 到这里，校验后落盘并生成
缩略图 / 全图——与 Bangumi 导入落地的封面走同一条图片管线（app/media.py），
写回 Content.image_url。

设计约束：
- 路由级守卫 require_admin_page（staff：管理员 / 站长）：游客跳登录、越权 403；
- 文件经大小 / 魔术字节 / 可解码三道校验（app/media.py），落盘名称由服务端
  生成，天然免疫路径穿越；
- 被替换掉的旧封面文件（本站生成的）随之清理，不占服务器空间；
- 写操作统一 PRG：303 回跳 next（默认作品详情页），cover=ok|error 加 why 代码回显。
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import UploadFile

from app import crud, media
from app.auth import require_admin_page
from app.config import settings
from app.page_common import DbSession, maybe_enforce_storage_caps, render_missing

router = APIRouter(tags=["cover-admin"])


def _form_text(form, key: str) -> str:
    """表单字段取文本：缺失按空串处理（与管理页表单助手同语义）。"""
    value = form.get(key)
    return str(value).strip() if value is not None else ""


def _url_with(base: str, **params) -> str:
    """在 base 的查询串上追加参数并保留原有参数（PRG 回显用）。"""
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items()})
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


@router.post(
    "/manage/contents/{content_id}/cover",
    dependencies=[Depends(require_admin_page)],
)
async def upload_content_cover(request: Request, content_id: int, db: DbSession):
    """上传作品封面：校验后落盘并生成缩略图 / 全图，写回 image_url。"""
    form = await request.form()
    next_url = _form_text(form, "next") or f"/contents/{content_id}"
    if not next_url.startswith("/"):
        next_url = f"/contents/{content_id}"

    content = crud.get_content(db, content_id)
    if content is None:
        return render_missing(request, "作品数据", content_id)

    upload = form.get("cover")
    # 注意：request.form() 返回的是 starlette 的 UploadFile，与 fastapi.UploadFile
    #（FastAPI 自建子类）不是同一个类，isinstance 必须按 starlette 的判断。
    if not isinstance(upload, UploadFile) or not upload.filename:
        return RedirectResponse(
            _url_with(next_url, cover="error", why="empty"), status_code=303
        )
    try:
        # 只读「上限 + 1KB」：超大文件不会整个进内存，超限立即被 save_upload 拒绝；
        # 读完即关，释放 multipart 落盘的临时文件。
        data = await upload.read(settings.cover_upload_max_bytes + 1024)
    finally:
        await upload.close()
    try:
        local_url = media.save_upload(data, content_id)
    except media.UploadError as exc:
        return RedirectResponse(
            _url_with(next_url, cover="error", why=exc.code), status_code=303
        )

    previous = content.image_url
    content.image_url = local_url
    db.commit()
    media.remove_cover_file(previous)
    maybe_enforce_storage_caps()
    return RedirectResponse(_url_with(next_url, cover="ok"), status_code=303)
