"""B站追番表读取：匿名公开接口，无需登录 / cookie（2026-10-20 实测可用）。

- ``GET https://api.bilibili.com/x/space/bangumi/follow/list?vmid=<UID>&type=1&pn&ps=30``
  匿名即返 ``code 0``：分页 ``ps`` 上限 30，``data.total`` 给总数；
- 只取 ``type=1``（番剧 + 国创，都算动画）；影视（type=2，纪录片 / 电视剧）
  不在 ACGN 范围，不拉；
- 错误码：``53013`` = 用户把追番列表设成隐私（页面给公开引导）；
  ``-352`` = 风控（UA / 频率问题）；其它非 0 统一转 PersonaError；
- 每条自带 styles（题材标签，缺的是真没打）、rating（B站评分，部分条目没有）、
  publish.release_date（年份）、stat.follow（热度）、follow_status
  （1 想看 / 2 在看 / 3 看过，实测按状态过滤真实生效）、series/season_title
  （系列分季，跨源去重要用）。

设计同 persona 的 Bangumi 拉取：opener / get_json 本地实现（B站要浏览器 UA、
节奏更慢，不与 Bangumi 共用），sleep 可注入便于测试。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from app.persona import PersonaError

BILIBILI_API_BASE = "https://api.bilibili.com"
# 接口 ps 上限 30（传 100 直接被风控拒返），翻页拉取。
BILIBILI_PAGE_SIZE = 30
# 最多翻 40 页（1200 条）：防接口异常时的死循环，正常追番表远到不了。
# follow_status 原样存快照（1 想看 / 2 在看 / 3 看过），统一状态映射在
# persona.BILIBILI_FOLLOW_STATUS（分析侧口径，避免循环导入）。
BILIBILI_MAX_PAGES = 40
# season_type：1 番剧 / 4 国创（均为动画）；2 影视、3 纪录片等不在 ACGN 范围。
BILIBILI_ACCEPT_TYPES = frozenset({1, 4})

# B站风控对默认 urllib UA 敏感，默认值给全形态浏览器 UA（可用 settings 覆盖）。
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _build_opener(proxy: str = ""):
    """构造 opener；proxy 为空时沿用系统 / 环境变量代理。"""
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    return urllib.request.build_opener(handler)


def _get_json(opener, url: str, user_agent: str, timeout: float, retries: int = 2, sleep=time.sleep) -> dict:
    """GET JSON；网络错误与 5xx 重试，其余转 PersonaError。"""
    last_reason = "未知错误"
    for attempt in range(retries + 1):
        if attempt:
            sleep(min(0.5 * 2 ** (attempt - 1), 4))
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": user_agent, "Accept": "application/json"}
            )
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_reason = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                continue
            raise PersonaError(last_reason) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_reason = f"网络错误：{exc}"
            if attempt < retries:
                continue
            raise PersonaError(last_reason) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_reason = f"响应不是有效 JSON：{exc}"
            if attempt < retries:
                continue
            raise PersonaError(last_reason) from exc
    raise PersonaError(last_reason)


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def error_message(code: int, message: str = "") -> str:
    """把 B站错误码翻成用户可操作的一句话（含隐私引导）。"""
    if code == 53013:
        return "该用户的追番列表未公开：B站 → 个人空间 → 空间设置 → 打开「公开我的追番列表」后再同步"
    if code == -352:
        return "触发 B站 风控（-352），请稍后再试"
    if code in (-404, -400, 61004):
        return "UID 不存在或接口暂时不可用，请检查数字 UID"
    return f"B站接口返回错误（{code}）：{message or '未知原因'}"


def normalize_item(raw: dict) -> dict | None:
    """把追番列表条目转成快照字段；非 ACGN 类型 / 无标题返回 None。"""
    season_id = _to_int(raw.get("season_id"))
    if not season_id:
        return None
    season_type = _to_int(raw.get("season_type")) or 0
    if season_type not in BILIBILI_ACCEPT_TYPES:
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None

    tags = [
        str(tag).strip()
        for tag in (raw.get("styles") or [])
        if str(tag).strip()
    ]
    rating = raw.get("rating") or {}
    publish = raw.get("publish") or {}
    series = raw.get("series") or {}
    stat = raw.get("stat") or {}
    release = str(publish.get("release_date") or publish.get("pub_time") or "")
    year = int(release[:4]) if release[:4].isdigit() else None
    return {
        "season_id": season_id,
        "title": title,
        "season_type": season_type,
        "season_type_name": str(raw.get("season_type_name") or "").strip(),
        "follow_status": _to_int(raw.get("follow_status")) or 0,
        "rating": _to_float(rating.get("score")) if isinstance(rating, dict) else None,
        "follow_count": _to_int(stat.get("follow")) or 0,
        "year": year,
        "tags": tags,
        "series_title": str(series.get("title") or "").strip() or None,
        "season_title": str(raw.get("season_title") or "").strip() or None,
        "is_finish": bool(raw.get("is_finish")),
    }


def fetch_user_follows(
    mid: str,
    *,
    user_agent: str = "",
    timeout: float = 15.0,
    max_items: int = 500,
    retries: int = 2,
    sleep=time.sleep,
) -> tuple[list[dict], int]:
    """读取某 UID 的公开追番列表，返回 (快照列表, 接口报告的总数)。

    mid 为数字 UID（个人空间地址栏那串数字）。分页拉取，页间 sleep 降频，
    避免触发风控。接口层错误（隐私 / 风控 / UID 不存在）转 PersonaError，
    消息带处理引导。
    """
    mid = (mid or "").strip()
    if not mid:
        raise ValueError("请填写 B站 UID")
    if not mid.isdigit():
        raise ValueError("B站 UID 应为纯数字（个人空间地址栏那串数字）")
    if len(mid) > 12:
        raise ValueError("UID 过长")

    opener = _build_opener()
    items: list[dict] = []
    reported_total = 0
    for page in range(1, BILIBILI_MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {"vmid": mid, "type": 1, "pn": page, "ps": BILIBILI_PAGE_SIZE}
        )
        url = f"{BILIBILI_API_BASE}/x/space/bangumi/follow/list?{query}"
        payload = _get_json(
            opener, url, user_agent or _DEFAULT_UA, timeout, retries=retries, sleep=sleep
        )
        code = _to_int(payload.get("code")) or 0
        if code != 0:
            raise PersonaError(
                error_message(code, str(payload.get("message") or ""))
            )
        data = payload.get("data") or {}
        rows = data.get("list") or []
        reported_total = _to_int(data.get("total")) or reported_total
        for raw in rows:
            if len(items) >= max_items:
                break
            normalized = normalize_item(raw)
            if normalized is not None:
                items.append(normalized)
        if len(items) >= max_items or not rows or len(rows) < BILIBILI_PAGE_SIZE:
            break
        if reported_total and page * BILIBILI_PAGE_SIZE >= reported_total:
            break
        sleep(0.3)
    return items, (reported_total or len(items))
