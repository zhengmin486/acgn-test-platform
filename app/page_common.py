"""页面路由共享设施：Jinja2 模板实例、数据库会话注解与统一 404 渲染。

业务页面（app/business_pages.py）与测试平台页面（app/testing_pages.py）
共用这里定义的对象，避免重复实例化模板环境或复制渲染逻辑。
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db

# 复用的依赖注解：模块级定义一次，各页面签名直接引用（FastAPI 推荐写法）。
DbSession = Annotated[Session, Depends(get_db)]

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def render_missing(request: Request, subject: str, subject_id):
    """统一 404 页面；subject 区分“内容/资讯/测试用例/执行记录”等主体。"""
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context={"subject": subject, "content_id": subject_id},
        status_code=404,
    )
