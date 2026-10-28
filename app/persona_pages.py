"""御宅成分检测页面路由：绑定 Bangumi 用户名 / B站 UID → 同步公开收藏与追番表
→ 生成画像 → 生成 / 贴回 LLM 文案。

边界：
- 路由级 require_login_page：画像只属于当前登录账号；
- 同步只写 bangumi_collections / bilibili_follows，生成只写 persona_reports；
  只有显式点「同步到本站」才写 contents 与站内收藏 / 标注（按 bangumi_id 去重，
  不覆盖已有作品与手动标注）；
- 三个数据源（Bangumi 收藏 + B站追番表 + 站内收藏标注）合并进同一 build_profile，
  跨源同作品保留 Bangumi 版（标签更全）；
- 同步成功即自动重算画像（保留已贴回的文案与语气设置），不必再手点「生成」；
- LLM 双轨：用户级 Key（自己填，走自己额度）优先于站点级 PERSONA_LLM_* 环境变量；
  都没配时用「复制提示词 → 网页版 → 贴回」；
- 所有写操作统一 PRG（303 + 查询参数回显）。
"""

import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import bilibili, crud, persona, security
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

# 免登录试算（引流）：不写库，内存里做三层软门禁，防止被当免费代理刷：
#   1) 20 秒防抖（_guest_hits）：防双击 / 连点，不占额度；
#   2) 设备闸（签名 Cookie，_guest_device_hits）：同一设备 24 小时最多 N 次；
#      清 Cookie / 无痕可绕过，所以叠一层 IP 闸；
#   3) IP 闸（_guest_ip_hits）：同一 IP 24 小时最多 M 次（额度放宽，兜住脚本）。
# 登录用户三层全部跳过（游走路：登录后绑定数据源随便同步）。
# 计数只在内存，重启即清——软门禁，不做账。
public_router = APIRouter(tags=["pages"])
GUEST_PAGE = "/persona"
GUEST_WINDOW_SECONDS = 24 * 3600
GUEST_DEVICE_COOKIE = "guest_trial"
# 设备 Cookie 本身留一年：额度按 24 小时窗口滚动，Cookie 没必要跟着过期。
GUEST_DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600
# 游客结果的认领凭据：签名里带结果行 id，注册后凭它把结果搬进账号。
RESULT_COOKIE = "persona_result"
RESULT_COOKIE_MAX_AGE = crud.ANONYMOUS_RESULT_TTL_DAYS * 24 * 3600


def _attach_result_cookie(response, result_id: int | None) -> None:
    """把结果 id 签进 Cookie：注册后凭它把游客结果领回来（签名防伪造）。"""
    if not result_id:
        return
    response.set_cookie(
        RESULT_COOKIE,
        security.create_result_token(int(result_id), settings.auth_secret_key),
        max_age=RESULT_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def _pending_result_id(request: Request) -> int | None:
    """Cookie 里未认领的结果 id；没有 / 签名不符返回 None。"""
    token = request.cookies.get(RESULT_COOKIE) or ""
    if not token:
        return None
    return security.read_result_token(token, settings.auth_secret_key)


def _try_claim_pending_result(db: DbSession, user_id: int, result_id: int) -> bool:
    """把游客快照搬成正式报告，成功返回 True。

    已经有报告就不覆盖——用户登录后自己攒的数据比一份游客试算更值钱。
    """
    if crud.get_persona_report(db, user_id) is not None:
        return False
    payload = crud.claim_anonymous_persona_result(db, result_id, user_id)
    if not payload or not payload.get("profile"):
        return False
    crud.save_persona_report(
        db,
        user_id,
        bangumi_username=None,
        tone="sharp",
        payload=payload,
        narrative=None,
        source="template",
    )
    return True


_guest_hits: dict[str, float] = {}
_guest_device_hits: dict[str, list[float]] = {}
_guest_ip_hits: dict[str, list[float]] = {}


def _guest_cooldown_left(host: str) -> int:
    cooldown = settings.persona_guest_cooldown_seconds
    if cooldown <= 0:
        return 0
    last = _guest_hits.get(host)
    if last is None:
        return 0
    left = cooldown - (time.monotonic() - last)
    return max(0, int(left) + 1) if left > 0 else 0


def _prune_hits(bucket: list[float], now: float) -> list[float]:
    """丢弃滑出 24 小时窗口的计数时间戳。"""
    return [stamp for stamp in bucket if now - stamp < GUEST_WINDOW_SECONDS]


def _guest_device_of(request: Request) -> str:
    """从签名 Cookie 里取出设备 id；无 Cookie / 伪造 / 格式错一律空串。"""
    token = request.cookies.get(GUEST_DEVICE_COOKIE, "")
    if not token:
        return ""
    return security.read_device_token(token, settings.auth_secret_key) or ""


def _guest_limit_left(dev: str, host: str) -> tuple[int, int]:
    """返回 (设备剩余次数, IP 剩余次数)；设备 id 为空时设备闸不参与判断。"""
    now = time.monotonic()
    left_device = settings.persona_guest_device_daily_limit
    if dev:
        bucket = _guest_device_hits.get(dev, [])
        bucket = _prune_hits(bucket, now)
        _guest_device_hits[dev] = bucket
        left_device -= len(bucket)
    left_ip = settings.persona_guest_ip_daily_limit - len(
        _prune_hits(_guest_ip_hits.get(host, []), now)
    )
    return left_device, left_ip


def _record_guest_use(dev: str, host: str) -> None:
    """成功试算后记账：设备 + IP 各记一次。"""
    now = time.monotonic()
    if dev:
        _guest_device_hits.setdefault(dev, []).append(now)
    _guest_ip_hits.setdefault(host, []).append(now)


def _mask_key(key: str) -> str:
    """Key 脱敏：只露首 4 尾 4，页面回显用（绝不回显全文）。"""
    key = key or ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"


def _llm_config(request: Request, db: DbSession) -> dict:
    """LLM 配置：用户级 Key 优先，未配回落到站点级环境变量。

    返回字典额外带 source（user / site / none）与 masked_key（用户级时）。
    用户填了 Key 就以用户配置为准（哪怕还缺 model，提示也对着用户的配置说，
    不静默切回站点配置——避免「我明明填了 Key 怎么用别人的」）。
    """
    current_user = getattr(request.state, "current_user", None)
    if current_user is not None:
        user_cfg = crud.get_user_llm_config(db, current_user.id)
        if user_cfg["api_key"]:
            resolved = persona.resolve_llm_config(
                user_cfg["provider"],
                user_cfg["base_url"],
                user_cfg["api_key"],
                user_cfg["model"],
                settings.persona_llm_max_tokens,
            )
            resolved["source"] = "user"
            resolved["masked_key"] = _mask_key(user_cfg["api_key"])
            return resolved
    resolved = persona.resolve_llm_config(
        settings.persona_llm_provider,
        settings.persona_llm_base_url,
        settings.persona_llm_api_key,
        settings.persona_llm_model,
        settings.persona_llm_max_tokens,
    )
    resolved["source"] = "site" if resolved["ready"] else "none"
    resolved["masked_key"] = ""
    return resolved


def _regenerate_report(
    db: DbSession,
    uid: int,
    *,
    tone: str | None = None,
    tone_prompt: str | None = None,
    extra_prompt: str | None = None,
) -> str:
    """用当前三源快照重算画像报告（手动生成与同步后自动重算共用）。

    返回空串表示成功；非空为可读的错误消息（无数据可分析）。
    语气 / 提示词设置：显式传入的优先，否则沿用报告里的旧值；
    已贴回的 LLM 文案原样保留（重算只换事实与骨架，不丢用户的文案）。
    """
    bangumi_rows = crud.list_bangumi_collections(db, uid)
    bilibili_rows = crud.list_bilibili_follows(db, uid)
    site_works, site_counts = _site_works(db, uid)
    bangumi_works = [
        work
        for work in (persona.work_from_bangumi(row) for row in bangumi_rows)
        if work
    ]
    bilibili_works = [persona.work_from_bilibili(row) for row in bilibili_rows]
    works = persona.dedupe_works_across_sources(bangumi_works, bilibili_works)
    works += site_works
    if not works:
        return "还没有可分析的数据：先在作品库收藏或标注几部作品，也可以同步 Bangumi 收藏 / B站追番表"

    report = crud.get_persona_report(db, uid)
    use_tone = tone or (report.tone if report else "sharp")
    use_tone_prompt = (
        tone_prompt if tone_prompt is not None else (report.tone_prompt if report else "")
    )
    use_extra = (
        extra_prompt if extra_prompt is not None else (report.extra_prompt if report else "")
    )
    profile = persona.build_profile(
        works,
        site_counts=site_counts,
        bangumi_total=len(bangumi_rows),
        bilibili_total=len(bilibili_rows),
    )
    exclude_ids = {int(row.subject_id) for row in bangumi_rows}
    recommendations = persona.recommend_from_site(db, profile, exclude_ids)
    _rarity = persona.attach_rarity(db, profile)
    payload = {
        "profile": profile,
        "report": persona.build_template_report(profile, _rarity),
        "prompt": persona.build_llm_prompt(
            profile,
            use_tone,
            tone_prompt=use_tone_prompt or "",
            extra_prompt=use_extra or "",
            recommendations=recommendations,
        ),
        "radar": persona.build_radar(profile["dimensions"]),
        "recommendations": recommendations,
        "rarity": _rarity,
    }
    crud.save_persona_report(
        db,
        uid,
        bangumi_username=report.bangumi_username if report else None,
        tone=use_tone,
        payload=payload,
        narrative=report.narrative if report else None,
        source=report.narrative_source if report else "template",
        tone_prompt=use_tone_prompt or None,
        extra_prompt=use_extra or None,
        bilibili_mid=report.bilibili_mid if report else None,
    )
    return ""


def _freshness_hint(
    report, site_total: int, bangumi_count: int, bilibili_count: int
) -> str:
    """画像新鲜度提示：与首页推荐同一套双闸口径。

    - 站内收藏 / 标注数同时超过「画像总数 × 比例」和「画像总数 + 基数」→
      提醒重算（首页推荐已切实时口径，报告还挂着旧画像）；
    - 外部快照条数与画像记录不一致（重新同步过）→ 提醒重算。
    """
    if report is None or not report.payload:
        return ""
    profile = (report.payload or {}).get("profile") or {}
    profile_total = int(profile.get("total") or 0)
    if (
        profile_total > 0
        and site_total >= profile_total * persona.HOME_TASTE_PROFILE_RATIO
        and site_total >= profile_total + persona.HOME_TASTE_PROFILE_BASE
    ):
        return (
            f"站内收藏 / 标注（{site_total} 部）已远超画像规模（{profile_total} 部），"
            "首页推荐已按最新口味生效；点下方「重新生成」可同步刷新报告"
        )
    snapshot_total = bangumi_count + bilibili_count
    profile_snapshot = int(profile.get("bangumi_total") or 0) + int(
        profile.get("bilibili_total") or 0
    )
    if snapshot_total != profile_snapshot:
        return (
            f"同步的收藏数据有变化（快照 {snapshot_total} 条 / 画像生成时 {profile_snapshot} 条），"
            "点下方「重新生成」刷新报告"
        )
    return ""


def _uid(request: Request) -> int:
    current_user = getattr(request.state, "current_user", None)
    if current_user is None:
        raise LoginRequired(next_url=request.url.path)
    return current_user.id


def _site_works(db: DbSession, user_id: int) -> tuple[list[dict], dict]:
    """把站内收藏 / 标注转成统一作品结构，并返回站内统计（批量带标签，无 N+1）。"""
    rows = crud.list_user_taste_rows(db, user_id)
    works = []
    favorites = statuses = 0
    for content, tags, status, favorited, _activity in rows:
        favorites += 1 if favorited else 0
        statuses += 1 if status else 0
        works.append(persona.work_from_site(content, status, tags))
    return works, {"favorites": favorites, "statuses": statuses}


def _site_taste_summary(db: DbSession, user_id: int) -> tuple[dict, int]:
    """站内口味的「只要数字」版：(统计字典, 总条数)。

    主页只展示计数，不需要作品实体——用聚合查询替代 _site_works，省掉 Content
    实例化与标签批量查询两份开销；真正要把作品喂给 build_profile 的场景才用前者
    （2026-10-28 改）。
    """
    total, favorites, statuses = crud.count_user_taste_rows(db, user_id)
    return {"favorites": favorites, "statuses": statuses}, total


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


def _backfill_template_report(payload: dict | None) -> dict | None:
    """历史报告可能缺 trait_blurbs（旧代码未生成该键），在此回填，避免模板 500
    并让老用户也能看到人格标签的毒舌短评。"""
    if not payload:
        return None
    report = payload.get("report")
    if not isinstance(report, dict):
        return report
    if "trait_blurbs" in report:
        return report
    tags = (
        report.get("persona_tags")
        or (payload.get("profile") or {}).get("persona_tags")
        or []
    )
    report = dict(report)
    report["trait_blurbs"] = {t: persona.TRAIT_BLURBS.get(t, "") for t in tags}
    return report


@router.get("/me/persona", response_class=HTMLResponse)
def persona_page(request: Request, db: DbSession):
    """画像主页：绑定 / 同步 / 生成 / 报告 / 提示词与贴回。"""
    uid = _uid(request)
    # 游客结果领回：注册后第一次进画像页，把未认领的快照搬成正式报告，再重定向刷新
    pending_id = _pending_result_id(request)
    if pending_id is not None and _try_claim_pending_result(db, uid, pending_id):
        response = RedirectResponse(PAGE, status_code=303)
        response.delete_cookie(RESULT_COOKIE)
        return response
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

    bilibili_rows = crud.list_bilibili_follows(db, uid)
    bilibili_status_counts: dict[str, int] = {}
    for row in bilibili_rows:
        status_name = persona.BILIBILI_FOLLOW_STATUS.get(
            int(row.follow_status or 0), "interest"
        )
        bilibili_status_counts[status_name] = (
            bilibili_status_counts.get(status_name, 0) + 1
        )

    llm = _llm_config(request, db)
    # 主页只用计数，不拉作品实体（真正要用作品的是 _regenerate_report）
    site_counts, site_work_count = _site_taste_summary(db, uid)
    return templates.TemplateResponse(
        request=request,
        name="persona.html",
        context={
            "account": account,
            "guest_mode": False,
            "collections_count": len(collections),
            "site_counts": site_counts,
            "site_work_count": site_work_count,
            "status_counts": status_counts,
            "type_counts": type_counts,
            "type_labels": persona.SITE_TYPE_LABELS,
            "status_labels": persona.STATUS_LABELS,
            "report": report,
            "payload": payload,
            "profile": payload.get("profile") if payload else None,
            # 分享卡参数：由画像现算，不落库（卡片内容只依赖人格标签与维度）
            "share_card_token": (
                persona.encode_card(payload.get("profile") or {}, payload.get("rarity"))
                if payload
                else ""
            ),
            "template_report": _backfill_template_report(payload),
            "radar": payload.get("radar") if payload else None,
            "prompt": payload.get("prompt") if payload else "",
            "narrative": report.narrative if report else None,
            "narrative_source": report.narrative_source if report else "template",
            "bound_username": report.bangumi_username if report else "",
            "llm": llm,
            "llm_enabled": llm["ready"],
            "llm_source": llm.get("source", "none"),
            "llm_key_masked": llm.get("masked_key", ""),
            "llm_providers": persona.LLM_PROVIDERS,
            "bilibili_count": len(bilibili_rows),
            "bilibili_status_counts": bilibili_status_counts,
            "bound_bilibili_mid": report.bilibili_mid if report else "",
            "freshness_hint": _freshness_hint(
                report,
                site_work_count,
                len(collections),
                len(bilibili_rows),
            ),
            "site_sync": _site_sync_stats(db, collections, _sync_limit(request)),
            "site_imports_total": crud.count_user_site_imports(db, uid),
            "site_imports": crud.list_user_site_imports(db, uid, limit=10),
            "tone_labels": persona.TONE_LABELS,
            "tone": report.tone if report else "sharp",
            "tone_prompt": report.tone_prompt if report else "",
            "extra_prompt": report.extra_prompt if report else "",
            "tone_prompt_max": persona.MAX_TONE_PROMPT,
            "extra_prompt_max": persona.MAX_EXTRA_PROMPT,
        },
    )


@router.post("/me/persona/sync")
def sync_collections(request: Request, db: DbSession, username: str = Form(default="")):
    """按用户名拉取 Bangumi 公开收藏并覆盖快照，随后自动重算画像。"""
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
    error = _regenerate_report(db, uid)
    query = {"sync": count}
    if error:
        query["error"] = error[:200]
    else:
        query["generated"] = "1"
    return RedirectResponse(f"{PAGE}?{urlencode(query)}", status_code=303)


@router.post("/me/persona/sync-bilibili")
def sync_bilibili(request: Request, db: DbSession, mid: str = Form(default="")):
    """按 UID 拉取 B站公开追番列表并覆盖快照，随后自动重算画像。

    接口匿名可读（无需 cookie）；用户把追番列表设成隐私时返回 53013，
    错误消息带公开引导（见 bilibili.error_message）。
    """
    uid = _uid(request)
    try:
        items, _total = bilibili.fetch_user_follows(
            mid,
            user_agent=settings.bilibili_user_agent,
            timeout=settings.bilibili_timeout,
            max_items=settings.bilibili_max_items,
        )
    except (ValueError, persona.PersonaError) as exc:
        return RedirectResponse(
            f"{PAGE}?error={quote(str(exc)[:200])}", status_code=303
        )
    count = crud.replace_bilibili_follows(db, uid, items)
    crud.set_persona_bilibili_mid(db, uid, mid.strip())
    error = _regenerate_report(db, uid)
    query = {"bili": count}
    if error:
        query["error"] = error[:200]
    else:
        query["generated"] = "1"
    return RedirectResponse(f"{PAGE}?{urlencode(query)}", status_code=303)


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
    # 站内收藏 / 标注变了：自动重算画像（首页推荐与报告同步生效）。
    error = _regenerate_report(db, uid)
    query = {
        "site": "ok",
        "site_new": stats["new"],
        "site_linked": stats["linked"],
        "site_fav": stats["favorited"],
        "site_mark": stats["marked"],
        "site_limited": "1" if stats["limited"] else "0",
    }
    if error:
        query["error"] = error[:200]
    else:
        query["generated"] = "1"
    return RedirectResponse(f"{PAGE}?{urlencode(query)}", status_code=303)


@router.post("/me/persona/site-imports/undo")
def undo_site_imports(request: Request, db: DbSession):
    """撤销本账号导入的作品；被他人使用的保留并回显数量。"""
    uid = _uid(request)
    stats = crud.undo_site_imports(db, uid)
    query = urlencode({"undo": "ok", "undo_removed": stats["removed"], "undo_kept": stats["kept"]})
    return RedirectResponse(f"{PAGE}?{query}", status_code=303)


@router.post("/me/persona/generate")
def generate_report(
    request: Request,
    db: DbSession,
    tone: str = Form(default="sharp"),
    tone_prompt: str = Form(default=""),
    extra_prompt: str = Form(default=""),
):
    """手动生成：更新语气 / 提示词设置后，按三源快照重算画像。"""
    uid = _uid(request)
    tone = tone if tone in persona.TONE_LABELS else "sharp"
    tone_prompt = (tone_prompt or "").strip()[: persona.MAX_TONE_PROMPT]
    extra_prompt = (extra_prompt or "").strip()[: persona.MAX_EXTRA_PROMPT]
    if tone == "custom" and not tone_prompt:
        return RedirectResponse(
            f"{PAGE}?error={quote('选择了「自定义语气」，请先填写语气说明')}",
            status_code=303,
        )
    error = _regenerate_report(
        db,
        uid,
        tone=tone,
        tone_prompt=tone_prompt,
        extra_prompt=extra_prompt,
    )
    if error:
        return RedirectResponse(f"{PAGE}?error={quote(error[:200])}", status_code=303)
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
    """直接调用 LLM（用户级 Key 优先，站点级兜底）生成文案。"""
    uid = _uid(request)
    report = crud.get_persona_report(db, uid)
    if report is None or not report.payload:
        return RedirectResponse(
            f"{PAGE}?error={quote('还没有生成画像报告')}", status_code=303
        )
    llm = _llm_config(request, db)
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
def test_llm_connection(request: Request, db: DbSession):
    """用一句话探测接口是否可用（Key 是否正确、模型是否存在）。"""
    llm = _llm_config(request, db)
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


@router.post("/me/persona/llm-key")
def save_llm_key(
    request: Request,
    db: DbSession,
    provider: str = Form(default=""),
    api_key: str = Form(default=""),
    base_url: str = Form(default=""),
    model: str = Form(default=""),
):
    """保存用户自己的 LLM 接口 Key（OpenAI 兼容协议）。

    Key 只存服务端数据库、仅用于调用接口，页面脱敏回显；建议用子账号 /
    小额 Key。provider 预设（deepseek/doubao/glm）自动补地址与模型，
    「自定义」需自己填 base_url 与 model。
    """
    uid = _uid(request)
    api_key = (api_key or "").strip()
    if not api_key:
        return RedirectResponse(f"{PAGE}?error={quote('请填写 API Key')}", status_code=303)
    if len(api_key) > 200:
        return RedirectResponse(f"{PAGE}?error={quote('API Key 过长')}", status_code=303)
    provider = (provider or "").strip().lower()
    if provider not in persona.LLM_PROVIDERS and provider != "custom":
        provider = "custom"
    if provider == "custom" and not (base_url or "").strip():
        return RedirectResponse(
            f"{PAGE}?error={quote('自定义接口需要填写 base_url')}", status_code=303
        )
    crud.set_user_llm_config(
        db, uid, provider=provider, api_key=api_key, base_url=base_url, model=model
    )
    return RedirectResponse(f"{PAGE}?llmkey=saved", status_code=303)


@router.post("/me/persona/llm-key/clear")
def clear_llm_key(request: Request, db: DbSession):
    """清除用户级 LLM 配置，回落到站点级 / 手动贴回模式。"""
    uid = _uid(request)
    crud.clear_user_llm_config(db, uid)
    return RedirectResponse(f"{PAGE}?llmkey=cleared", status_code=303)


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


def _render_guest_error(
    request: Request, message: str, username: str = "", mid: str = ""
):
    return templates.TemplateResponse(
        request=request,
        name="persona_landing.html",
        context={
            "error": message,
            "max_items": settings.persona_guest_max_items,
            "bili_max_items": settings.persona_guest_bilibili_max_items,
            "username": username,
            "mid": mid,
        },
        status_code=200,
    )


def _guest_bilibili_counts(rows) -> dict[str, int]:
    """游客态 B站追番的状态分布（与登录态同口径）。"""
    counts: dict[str, int] = {}
    for row in rows:
        status_name = persona.BILIBILI_FOLLOW_STATUS.get(
            int(row.follow_status or 0), "interest"
        )
        counts[status_name] = counts.get(status_name, 0) + 1
    return counts


@public_router.get(GUEST_PAGE, response_class=HTMLResponse)
def persona_landing(request: Request):
    """免登录试算入口：填 Bangumi 用户名 / B站 UID 就能先看一版画像（不保存）。"""
    return templates.TemplateResponse(
        request=request,
        name="persona_landing.html",
        context={
            "error": request.query_params.get("error", ""),
            "max_items": settings.persona_guest_max_items,
            "bili_max_items": settings.persona_guest_bilibili_max_items,
            "username": "",
            "mid": "",
        },
    )


@public_router.post("/persona/preview", response_class=HTMLResponse)
def persona_preview(
    request: Request,
    db: DbSession,
    username: str = Form(default=""),
    mid: str = Form(default=""),
):
    """免登录试算：拉公开收藏 / 追番表 → 算规则画像 → 直接渲染。

    两个数据源填一个或都填，都填时跨源合并（同作品保留 Bangumi 版）：
    - Bangumi 用户名 → 公开收藏（上限 persona_guest_max_items）；
    - B站 UID → 公开追番表（匿名可读，上限 persona_guest_bilibili_max_items）。
    单源失败不致命：成功的源照算，失败原因进 profile.notes 在结果页提示；
    两个源都失败 / 都没数据才报错。全程不写库、不建账号、不绑定。
    """
    host = request.client.host if request.client else "unknown"
    name = (username or "").strip()
    bili_mid = (mid or "").strip()
    current_user = getattr(request.state, "current_user", None)
    dev = _guest_device_of(request)
    if current_user is None:  # 登录用户不受限
        wait = _guest_cooldown_left(host)
        if wait:
            return _render_guest_error(
                request, f"操作有点频繁，请 {wait} 秒后再试。", name, bili_mid
            )
        left_device, left_ip = _guest_limit_left(dev, host)
        if dev and left_device <= 0:
            return _render_guest_error(
                request,
                f"这个设备今天已经试算 {settings.persona_guest_device_daily_limit} 次了，"
                "登录后不受限制。",
                name,
                bili_mid,
            )
        if left_ip <= 0:
            return _render_guest_error(
                request,
                f"当前网络今天已试算 {settings.persona_guest_ip_daily_limit} 次，"
                "登录后不受限制。",
                name,
                bili_mid,
            )

    if not name and not bili_mid:
        return _render_guest_error(
            request, "请填写 Bangumi 用户名或 B站 UID（至少一个）。", name, bili_mid
        )

    notes: list[str] = []
    bangumi_items: list[dict] = []
    bilibili_items: list[dict] = []
    if name:
        try:
            bangumi_items, _total = persona.fetch_user_collections(
                name,
                base_url=settings.bangumi_api_base,
                user_agent=settings.bangumi_user_agent,
                proxy=settings.bangumi_proxy,
                timeout=settings.bangumi_timeout,
                max_items=settings.persona_guest_max_items,
            )
            if not bangumi_items:
                notes.append(f"Bangumi 没读到「{name}」的公开收藏（已跳过）。")
        except (ValueError, persona.PersonaError) as exc:
            notes.append(f"Bangumi 收藏读取失败：{str(exc)[:120]}（已跳过）")
    if bili_mid:
        try:
            bilibili_items, _total = bilibili.fetch_user_follows(
                bili_mid,
                user_agent=settings.bilibili_user_agent,
                timeout=settings.bilibili_timeout,
                max_items=settings.persona_guest_bilibili_max_items,
            )
            if not bilibili_items:
                notes.append(f"B站 UID {bili_mid} 的追番表是空的（已跳过）。")
        except (ValueError, persona.PersonaError) as exc:
            notes.append(f"B站追番表读取失败：{str(exc)[:120]}（已跳过）")

    bangumi_rows = [SimpleNamespace(**item) for item in bangumi_items]
    bilibili_rows = [SimpleNamespace(**item) for item in bilibili_items]
    bangumi_works = [
        work for work in (persona.work_from_bangumi(row) for row in bangumi_rows) if work
    ]
    bilibili_works = [persona.work_from_bilibili(row) for row in bilibili_rows]
    works = persona.dedupe_works_across_sources(bangumi_works, bilibili_works)
    if not works:
        detail = "；".join(notes) if notes else "接口没有返回可识别的作品"
        return _render_guest_error(
            request, f"没读到可分析的公开数据：{detail}", name, bili_mid
        )

    _guest_hits[host] = time.monotonic()
    # 记账：首访没有设备 Cookie 时新签一个，本次也算进该设备额度
    new_device_token = ""
    if current_user is None:
        if not dev:
            new_device_token = security.create_device_token(settings.auth_secret_key)
            dev = security.read_device_token(
                new_device_token, settings.auth_secret_key
            ) or ""
        _record_guest_use(dev, host)

    profile = persona.build_profile(
        works,
        site_counts={"favorites": 0, "statuses": 0},
        bangumi_total=len(bangumi_items),
        bilibili_total=len(bilibili_items),
        notes=notes,
    )
    exclude_ids = {
        int(work["subject_id"]) for work in works if work.get("subject_id")
    }
    recommendations = persona.recommend_from_site(db, profile, exclude_ids)
    _rarity = persona.attach_rarity(db, profile)
    payload = {
        "profile": profile,
        "report": persona.build_template_report(profile, _rarity),
        "prompt": persona.build_llm_prompt(
            profile, "sharp", recommendations=recommendations
        ),
        "radar": persona.build_radar(profile["dimensions"]),
        "recommendations": recommendations,
        "rarity": _rarity,
    }
    status_counts, type_counts = _guest_counts(bangumi_items)
    stub_report = SimpleNamespace(
        generated_at=datetime.now(),
        narrative=None,
        narrative_source="template",
        bangumi_username=name,
        tone="sharp",
    )
    # 结果页头部：谁 + 哪几个源 + 各自条数（模板只负责排版）
    targets = []
    if name:
        targets.append(name)
    if bili_mid:
        targets.append(f"B站 UID {bili_mid}")
    sources = []
    if bangumi_items:
        sources.append(f"Bangumi 收藏 {len(bangumi_items)} 条")
    if bilibili_items:
        sources.append(f"B站追番 {len(bilibili_items)} 条")

    result_id = crud.save_anonymous_persona_result(db, payload, source="preview")
    response = templates.TemplateResponse(
        request=request,
        name="persona.html",
        context={
            "guest_mode": True,
            "account": None,
            "collections_count": len(bangumi_items),
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
            "share_card_token": persona.encode_card(profile, payload.get("rarity")),
            "narrative": None,
            "narrative_source": "template",
            "bound_username": name,
            # 游客试算不提供「直接生成」：只给站点级配置的展示与手动贴回，
            # 用户级 Key 随登录态走（_llm_config 需要 request + db）。
            "llm": persona.resolve_llm_config(
                settings.persona_llm_provider,
                settings.persona_llm_base_url,
                settings.persona_llm_api_key,
                settings.persona_llm_model,
                settings.persona_llm_max_tokens,
            ),
            "llm_enabled": False,
            "llm_source": "none",
            "llm_key_masked": "",
            "llm_providers": persona.LLM_PROVIDERS,
            "bilibili_count": len(bilibili_items),
            "bilibili_status_counts": _guest_bilibili_counts(bilibili_rows),
            "bound_bilibili_mid": bili_mid,
            "guest_targets": " + ".join(targets),
            "guest_sources": " + ".join(sources),
            "guest_total": len(bangumi_items) + len(bilibili_items),
            "freshness_hint": "",
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
            "guest_bili_max_items": settings.persona_guest_bilibili_max_items,
            "result_kept_days": crud.ANONYMOUS_RESULT_TTL_DAYS,
        },
    )
    _attach_result_cookie(response, result_id)
    if new_device_token:
        response.set_cookie(
            GUEST_DEVICE_COOKIE,
            new_device_token,
            max_age=GUEST_DEVICE_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
    return response


# ---------- 选题式成分测试（引流主入口：不需要任何外部账号） ----------
#
# 「填 Bangumi 用户名」这条路人根本进不来——得先有一个 Bangumi 账号，还得知道它的
# 公开收藏页能匿名读。所以引流漏斗的断点一就卡在填用户名这一步。
# 这里改成用站内作品库出题：点选「看过 / 想看 / 无感」就能出画像，全程不联网、
# 不建账号、不写任何外部数据源，天然也不可能被当代理刷。
# 因此**不走上面的三层游客限流**（那是为「对外拉取」设计的闸门），只保留 20 秒
# 防抖挡住连点。
QUIZ_ROUTE = "/persona/quiz"
QUIZ_SIZES = (20, 100)
QUIZ_PAGE_SIZE = 20
# 最少要有几部正向作品才出结果：再少画像基本是噪音，不如直说
QUIZ_MIN_POSITIVE = 3
# 「换一批」的随机种子上限——只是抽题，不需要密码学强度
QUIZ_SEED_MAX = 1000000


def _quiz_size(value: object) -> int:
    """题量只认两档：其余值一律回落到默认档，避免被拼 URL 放大成全表查询。"""
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return QUIZ_PAGE_SIZE
    return size if size in QUIZ_SIZES else QUIZ_PAGE_SIZE


def _quiz_seed(value: object) -> int:
    """抽题种子归一到 [0, QUIZ_SEED_MAX)：非数字一律当 0。"""
    try:
        return abs(int(value)) % QUIZ_SEED_MAX  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@public_router.get(QUIZ_ROUTE, response_class=HTMLResponse)
def persona_quiz(request: Request, db: DbSession, size: str = "", seed: str = "0"):
    """选题式成分测试：按类目配额出题，点选后即可出画像。

    seed 同样收裸值后自行归一（并在下面把「下一批」的 seed 一起算好），
    不然 ?seed=abc 也会被 FastAPI 挡在门外。

    seed 与 size 决定这一批题面，同参数刷新不换题——选到一半刷新不该重来。
    """
    picked_size = _quiz_size(size)
    seed_value = _quiz_seed(seed)
    works = crud.sample_quiz_contents(db, size=picked_size, seed=seed_value)
    current_user = getattr(request.state, "current_user", None)
    return templates.TemplateResponse(
        request=request,
        name="persona_quiz.html",
        context={
            "works": works,
            "size": picked_size,
            "seed": seed_value,
            "next_seed": (seed_value + 1) % QUIZ_SEED_MAX,
            "sizes": QUIZ_SIZES,
            "min_positive": QUIZ_MIN_POSITIVE,
            "logged_in": current_user is not None,
            "type_labels": persona.SITE_TYPE_LABELS,
        },
    )


def _render_quiz_form(
    request: Request, db: DbSession, *, size: int, seed: int, error: str = ""
) -> HTMLResponse:
    """重画选题页：回到同一批题面，带上提示语（提交失败 / 样本不足时都能复用）。"""
    return templates.TemplateResponse(
        request=request,
        name="persona_quiz.html",
        context={
            "works": crud.sample_quiz_contents(db, size=size, seed=seed),
            "size": size,
            "seed": seed,
            "next_seed": (seed + 1) % QUIZ_SEED_MAX,
            "sizes": QUIZ_SIZES,
            "min_positive": QUIZ_MIN_POSITIVE,
            "logged_in": getattr(request.state, "current_user", None) is not None,
            "type_labels": persona.SITE_TYPE_LABELS,
            "error": error,
        },
    )


@public_router.post(QUIZ_ROUTE, response_class=HTMLResponse)
def persona_quiz_result(
    request: Request,
    db: DbSession,
    picks: list[str] = Form([]),
    seen: list[str] = Form([]),
    want: list[str] = Form([]),
    size: str = Form(""),
    seed: str = Form("0"),
):
    """按点选结果算画像并渲染结果页（复用游客试算那一套渲染）。

    seen / want 两组 checkbox 分别提交 id 列表（同一作品同时勾上时以 seen 为准——
    先看过的更可信）。**「无感」不进 works**：它只是没看过，不是负向信号；塞进
    interest 会把没看过的作品也算成口味，画像直接失真。
    """
    picked_size = _quiz_size(size)
    seed_value = _quiz_seed(seed)
    host = request.client.host if request.client else "unknown"
    wait = _guest_cooldown_left(host)
    if wait:
        return _render_quiz_form(
            request,
            db,
            size=picked_size,
            seed=seed_value,
            error=f"点太快了，请 {wait} 秒后再提交。",
        )

    chosen: dict[int, str] = {}
    # seen 在前：同一 id 两组都勾上时以 seen 为准
    for group, status in ((seen, "seen"), (want, "want")):
        for raw in group or []:
            if str(raw).strip().isdigit():
                chosen.setdefault(int(str(raw).strip()), status)
    # 上限兜底：提交条数不该超过这一批的题面数量
    if len(chosen) > picked_size:
        chosen = dict(list(chosen.items())[:picked_size])

    if len(chosen) < QUIZ_MIN_POSITIVE:
        return _render_quiz_form(
            request,
            db,
            size=picked_size,
            seed=seed_value,
            error=f"至少标记 {QUIZ_MIN_POSITIVE} 部看过或想看的，才够算出画像。",
        )

    contents = crud.list_contents_by_ids(db, list(chosen))
    tags_map = crud.list_content_tags_map(db, [row.id for row in contents])
    works = [
        persona.work_from_site(row, chosen[row.id], tags_map.get(row.id, []))
        for row in contents
    ]
    if not works:
        return _render_quiz_form(
            request,
            db,
            size=picked_size,
            seed=seed_value,
            error="这批作品读不到了，换个种子再来一次。",
        )

    _guest_hits[host] = time.monotonic()
    notes = [f"依据你在当日选题里标记的 {len(works)} 部作品"]
    profile = persona.build_profile(
        works,
        site_counts={"favorites": 0, "statuses": len(works)},
        notes=notes,
    )
    # 排除已标记过的：推荐要避开这批题面里的作品，否则等于把自己选过的推回来
    exclude_ids = {int(row.bangumi_id) for row in contents if row.bangumi_id is not None}
    recommendations = persona.recommend_from_site(db, profile, exclude_ids)
    _rarity = persona.attach_rarity(db, profile)
    payload = {
        "profile": profile,
        "report": persona.build_template_report(profile, _rarity),
        "prompt": persona.build_llm_prompt(
            profile, "sharp", recommendations=recommendations
        ),
        "radar": persona.build_radar(profile["dimensions"]),
        "recommendations": recommendations,
        "rarity": _rarity,
    }
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for row in contents:
        status_counts[chosen[row.id]] = status_counts.get(chosen[row.id], 0) + 1
        type_counts[row.type] = type_counts.get(row.type, 0) + 1

    # 结果留一份快照：注册后凭 Cookie 领回（游客自己刷新也能再打开这张卡）
    result_id = crud.save_anonymous_persona_result(db, payload, source="quiz")
    response = templates.TemplateResponse(
        request=request,
        name="persona.html",
        context={
            "guest_mode": True,
            "account": None,
            "collections_count": len(works),
            "status_counts": status_counts,
            "type_counts": type_counts,
            "type_labels": persona.SITE_TYPE_LABELS,
            "status_labels": persona.STATUS_LABELS,
            "report": SimpleNamespace(
                generated_at=datetime.now(),
                narrative=None,
                narrative_source="template",
                bangumi_username="",
                tone="sharp",
            ),
            "payload": payload,
            "profile": profile,
            "template_report": payload["report"],
            "radar": payload["radar"],
            "prompt": payload["prompt"],
            "share_card_token": persona.encode_card(profile, payload.get("rarity")),
            "narrative": None,
            "narrative_source": "template",
            "bound_username": "",
            "llm": persona.resolve_llm_config(
                settings.persona_llm_provider,
                settings.persona_llm_base_url,
                settings.persona_llm_api_key,
                settings.persona_llm_model,
                settings.persona_llm_max_tokens,
            ),
            "llm_enabled": False,
            "llm_source": "none",
            "llm_key_masked": "",
            "llm_providers": persona.LLM_PROVIDERS,
            "bilibili_count": 0,
            "bilibili_status_counts": {},
            "bound_bilibili_mid": "",
            "guest_targets": "当日选题",
            "guest_sources": f"站内选题 {len(works)} 条",
            "guest_total": len(works),
            "freshness_hint": "",
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
            "guest_bili_max_items": settings.persona_guest_bilibili_max_items,
            "quiz_back_url": f"{QUIZ_ROUTE}?{urlencode({'size': picked_size, 'seed': seed})}",
            "result_kept_days": crud.ANONYMOUS_RESULT_TTL_DAYS,
        },
    )
    _attach_result_cookie(response, result_id)
    return response


# 分享卡：结果页唯一的「可晒物」。参数全部由结果页编码进 URL，服务端不查库、
# 不落库——分享出去的那张卡不该绑定谁算出来的（也省掉一张表）。
SHARE_CARD_ROUTE = "/persona/card"


@public_router.get(SHARE_CARD_ROUTE)
def persona_share_card(d: str = ""):
    """竖版分享卡（image/svg+xml）：浏览器可直接预览，也可另存为图片。"""
    data = persona.decode_card(d)
    if data is None:
        raise HTTPException(status_code=404, detail="分享卡参数无效")
    return Response(
        content=persona.build_share_card(data),
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": 'inline; filename="otaku-persona.svg"',
            # 卡片内容完全由 URL 决定，可以放心让浏览器和 CDN 缓存
            "Cache-Control": "public, max-age=600",
        },
    )
