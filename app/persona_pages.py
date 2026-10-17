"""阿宅画像页面路由：绑定 Bangumi 用户名 → 同步公开收藏 → 生成画像 → 贴回 LLM 文案。

边界：
- 路由级 require_login_page：画像只属于当前登录账号；
- 同步只写 bangumi_collections，生成只写 persona_reports；只有显式点「同步到本站」才写
  contents 与站内收藏 / 标注（按 bangumi_id 去重，不覆盖已有作品与手动标注）；
- LLM 不写死：配置 PERSONA_LLM_PROVIDER（deepseek/doubao/glm）+ Key 才提供「直接生成」，
  否则用「复制提示词 → 千问/豆包/DeepSeek 网页版 → 贴回」；
- 所有写操作统一 PRG（303 + 查询参数回显）。
"""

import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import crud, persona
from app.auth import LoginRequired, require_login_page
from app.config import settings
from app.page_common import DbSession, templates

PAGE = "/me/persona"


def _guard(request: Request, db: DbSession):
    """登录守卫：POST 的 next 回落到画像主页，避免登录后跳到只支持 POST 的地址。"""
    next_url = PAGE if request.method == "POST" else request.url.path
    try:
        return require_login_page(request, db)
    except LoginRequired:
        raise LoginRequired(next_url=next_url) from None


router = APIRouter(tags=["pages"], dependencies=[Depends(_guard)])

# 免登录试算（引流）：不写库，只在内存里做一次 IP 冷却，防止被当免费代理刷 Bangumi。
public_router = APIRouter(tags=["pages"])
GUEST_PAGE = "/persona"
_guest_hits: dict[str, float] = {}


def _guest_cooldown_left(host: str) -> int:
    cooldown = settings.persona_guest_cooldown_seconds
    if cooldown <= 0:
        return 0
    last = _guest_hits.get(host)
    if last is None:
        return 0
    left = cooldown - (time.monotonic() - last)
    return max(0, int(left) + 1) if left > 0 else 0


def _llm_config() -> dict:
    """当前进程的 LLM 配置（provider 预设 + 显式覆盖）。"""
    return persona.resolve_llm_config(
        settings.persona_llm_provider,
        settings.persona_llm_base_url,
        settings.persona_llm_api_key,
        settings.persona_llm_model,
        settings.persona_llm_max_tokens,
    )


def _uid(request: Request) -> int:
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise LoginRequired(next_url=request.url.path)
    return current_user.id


def _site_works(db: DbSession, user_id: int) -> tuple[list[dict], dict]:
    """把站内收藏 / 标注转成统一作品结构，并返回站内统计。"""
    favorites = crud.list_favorites(db, user_id, page=1, page_size=1000)[1]
    statuses = crud.list_content_status(db, user_id, page=1, page_size=1000)[1]

    merged: dict[int, dict] = {}
    for content in favorites:
        merged[content.id] = {"content": content, "status": "interest"}
    for content, status, _ in statuses:
        entry = merged.setdefault(content.id, {"content": content, "status": status})
        if status in ("seen", "want"):
            entry["status"] = status

    works = []
    for content_id, entry in merged.items():
        tags = crud.list_content_tags(db, content_id)
        works.append(persona.work_from_site(entry["content"], entry["status"], tags))
    return works, {"favorites": len(favorites), "statuses": len(statuses)}


def _sync_limit(request: Request) -> int:
    """同步到本站的单次上限：staff 用更大的额度。"""
    current_user = getattr(request.state, "current_user", None)
    if current_user is not None and current_user.is_staff:
        return settings.persona_site_sync_limit_staff
    return settings.persona_site_sync_limit


def _site_sync_stats(db: DbSession, collections: list, limit: int) -> dict:
    """统计「同步到本站」的预览数量：可识别 / 已有 / 待导入。"""
    works = [
        work
        for work in (persona.work_from_bangumi(row) for row in collections)
        if work
    ]
    ids = [int(work["subject_id"]) for work in works]
    existing = crud.count_contents_by_bangumi(db, ids) if ids else 0
    return {
        "total": len(works),
        "existing": existing,
        "pending": max(0, len(works) - existing),
        "limit": limit,
    }


@router.get("/me/persona", response_class=HTMLResponse)
def persona_page(request: Request, db: DbSession):
    """画像主页：绑定 / 同步 / 生成 / 报告 / 提示词与贴回。"""
    uid = _uid(request)
    account = crud.get_user(db, uid)
    collections = crud.list_bangumi_collections(db, uid)
    report = crud.get_persona_report(db, uid)
    payload = report.payload if report and report.payload else None

    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for item in collections:
        status_name = persona.COLLECTION_STATUS.get(
            int(item.collection_type or 0), "interest"
        )
        status_counts[status_name] = status_counts.get(status_name, 0) + 1
        site_type = persona._site_type(int(item.subject_type), item.subject_tags)
        if site_type:
            type_counts[site_type] = type_counts.get(site_type, 0) + 1

    llm = _llm_config()
    return templates.TemplateResponse(
        request=request,
        name="persona.html",
        context={
            "account": account,
            "guest_mode": False,
            "collections_count": len(collections),
            "status_counts": status_counts,
            "type_counts": type_counts,
            "type_labels": persona.SITE_TYPE_LABELS,
            "status_labels": persona.STATUS_LABELS,
            "report": report,
            "payload": payload,
            "profile": payload.get("profile") if payload else None,
            "template_report": payload.get("report") if payload else None,
            "radar": payload.get("radar") if payload else None,
            "prompt": payload.get("prompt") if payload else "",
            "narrative": report.narrative if report else None,
            "narrative_source": report.narrative_source if report else "template",
            "bound_username": report.bangumi_username if report else "",
            "llm": llm,
            "llm_enabled": llm["ready"],
            "site_sync": _site_sync_stats(db, collections, _sync_limit(request)),
            "site_imports_total": crud.count_user_site_imports(db, uid),
            "site_imports": crud.list_user_site_imports(db, uid, limit=10),
            "tone_labels": persona.TONE_LABELS,
            "tone": report.tone if report else "sharp",
        },
    )


@router.post("/me/persona/sync")
def sync_collections(request: Request, db: DbSession, username: str = Form(default="")):
    """按用户名拉取 Bangumi 公开收藏并覆盖快照。"""
    uid = _uid(request)
    try:
        items, _total = persona.fetch_user_collections(
            username,
            base_url=settings.bangumi_api_base,
            user_agent=settings.bangumi_user_agent,
            proxy=settings.bangumi_proxy,
            timeout=settings.bangumi_timeout,
            max_items=settings.bangumi_max_items,
        )
    except (ValueError, persona.PersonaError) as exc:
        return RedirectResponse(
            f"{PAGE}?error={quote(str(exc)[:200])}", status_code=303
        )
    count = crud.replace_bangumi_collections(db, uid, items)
    crud.set_persona_username(db, uid, username.strip())
    return RedirectResponse(f"{PAGE}?sync={count}", status_code=303)


@router.post("/me/persona/sync-site")
def sync_to_site(request: Request, db: DbSession):
    """把收藏里的作品补进站内作品库，并补齐收藏 / 标注（按 bangumi_id 去重）。"""
    uid = _uid(request)
    collections = crud.list_bangumi_collections(db, uid)
    if not collections:
        return RedirectResponse(
            f"{PAGE}?error={quote('还没有同步 Bangumi 收藏，先在上方绑定用户名并同步')}",
            status_code=303,
        )
    works = [
        work
        for work in (persona.work_from_bangumi(row) for row in collections)
        if work
    ]
    stats = crud.import_works_to_site(
        db, uid, works, limit=_sync_limit(request)
    )
    query = urlencode(
        {
            "site": "ok",
            "site_new": stats["new"],
            "site_linked": stats["linked"],
            "site_fav": stats["favorited"],
            "site_mark": stats["marked"],
            "site_limited": "1" if stats["limited"] else "0",
        }
    )
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


@router.post("/me/persona/site-imports/undo")
def undo_site_imports(request: Request, db: DbSession):
    """撤销本账号导入的作品；被他人使用的保留并回显数量。"""
    uid = _uid(request)
    stats = crud.undo_site_imports(db, uid)
    query = urlencode({"undo": "ok", "undo_removed": stats["removed"], "undo_kept": stats["kept"]})
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


@router.post("/me/persona/generate")
def generate_report(request: Request, db: DbSession, tone: str = Form(default="sharp")):
    """用当前快照 + 站内收藏计算规则画像，并生成模板报告与 LLM 提示词。"""
    uid = _uid(request)
    collections = crud.list_bangumi_collections(db, uid)
    if not collections:
        return RedirectResponse(
            f"{PAGE}?error={quote('还没有同步 Bangumi 收藏，先在上方绑定用户名并同步')}",
            status_code=303,
        )
    tone = tone if tone in persona.TONE_LABELS else "sharp"

    bangumi_works = [persona.work_from_bangumi(item) for item in collections]
    bangumi_works = [work for work in bangumi_works if work]
    site_works, site_counts = _site_works(db, uid)

    profile = persona.build_profile(
        bangumi_works + site_works,
        site_counts=site_counts,
        bangumi_total=len(collections),
    )
    payload = {
        "profile": profile,
        "report": persona.build_template_report(profile),
        "prompt": persona.build_llm_prompt(profile, tone),
        "radar": persona.build_radar(profile["dimensions"]),
    }
    report = crud.get_persona_report(db, uid)
    crud.save_persona_report(
        db,
        uid,
        bangumi_username=report.bangumi_username if report else None,
        tone=tone,
        payload=payload,
        narrative=None,
        source="template",
    )
    return RedirectResponse(f"{PAGE}?generated=1", status_code=303)


@router.post("/me/persona/narrative")
def save_narrative(request: Request, db: DbSession, narrative: str = Form(default="")):
    """保存用户从网页版 / App 贴回的 LLM 文案。"""
    uid = _uid(request)
    if crud.get_persona_report(db, uid) is None:
        return RedirectResponse(
            f"{PAGE}?error={quote('还没有生成画像报告')}", status_code=303
        )
    if not narrative.strip():
        return RedirectResponse(
            f"{PAGE}?error={quote('贴回的文案不能为空')}", status_code=303
        )
    crud.set_persona_narrative(db, uid, narrative, source="manual")
    return RedirectResponse(f"{PAGE}?narrative=manual", status_code=303)


@router.post("/me/persona/llm")
def generate_narrative_with_llm(request: Request, db: DbSession):
    """已配置 LLM 时，直接调用对应服务商的 OpenAI 兼容接口生成文案。"""
    uid = _uid(request)
    report = crud.get_persona_report(db, uid)
    if report is None or not report.payload:
        return RedirectResponse(
            f"{PAGE}?error={quote('还没有生成画像报告')}", status_code=303
        )
    llm = _llm_config()
    if not llm["ready"]:
        return RedirectResponse(
            f"{PAGE}?error={quote(llm['hint'] or '未配置 LLM 接口')}", status_code=303
        )
    prompt = report.payload.get("prompt") or ""
    try:
        text = persona.call_llm(
            prompt,
            base_url=llm["base_url"],
            api_key=llm["api_key"],
            model=llm["model"],
            proxy=settings.bangumi_proxy,
            max_tokens=llm["max_tokens"],
        )
    except persona.PersonaError as exc:
        return RedirectResponse(
            f"{PAGE}?error={quote(str(exc)[:200])}", status_code=303
        )
    crud.set_persona_narrative(db, uid, text, source="llm")
    return RedirectResponse(f"{PAGE}?narrative=llm", status_code=303)


@router.post("/me/persona/llm/test")
def test_llm_connection():
    """用一句话探测接口是否可用（Key 是否正确、模型是否存在）。"""
    llm = _llm_config()
    if not llm["ready"]:
        return RedirectResponse(
            f"{PAGE}?error={quote(llm['hint'] or '未配置 LLM 接口')}", status_code=303
        )
    try:
        text = persona.call_llm(
            "只回复两个字：可用",
            base_url=llm["base_url"],
            api_key=llm["api_key"],
            model=llm["model"],
            proxy=settings.bangumi_proxy,
            timeout=30.0,
            max_tokens=64,
        )
    except persona.PersonaError as exc:
        return RedirectResponse(
            f"{PAGE}?error={quote(str(exc)[:200])}", status_code=303
        )
    return RedirectResponse(
        f"{PAGE}?llmtest={quote(llm['label'])}&llmreply={quote(text[:60])}",
        status_code=303,
    )


@router.post("/me/persona/clear")
def clear_persona(request: Request, db: DbSession):
    """清除本账号的收藏快照与画像（隐私：一键删除）。"""
    uid = _uid(request)
    crud.clear_persona(db, uid)
    return RedirectResponse(f"{PAGE}?cleared=1", status_code=303)


def _guest_counts(items: list[dict]) -> tuple[dict, dict]:
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for item in items:
        status_name = persona.COLLECTION_STATUS.get(
            int(item.get("collection_type") or 0), "interest"
        )
        status_counts[status_name] = status_counts.get(status_name, 0) + 1
        site_type = persona._site_type(
            int(item.get("subject_type") or 0), item.get("subject_tags")
        )
        if site_type:
            type_counts[site_type] = type_counts.get(site_type, 0) + 1
    return status_counts, type_counts


def _render_guest_error(request: Request, message: str):
    return templates.TemplateResponse(
        request=request,
        name="persona_landing.html",
        context={
            "error": message,
            "max_items": settings.persona_guest_max_items,
            "username": "",
        },
        status_code=200,
    )


@public_router.get(GUEST_PAGE, response_class=HTMLResponse)
def persona_landing(request: Request):
    """免登录试算入口：填个 Bangumi 用户名就能先看一版画像（不保存）。"""
    return templates.TemplateResponse(
        request=request,
        name="persona_landing.html",
        context={
            "error": request.query_params.get("error", ""),
            "max_items": settings.persona_guest_max_items,
            "username": "",
        },
    )


@public_router.post("/persona/preview", response_class=HTMLResponse)
def persona_preview(request: Request, username: str = Form(default="")):
    """免登录试算：拉公开收藏 → 算规则画像 → 直接渲染（不写库、不建账号）。"""
    host = request.client.host if request.client else "unknown"
    wait = _guest_cooldown_left(host)
    if wait:
        return _render_guest_error(request, f"操作有点频繁，请 {wait} 秒后再试。")

    name = username.strip()
    if not name:
        return _render_guest_error(request, "请先填写 Bangumi 用户名。")

    try:
        items, _total = persona.fetch_user_collections(
            name,
            base_url=settings.bangumi_api_base,
            user_agent=settings.bangumi_user_agent,
            proxy=settings.bangumi_proxy,
            timeout=settings.bangumi_timeout,
            max_items=settings.persona_guest_max_items,
        )
    except (ValueError, persona.PersonaError) as exc:
        return _render_guest_error(request, str(exc)[:200])

    if not items:
        return _render_guest_error(
            request, f"没读到「{name}」的公开收藏。检查用户名，或确认收藏是否公开。"
        )
    _guest_hits[host] = time.monotonic()

    rows = [SimpleNamespace(**item) for item in items]
    works = [work for work in (persona.work_from_bangumi(row) for row in rows) if work]
    profile = persona.build_profile(
        works, site_counts={"favorites": 0, "statuses": 0}, bangumi_total=len(items)
    )
    payload = {
        "profile": profile,
        "report": persona.build_template_report(profile),
        "prompt": persona.build_llm_prompt(profile, "sharp"),
        "radar": persona.build_radar(profile["dimensions"]),
    }
    status_counts, type_counts = _guest_counts(items)
    stub_report = SimpleNamespace(
        generated_at=datetime.now(),
        narrative=None,
        narrative_source="template",
        bangumi_username=name,
        tone="sharp",
    )

    return templates.TemplateResponse(
        request=request,
        name="persona.html",
        context={
            "guest_mode": True,
            "account": None,
            "collections_count": len(items),
            "status_counts": status_counts,
            "type_counts": type_counts,
            "type_labels": persona.SITE_TYPE_LABELS,
            "status_labels": persona.STATUS_LABELS,
            "report": stub_report,
            "payload": payload,
            "profile": profile,
            "template_report": payload["report"],
            "radar": payload["radar"],
            "prompt": payload["prompt"],
            "narrative": None,
            "narrative_source": "template",
            "bound_username": name,
            "llm": _llm_config(),
            "llm_enabled": False,
            "site_sync": {
                "total": len(works),
                "existing": 0,
                "pending": 0,
                "limit": settings.persona_site_sync_limit,
            },
            "site_imports_total": 0,
            "site_imports": [],
            "tone_labels": persona.TONE_LABELS,
            "tone": "sharp",
            "guest_max_items": settings.persona_guest_max_items,
        },
    )
