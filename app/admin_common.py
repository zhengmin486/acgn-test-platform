"""管理页面共享小工具：内容管理（/manage/*）与站点设置（/admin/*）两组路由共用。

只放与业务无关的纯函数：不定义路由、不碰数据库、不 import 任何页面模块，
这样两组路由共用同一套拼参 / 分页 / 截断规则，又不会互相依赖。
"""

import math
from urllib.parse import urlencode


def page_count(total: int, page_size: int = 20) -> int:
    """总页数（至少 1，便于模板统一渲染分页）。"""
    return max(1, math.ceil(total / page_size)) if total else 1


def url_with(path: str, **params) -> str:
    """拼装带查询参数的站内地址；空字符串 / None 参数自动忽略。"""
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return f"{path}?{urlencode(clean)}" if clean else path


def page_param(value) -> str | None:
    """页码回传：第 1 页不写入地址，保持 URL 简洁。"""
    text = str(value).strip()
    return None if text in ("", "0", "1") else text


def form_text(form, key: str) -> str:
    """取表单里的单个文本字段（不存在时返回空串）。"""
    return str(form.get(key, "")).strip()


def collect_ids(values) -> list[int]:
    """把表单里的 ids 原始值转成合法主键列表；非数字项直接忽略。"""
    ids: list[int] = []
    for value in values:
        text = str(value).strip()
        if text.isdigit():
            ids.append(int(text))
    return ids


def short_text(text: str, limit: int = 300) -> str:
    """折叠空白并截断过长提示（默认 300 字，避免撑爆地址栏与页面）。"""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def actor_id(request) -> int | None:
    """当前登录管理员的 id（用于运行记录审计）；未知时返回 None。"""
    return getattr(getattr(request.state, "current_user", None), "id", None)
