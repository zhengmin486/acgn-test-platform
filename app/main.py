"""FastAPI 应用入口。

JSON API 与页面数据由 SQLAlchemy 数据层提供（app/crud.py）。
连接配置从环境变量 / .env 读取（app/config.py）；
目标数据库为 MySQL，本机服务就绪前默认 SQLite 用于本地开发与验证。

横切设施（开发期优化）：日志级别可配（logging_setup）、安全响应头与
静态资源缓存走配置、上传目录容量巡检、未处理异常统一脱敏 500（堆栈
只进服务端日志，不返回前端）。
"""

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import (
    account_pages,
    api,
    auth,
    business_pages,
    cases,
    content_admin_pages,
    cover_admin,
    crud,
    ingest_api,
    legacy_redirects,
    media,
    persona_pages,
    site_admin_pages,
    space_pages,
    submission_pages,
    testing_pages,
)
from app.auth import (
    LoginRequired,
    PermissionDenied,
    load_current_user,
    login_required_handler,
    permission_denied_handler,
    require_admin_api,
    require_admin_page,
)
from app.config import settings
from app.database import SessionLocal, engine
from app.logging_setup import configure_logging
from app.page_common import templates

logger = logging.getLogger(__name__)

# 日志最先装配：后续初始化（建表 / 容量巡检）的告警才能按配置的级别输出。
configure_logging()

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Otaku · ACGN 内容平台与接口自动化测试后台",
    description="Otaku 是一个 ACGN 内容平台（作品 / 资讯 / 档期），同进程内含一套接口回归测试后台"
    "（数据已接入数据库层，本地验证默认 SQLite）。",
    version="0.2.0",
    # 应用级依赖：每个请求解析一次当前登录用户（含内容页面），供模板与守卫复用。
    dependencies=[Depends(load_current_user)],
)


# ---------- 安全响应头（走配置，可整体关闭） ----------


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    if settings.security_headers_enabled:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if settings.x_frame_options:
            response.headers.setdefault("X-Frame-Options", settings.x_frame_options)
    return response


# ---------- 静态资源与图片缓存（秒数走配置；0 表示不输出） ----------

_STATIC_PREFIXES = ("/static/",)
_MEDIA_PREFIXES = ("/covers/", "/avatars/", "/article-images/", "/media/")


@app.middleware("http")
async def _cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith(_STATIC_PREFIXES) and settings.static_cache_max_age > 0:
        response.headers.setdefault(
            "Cache-Control", f"public, max-age={settings.static_cache_max_age}"
        )
    elif path.startswith(_MEDIA_PREFIXES) and settings.media_cache_max_age > 0:
        response.headers.setdefault(
            "Cache-Control", f"public, max-age={settings.media_cache_max_age}"
        )
    return response


if settings.gzip_enabled:
    from fastapi.middleware.gzip import GZipMiddleware

    # 默认关闭（流量大头是已压缩的 WebP，压缩它们只费 CPU）；配置开启后生效。
    app.add_middleware(
        GZipMiddleware,
        minimum_size=settings.gzip_minimum_size,
        compresslevel=settings.gzip_compress_level,
    )


# ---------- 未处理异常的统一 500：堆栈进日志，前端只拿脱敏响应 ----------


async def _server_error_handler(request: Request, exc: Exception):
    # 服务端留全量堆栈（排障用）；客户端按区域拿通用文案，不泄露任何内部细节。
    logger.exception("未处理异常：%s %s", request.method, request.url.path)
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "服务器内部错误"}, status_code=500)
    return templates.TemplateResponse(
        request=request,
        name="500.html",
        context={},
        status_code=500,
    )


app.include_router(auth.router)
app.include_router(api.router)
# 增量入库通道（/api/ingest/*）：本机抓 Bangumi 后回传服务器入库。
# 鉴权在端点内（X-Ingest-Key + 站长），不在这里挂 require_admin_api——
# 抓取端是脚本、没有站内登录态。未配置 INGEST_API_KEY 时端点一律 503。
app.include_router(ingest_api.router)

# 测试用例 / 执行记录 / 批次 API：仅管理员可访问（未登录 401、越权 403）。
app.include_router(cases.router, dependencies=[Depends(require_admin_api)])
app.include_router(business_pages.router)
# 个人空间页面（/me）：内容平台的登录用户功能，未登录跳登录页（路由内部校验）。
app.include_router(space_pages.router)
# 账号设置（/settings/profile、/settings/security）：个人资料 + 邮箱 / 手机的绑定解绑。
# 从 /me 独立成页（主流站点的「头像菜单 → 账号设置」结构），入口见 base_content.html。
app.include_router(account_pages.router)
# 作品申请（/submit、/contents/{id}/edit）：登录用户提交新增 / 修改提案，等待审核。
app.include_router(submission_pages.router)
# 封面图片上传（/manage/contents/{id}/cover，仅 staff）：管理员给作品补图 / 换图，
# 与导入落地的封面同一条 app/media.py 管线（校验 → 落盘 → 双尺寸派生）。
app.include_router(cover_admin.router)
# 御宅成分检测（/me/persona）：登录用户绑定 Bangumi 用户名、同步公开收藏并生成画像。
app.include_router(persona_pages.router)
# 免登录试算（/persona）：引流入口，不写库、不建账号，只渲染当次结果。
app.include_router(persona_pages.public_router)
# 测试管理平台页面：仅管理员可访问（未登录跳登录页、越权 403）。
app.include_router(testing_pages.router, dependencies=[Depends(require_admin_page)])
# 内容管理页面（内容平台 → 内容管理，/manage/*）：同样仅管理员可访问。
app.include_router(content_admin_pages.router, dependencies=[Depends(require_admin_page)])
# 站点设置页面（内容平台 → 站点设置，/admin/*）：仅管理员可访问；
# 角色管理在路由内再叠一层 require_owner_page（仅站长）。
app.include_router(site_admin_pages.router, dependencies=[Depends(require_admin_page)])
# 旧地址兼容：/admin/data* 与 /schedule/runs 跳转到新地址。
# 放在最后，避免 catch-all 抢在真实路由之前匹配。
app.include_router(legacy_redirects.router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# 封面图静态目录：Bangumi 导入时下载到 cover_storage_dir（配置可为项目内
# 相对路径或其它盘符的绝对路径），统一挂到 /covers，Content.image_url 存
# /covers/<文件名> 引用。目录不存在则创建，保证首次导入前挂载就有效。
# 改用 media.cover_root()：与容量巡检 / 上传落盘共用同一个「相对路径按项目根
# 解析」的口径，避免 main 与 media 各算一套。
COVER_DIR = media.cover_root()
COVER_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/covers", StaticFiles(directory=COVER_DIR), name="covers")

# 用户头像目录：与封面同一套「可指到其它盘符」的配置，挂到 /avatars。
# User.avatar_url 存 /avatars/<文件名>；目录不存在则建，保证首次上传前挂载有效。
AVATAR_DIR = media.avatar_root()
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=AVATAR_DIR), name="avatars")

# 资讯图片目录：正文图与头图本地化后落盘，Article.image_url / 正文 [图] 行
# 存 /article-images/<文件名>。同样支持指到其它盘符。
ARTICLE_IMAGE_DIR = Path(settings.article_image_storage_dir)
if not ARTICLE_IMAGE_DIR.is_absolute():
    ARTICLE_IMAGE_DIR = BASE_DIR.parent / ARTICLE_IMAGE_DIR
ARTICLE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/article-images", StaticFiles(directory=ARTICLE_IMAGE_DIR), name="article-images")

# 页面级拒绝的统一处理：未登录 -> 登录页；权限不足 -> 403 页面。
app.add_exception_handler(LoginRequired, login_required_handler)
app.add_exception_handler(PermissionDenied, permission_denied_handler)
# 未处理异常的兜底 500：页面与 API 分别给脱敏响应（handler 定义见上）。
app.add_exception_handler(Exception, _server_error_handler)

# 启动即初始化：建表（如无）并播种开发/测试数据；幂等，表已有数据时不覆盖。
# 放在导入期执行，保证 uvicorn 与 TestClient 都拿到就绪的数据库；生产化后换 Alembic。
crud.init_db(engine)

# 启动即做一次上传目录巡检：清崩溃遗留的 .tmp-* 临时文件；目录总量超配置
# 上限时先删可再生派生图、再删未被引用的文件（被 contents/users 引用的绝不动）。
# 之后由上传路径按节流触发（page_common.maybe_enforce_storage_caps）。
try:
    with SessionLocal() as _sweep_db:
        _sweep = media.enforce_storage_caps(
            referenced_covers=crud.list_cover_reference_names(_sweep_db),
            referenced_avatars=crud.list_avatar_reference_names(_sweep_db),
        )
    _removed = (
        _sweep["covers"]["removed_variants"]
        + _sweep["covers"]["removed_unreferenced"]
        + _sweep["avatars"]["removed_unreferenced"]
    )
    if _removed:
        logger.warning(
            "启动容量巡检清理了 %d 个文件（临时残留 %d 个）",
            _removed,
            _sweep["purged_temp_files"],
        )
except Exception:  # 容量巡检失败不阻断启动，堆栈只进日志
    logger.exception("启动上传目录巡检失败（不影响启动）")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
