"""FastAPI 应用入口。

JSON API 与页面数据由 SQLAlchemy 数据层提供（app/crud.py）。
连接配置从环境变量 / .env 读取（app/config.py）；
目标数据库为 MySQL，本机服务就绪前默认 SQLite 用于本地开发与验证。
"""

from pathlib import Path

from fastapi import Depends, FastAPI
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
from app.database import engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Otaku · ACGN 内容平台与接口自动化测试后台",
    description="Otaku 是一个 ACGN 内容平台（作品 / 资讯 / 档期），同进程内含一套接口回归测试后台"
    "（数据已接入数据库层，本地验证默认 SQLite）。",
    version="0.2.0",
    # 应用级依赖：每个请求解析一次当前登录用户（含内容页面），供模板与守卫复用。
    dependencies=[Depends(load_current_user)],
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
COVER_DIR = Path(settings.cover_storage_dir)
if not COVER_DIR.is_absolute():
    COVER_DIR = BASE_DIR.parent / COVER_DIR
COVER_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/covers", StaticFiles(directory=COVER_DIR), name="covers")

# 用户头像目录：与封面同一套「可指到其它盘符」的配置，挂到 /avatars。
# User.avatar_url 存 /avatars/<文件名>；目录不存在则建，保证首次上传前挂载有效。
AVATAR_DIR = Path(settings.avatar_storage_dir)
if not AVATAR_DIR.is_absolute():
    AVATAR_DIR = BASE_DIR.parent / AVATAR_DIR
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

# 启动即初始化：建表（如无）并播种开发/测试数据；幂等，表已有数据时不覆盖。
# 放在导入期执行，保证 uvicorn 与 TestClient 都拿到就绪的数据库；生产化后换 Alembic。
crud.init_db(engine)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


