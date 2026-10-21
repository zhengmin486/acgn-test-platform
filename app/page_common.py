"""页面路由共享设施：Jinja2 模板实例、数据库会话注解与统一 404 渲染。

业务页面（app/business_pages.py）与测试平台页面（app/testing_pages.py）
共用这里定义的对象，避免重复实例化模板环境或复制渲染逻辑。
"""

import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import media
from app.config import settings
from app.database import get_db

logger = logging.getLogger(__name__)

# 复用的依赖注解：模块级定义一次，各页面签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]


def maybe_enforce_storage_caps() -> None:
    """上传成功后的存储容量巡检（节流触发，见 media.storage_cap_check_due）。

    封面 / 头像目录超限时按「可再生优先」清理（app/media.enforce_storage_caps）；
    巡检要遍历目录，故不能每次上传都跑——到节流间隔才执行一次。任何失败只记
    日志，绝不影响上传请求本身。
    """
    if not media.storage_cap_check_due():
        return
    from app import crud  # 局部导入：数据层只在巡检时才需要
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        stats = media.enforce_storage_caps(
            referenced_covers=crud.list_cover_reference_names(db),
            referenced_avatars=crud.list_avatar_reference_names(db),
        )
        covers = stats["covers"]
        avatars = stats["avatars"]
        if covers["removed_variants"] or covers["removed_unreferenced"] or avatars["removed_unreferenced"]:
            logger.warning(
                "上传目录超限已清理：封面派生图 %d 个、未引用封面 %d 个、未引用头像 %d 个",
                covers["removed_variants"],
                covers["removed_unreferenced"],
                avatars["removed_unreferenced"],
            )
    except Exception:  # 容量巡检失败不影响上传，堆栈只进日志
        logger.exception("上传目录容量巡检失败")
    finally:
        db.close()

BASE_DIR = Path(__file__).resolve().parent


def _inject_current_user(request: Request) -> dict:
    """模板上下文注入当前登录用户，供两套布局渲染登录状态。

    当前用户由应用级依赖（app.auth.load_current_user）解析并写入
    request.state，这里只做读取，不重复查库、也不引入模块间循环依赖。
    """
    return {"current_user": getattr(request.state, "current_user", None)}


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
    context_processors=[_inject_current_user],
)

# 静态资源缓存版本号（模板全局）：取 style.css 的 mtime。
# 模板以 /static/style.css?v={{ asset_v }} 引用，样式一改版本号就变，
# 浏览器自动拉新文件，不再出现「改了 CSS 用户端还是旧样式」的缓存问题；
# 文件缺失（异常环境）时退化为固定值，不影响渲染。
try:
    templates.env.globals["asset_v"] = str(int((BASE_DIR / "static" / "style.css").stat().st_mtime))
except OSError:
    templates.env.globals["asset_v"] = "1"


# 封面图片助手（模板全局）：本地封面走 /media 派生图端点（缩略图 / 压缩全图），
# 远端完整地址原样透传；模板里直接 {{ thumb_src(item.image_url) }}。
templates.env.globals["thumb_src"] = media.thumb_src
templates.env.globals["full_src"] = media.full_src

# 图片对外基址的 origin（base.html head 的 preconnect 用）：配置了
# MEDIA_BASE_URL（CDN / 图床）时提前完成 DNS + TLS 握手；默认同源不输出。
_mb = urlsplit(settings.media_base_url.strip())
templates.env.globals["media_origin"] = (
    f"{_mb.scheme}://{_mb.netloc}" if _mb.scheme and _mb.netloc else ""
)


def news_time(value, with_time: bool = True) -> str:
    """资讯时间的展示格式：源只给到日期时**不显示 00:00**，避免编造时刻。

    ANIPLEX 详情页只有日期（`Oct 20, 2026`），入库后 published_at 是当天 00:00；
    直接 strftime('%H:%M') 会渲染成 `00:00`，看着像「凌晨刚发」，时效观感全错。
    这里把「整点为 0」的时间当作日期粒度，只输出日期。
    """
    if value is None:
        return ""
    date_part = value.strftime("%Y-%m-%d")
    if not with_time:
        return date_part
    if value.hour == 0 and value.minute == 0:
        return date_part
    return value.strftime("%Y-%m-%d %H:%M")


templates.env.globals["news_time"] = news_time

# 作品类型标签（模板全局 + 各路由复用的规范来源）：
# 导航搜索的类型下拉在所有内容页都要渲染，不能依赖单页路由传参，
# 所以提升到这里注册为 Jinja 全局；business_pages 仅 re-export 保持旧导入路径不变。
CONTENT_TYPE_LABELS = {"anime": "动画", "comic": "漫画", "game": "游戏", "novel": "轻小说"}
templates.env.globals["type_labels"] = CONTENT_TYPE_LABELS

# 站点品牌（2026-10-19 改名）：**Otaku** 是站名，「ACGN 内容平台」降级为站点性质。
# 页面标题后缀、页脚副标、404 分区名都从这里取——模板不再各自写死站名，
# 以后改名只改这两个常量（原先 38 处标题后缀各写一遍，改一次要全库搜替换）。
SITE_NAME = "Otaku"
SITE_TAGLINE = "ACGN 内容平台"
templates.env.globals["site_name"] = SITE_NAME
templates.env.globals["site_tagline"] = SITE_TAGLINE

# 书籍细分标注（2026-10-19）：Bangumi 书籍区只有一个「小说」platform，里面既有
# 文库出身的轻小说，也有日本普通小说（纯文学 / 一般文艺）。早先一律入库后，
# 轻小说区混进《罪与罚》《红楼梦》这类普通书籍。
#
# 但「文学」**不是一个新类型**：拆类型的代价是导航、筛选、统计、我的空间各处的
# 类型枚举都要改一遍，而用户视角这两类都在同一个「轻小说区」翻——真正需要的
# 只是「这本书其实是普通文学，不是文库轻小说」的一眼区分。
# 所以：类型仍是 novel，额外用一个轻量标注列 contents.book_kind 打标，
# 卡片与详情页显示「文学」角标，参与不了、也不该参与类型筛选。
BOOK_KIND_LABELS = {"light_novel": "轻小说", "literature": "文学"}
templates.env.globals["book_kind_labels"] = BOOK_KIND_LABELS


# 按 404 主体归属选择所属区域的布局与返回入口：
# 内容类主体（内容/资讯）落在内容平台布局，测试/管理类主体落在测试管理平台布局。
_MISSING_SECTIONS = {
    "内容": {"layout": "base_content.html", "platform": "ACGN 内容平台",
             "back_href": "/", "back_label": "返回列表"},
    "资讯": {"layout": "base_content.html", "platform": "ACGN 内容平台",
             "back_href": "/articles", "back_label": "返回资讯频道"},
    "测试用例": {"layout": "base_admin.html", "platform": "测试管理平台",
               "back_href": "/testcases", "back_label": "返回用例列表"},
    "执行记录": {"layout": "base_admin.html", "platform": "测试管理平台",
               "back_href": "/runs", "back_label": "返回执行历史"},
    "执行批次": {"layout": "base_admin.html", "platform": "测试管理平台",
               "back_href": "/batches", "back_label": "返回批次列表"},
    # 数据管理（内容平台 → 内容管理）下的主体：404 也留在内容平台
    "作品数据": {"layout": "base_content.html", "platform": "ACGN 内容平台",
               "back_href": "/manage/contents", "back_label": "返回作品数据"},
    "资讯数据": {"layout": "base_content.html", "platform": "ACGN 内容平台",
               "back_href": "/manage/articles", "back_label": "返回资讯数据"},
    "档期作品": {"layout": "base_content.html", "platform": "ACGN 内容平台",
               "back_href": "/manage/schedules", "back_label": "返回新番档期"},
    # 作者/公司索引（作品关系）：404 回到索引页
    "作者/公司": {"layout": "base_content.html", "platform": "ACGN 内容平台",
               "back_href": "/creators", "back_label": "返回作者/公司索引"},
}
_MISSING_DEFAULT = {"layout": "base_content.html", "platform": "ACGN 内容平台",
                    "back_href": "/", "back_label": "返回列表"}


def render_missing(request: Request, subject: str, subject_id):
    """统一 404 页面；subject 区分“内容/资讯/测试用例/执行记录”等主体。

    主体决定使用内容平台还是测试管理平台的布局，保证 404 页面也处于正确的区域。
    """
    section = _MISSING_SECTIONS.get(subject, _MISSING_DEFAULT)
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context={"subject": subject, "content_id": subject_id, **section},
        status_code=404,
    )
