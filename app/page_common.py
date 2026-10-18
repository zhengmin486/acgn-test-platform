"""页面路由共享设施：Jinja2 模板实例、数据库会话注解与统一 404 渲染。

业务页面（app/business_pages.py）与测试平台页面（app/testing_pages.py）
共用这里定义的对象，避免重复实例化模板环境或复制渲染逻辑。
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import media
from app.database import get_db

# 复用的依赖注解：模块级定义一次，各页面签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]

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

# 封面图片助手（模板全局）：本地封面走 /media 派生图端点（缩略图 / 压缩全图），
# 远端完整地址原样透传；模板里直接 {{ thumb_src(item.image_url) }}。
templates.env.globals["thumb_src"] = media.thumb_src
templates.env.globals["full_src"] = media.full_src

# 作品类型标签（模板全局 + 各路由复用的规范来源）：
# 导航搜索的类型下拉在所有内容页都要渲染，不能依赖单页路由传参，
# 所以提升到这里注册为 Jinja 全局；business_pages 仅 re-export 保持旧导入路径不变。
CONTENT_TYPE_LABELS = {"anime": "动画", "comic": "漫画", "game": "游戏", "novel": "轻小说"}
templates.env.globals["type_labels"] = CONTENT_TYPE_LABELS


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
