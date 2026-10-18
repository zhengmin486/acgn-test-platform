"""阿宅画像：Bangumi 公开收藏拉取 + 规则画像 + LLM 提示词 / 调用。

设计要点：
- 只用 Bangumi 公开收藏接口（无需 OAuth），私有条目读不到；
- 规则引擎负责所有分数与事实，LLM 只负责把事实写成文案，禁止编造；
- 「避雷类型」来自抛弃（collection_type=5）与低分（rate<=4），
  并从正向高频标签中剔除，避免把喜欢的东西误判成避雷；
- Bangumi 不提供首次收藏时间，「入坑年份」用作品发行年近似，报告里会标注。
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

from app.models import BangumiCollection
from app.tagging import (
    GENRE_TAGS,
    STUDIO_ALIASES,
    TAG_CLUSTERS,
    is_genre_tag,
)

SITE_TYPE_LABELS = {
    "anime": "动画",
    "comic": "漫画",
    "game": "游戏",
    "novel": "轻小说",
}

# 主身份：按类型占比给出的标签
IDENTITY_LABELS = {
    "anime": "番剧党",
    "comic": "漫画党",
    "game": "游戏宅",
    "novel": "轻小说党",
}

# Bangumi collection_type：1 想看 / 2 在看 / 3 看过 / 4 搁置 / 5 抛弃
COLLECTION_STATUS = {1: "want", 2: "seen", 3: "seen", 4: "hold", 5: "drop"}
STATUS_LABELS = {
    "seen": "看过",
    "want": "想看",
    "hold": "搁置",
    "drop": "抛弃",
    "interest": "收藏",
}

# 标签归一（题材 / 公司 / 年份）统一在 app/tagging.py，画像与入库共用一套口径。
_is_genre_tag = is_genre_tag

DIMENSION_LABELS = [
    ("type_focus", "类型集中"),
    ("tag_focus", "题材垂直"),
    ("mainstream", "大众度"),
    ("exploration", "探索广度"),
    ("hype", "追更强度"),
    ("completion", "完成度"),
]

TONE_LABELS = {"sharp": "善意锐评", "gentle": "温和中性", "custom": "自定义语气"}

# 预设语气的具体措辞；custom 走用户自己填的说明。
TONE_PRESETS = {
    "sharp": "善意锐评：可以玩梗、犀利、有一点点毒舌，但不人身攻击、不贬低用户。",
    "gentle": "温和中性：客观、友善，不玩梗、不毒舌。",
}

# 自定义语气说明 / 追加要求长度上限（防止把整篇范文塞进提示词）。
MAX_TONE_PROMPT = 300
MAX_EXTRA_PROMPT = 2000


class PersonaError(Exception):
    """Bangumi / LLM 请求或数据格式错误。"""


# ---------- 小工具 ----------


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def _year(date_text: str | None) -> int | None:
    if not date_text:
        return None
    head = str(date_text)[:4]
    return int(head) if head.isdigit() else None


def _avg(values) -> float | None:
    items = [float(v) for v in values if v is not None]
    return sum(items) / len(items) if items else None


def _round(value: float) -> float:
    return round(max(0.0, min(100.0, float(value))), 1)


def _build_opener(proxy: str = ""):
    """构造 opener；proxy 为空时沿用系统 / 环境变量代理。"""
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    return urllib.request.build_opener(handler)


def _get_json(opener, url: str, user_agent: str, timeout: float, retries: int = 2, sleep=time.sleep) -> dict:
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


# ---------- Bangumi 拉取 ----------


def normalize_item(raw: dict) -> dict | None:
    """把 Bangumi 收藏条目转成快照字段；非动画 / 书籍 / 游戏返回 None。"""
    subject = raw.get("subject") or {}
    subject_type = _to_int(raw.get("subject_type")) or _to_int(subject.get("type"))
    if subject_type not in (1, 2, 4):
        return None
    if raw.get("private") is True:
        # 公开接口正常不会返回私有条目；万一返回也跳过，避免把隐私数据带进分析。
        return None

    subject_tags: list[dict] = []
    for tag in subject.get("tags") or []:
        if isinstance(tag, dict):
            name = str(tag.get("name") or "").strip()
            count = _to_int(tag.get("count")) or 0
        else:
            name = str(tag).strip()
            count = 0
        if name:
            subject_tags.append({"name": name, "count": count})

    name = str(subject.get("name") or subject.get("name_cn") or "").strip()
    name_cn = str(subject.get("name_cn") or "").strip() or None
    if not name and not name_cn:
        return None

    return {
        "subject_id": _to_int(raw.get("subject_id")) or _to_int(subject.get("id")) or 0,
        "subject_type": subject_type,
        "name": (name or name_cn or "")[:300],
        "name_cn": name_cn,
        "date": str(subject.get("date") or "").strip() or None,
        "score": _to_float(subject.get("score")),
        "rank": _to_int(subject.get("rank")),
        "collection_total": _to_int(subject.get("collection_total")),
        "rate": _to_float(raw.get("rate")) or 0.0,
        "collection_type": _to_int(raw.get("type")) or 0,
        "private": bool(raw.get("private")),
        "subject_tags": subject_tags,
        "user_tags": [str(tag).strip() for tag in (raw.get("tags") or []) if str(tag).strip()],
        "updated_at": _parse_dt(raw.get("updated_at")),
    }


def fetch_user_collections(
    username: str,
    *,
    base_url: str,
    user_agent: str,
    proxy: str = "",
    timeout: float = 15.0,
    max_items: int = 1000,
    retries: int = 2,
    sleep=time.sleep,
) -> tuple[list[dict], int]:
    """读取某用户的公开 Bangumi 收藏，返回 (快照列表, 接口报告的总数)。

    只保留动画 / 书籍 / 游戏三类；分页拉取，带超时与重试，页间 sleep 避免触发限流。
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("请填写 Bangumi 用户名")
    if len(username) > 100:
        raise ValueError("用户名过长")

    opener = _build_opener(proxy)
    items: list[dict] = []
    offset = 0
    reported_total: int | None = None

    while len(items) < max_items:
        query = urllib.parse.urlencode({"limit": 50, "offset": offset})
        url = f"{base_url.rstrip('/')}/v0/users/{urllib.parse.quote(username)}/collections?{query}"
        payload = _get_json(opener, url, user_agent, timeout, retries=retries, sleep=sleep)
        data = payload.get("data") or []
        if reported_total is None:
            reported_total = _to_int(payload.get("total"))
        for raw in data:
            if len(items) >= max_items:
                break
            normalized = normalize_item(raw)
            if normalized is not None:
                items.append(normalized)
        if not data or len(data) < 50:
            break
        offset += 50
        if reported_total is not None and offset >= reported_total:
            break
        sleep(0.25)

    return items, (reported_total or len(items))


# ---------- 统一作品结构 / 画像 ----------


def _site_type(subject_type: int, tags: list[dict] | None) -> str | None:
    """Bangumi 类型映射到站内类型；书籍按标签区分漫画 / 轻小说。"""
    if subject_type == 2:
        return "anime"
    if subject_type == 4:
        return "game"
    if subject_type == 1:
        names = " ".join(
            str(tag.get("name", "")) if isinstance(tag, dict) else str(tag)
            for tag in (tags or [])
        )
        return "comic" if "漫画" in names else "novel"
    return None


def work_from_bangumi(row: BangumiCollection) -> dict | None:
    site_type = _site_type(int(row.subject_type), row.subject_tags)
    if site_type is None:
        return None
    tags = [
        str(tag.get("name"))
        for tag in (row.subject_tags or [])
        if isinstance(tag, dict) and tag.get("name")
    ]
    if not tags:
        tags = [str(tag) for tag in (row.user_tags or []) if str(tag).strip()]
    return {
        "subject_id": row.subject_id,
        "title": row.name_cn or row.name,
        "type": site_type,
        "type_label": SITE_TYPE_LABELS.get(site_type, site_type),
        "year": _year(row.date),
        "status": COLLECTION_STATUS.get(int(row.collection_type or 0), "interest"),
        "tags": tags,
        "score": row.score,
        "popularity": row.collection_total,
        "rate": row.rate or 0,
        "source": "bangumi",
    }


def work_from_site(content, status: str, tags: list[str]) -> dict:
    """站内收藏 / 标注转成统一作品结构（没有年份与热度，只参与类型与标签统计）。"""
    mapped_status = {"seen": "seen", "want": "want", "unseen": "interest"}.get(
        status, "interest"
    )
    return {
        "title": content.title,
        "type": content.type,
        "year": None,
        "status": mapped_status,
        "tags": [str(tag) for tag in (tags or []) if str(tag).strip()],
        "score": content.rating,
        "popularity": None,
        "rate": 0,
        "source": "site",
    }


def _dedupe(works: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result = []
    for work in works:
        key = (work.get("source", ""), work.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(work)
    return result


def _identity(type_shares: dict[str, float]) -> str:
    if not type_shares:
        return "杂食党"
    best_type, best_share = max(type_shares.items(), key=lambda pair: pair[1])
    if best_share < 50:
        return "杂食党"
    return IDENTITY_LABELS.get(best_type, "杂食党")


def _cluster(tag_counter: Counter) -> str:
    best_cluster, best_score = "杂食", 0
    for cluster, names in TAG_CLUSTERS.items():
        score = sum(tag_counter.get(name, 0) for name in names)
        if score > best_score:
            best_cluster, best_score = cluster, score
    return best_cluster


def _tag_focus(tag_counter: Counter) -> float:
    total = sum(tag_counter.values())
    if not total:
        return 0.0
    top3 = sum(count for _, count in tag_counter.most_common(3))
    return _round(top3 / total * 180)


def _mainstream(avg_popularity: float | None) -> float:
    if not avg_popularity:
        return 50.0
    return _round((math.log10(avg_popularity) - 2) / 4 * 100)


def _traits(
    dimensions: dict, want: int, seen: int, avg_score: float | None
) -> list[str]:
    traits: list[str] = []
    if seen and want / max(1, seen) >= 1.5:
        traits.append("囤积型")
    if dimensions["mainstream"] < 40:
        traits.append("冷门挖掘机")
    if dimensions["tag_focus"] >= 60:
        traits.append("专精型")
    if dimensions["exploration"] >= 70:
        traits.append("广撒网")
    if avg_score and avg_score >= 8:
        traits.append("高分党")
    return traits[:2]


def _representative(works: list[dict]) -> list[dict]:
    ranked = sorted(
        works,
        key=lambda w: (w.get("score") or 0, w.get("popularity") or 0),
        reverse=True,
    )
    result, seen_titles = [], set()
    for work in ranked:
        title = work.get("title")
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        result.append(
            {
                "title": title,
                "type": work.get("type"),
                "year": work.get("year"),
                "score": work.get("score"),
            }
        )
        if len(result) >= 5:
            break
    return result


def build_profile(
    works: list[dict],
    *,
    site_counts: dict | None = None,
    bangumi_total: int = 0,
    notes: list[str] | None = None,
) -> dict:
    """由统一作品结构计算画像：类型、标签、避雷、时间线、六维分数、人格标签。"""
    site_counts = site_counts or {}
    interest = [w for w in works if w.get("status") in ("seen", "want", "interest")]
    seen = [w for w in interest if w["status"] == "seen"]
    want = [w for w in interest if w["status"] == "want"]
    hold = [w for w in works if w.get("status") == "hold"]
    drops = [w for w in works if w.get("status") == "drop"]
    low_rated = [
        w for w in interest if w.get("rate") and float(w["rate"]) <= 4
    ]
    negative = _dedupe(drops + low_rated)
    total = len(interest)

    type_counter = Counter(
        w["type"] for w in interest if w.get("type") in SITE_TYPE_LABELS
    )
    type_shares = (
        {key: round(value / total * 100, 1) for key, value in type_counter.items()}
        if total
        else {}
    )

    tag_counter: Counter = Counter()
    for work in interest:
        for tag in set(work.get("tags") or []):
            if tag:
                tag_counter[tag] += 1
    top_tags = [
        {"name": name, "count": count} for name, count in tag_counter.most_common(12)
    ]
    genre_counter: Counter = Counter(
        {tag: count for tag, count in tag_counter.items() if _is_genre_tag(tag)}
    )
    top_genres = [
        {"name": name, "count": count} for name, count in genre_counter.most_common(8)
    ]

    negative_counter: Counter = Counter()
    for work in negative:
        for tag in set(work.get("tags") or []):
            if tag:
                negative_counter[tag] += 1
    # 正向作品里出现过的标签不算避雷（避免「日常」这种两边都有的误伤）
    positive_names = set(tag_counter.keys())
    avoided_tags = [
        {"name": name, "count": count}
        for name, count in negative_counter.most_common(50)
        if _is_genre_tag(name) and name not in positive_names
    ][:8]

    studio_counter: Counter = Counter()
    for work in interest:
        for tag in set(work.get("tags") or []):
            for canonical, aliases in STUDIO_ALIASES.items():
                if tag in aliases:
                    studio_counter[canonical] += 1
    top_studios = [
        {"name": name, "count": count}
        for name, count in studio_counter.most_common(5)
    ]

    year_items = [w for w in interest if w.get("year")]
    entry = min(year_items, key=lambda w: w["year"]) if year_items else None
    latest = max(year_items, key=lambda w: w["year"]) if year_items else None

    avg_popularity = _avg(
        [w.get("popularity") for w in interest if w.get("popularity")]
    )
    avg_score = _avg([w.get("score") for w in interest if w.get("score")])

    dimensions = {
        "type_focus": _round(max(type_shares.values()) if type_shares else 0),
        "tag_focus": _tag_focus(genre_counter),
        "mainstream": _mainstream(avg_popularity),
        "exploration": _round(min(100, len(tag_counter) * 4)),
        "hype": _round(min(100, (len(want) / max(1, len(seen))) * 50)),
        "completion": _round(len(seen) / max(1, len(interest)) * 100),
    }

    identity = _identity(type_shares)
    cluster = _cluster(genre_counter)
    traits = _traits(dimensions, len(want), len(seen), avg_score)

    return {
        "total": total,
        "seen": len(seen),
        "want": len(want),
        "hold": len(hold),
        "drop": len(drops),
        "negative": len(negative),
        "bangumi_total": bangumi_total,
        "site_counts": site_counts,
        "type_counts": dict(type_counter),
        "type_shares": type_shares,
        "top_tags": top_tags,
        "top_genres": top_genres,
        "avoided_tags": avoided_tags,
        "top_studios": top_studios,
        "entry_year": entry["year"] if entry else None,
        "latest_year": latest["year"] if latest else None,
        "entry_work": entry["title"] if entry else None,
        "latest_work": latest["title"] if latest else None,
        "avg_score": round(avg_score, 2) if avg_score else None,
        "avg_popularity": int(avg_popularity) if avg_popularity else None,
        "dimensions": dimensions,
        "identity": identity,
        "cluster": cluster,
        "persona_tags": [f"{cluster}系", identity] + traits,
        "representative": _representative(interest),
        "notes": notes or [],
    }


def build_template_report(profile: dict) -> dict:
    """不依赖 LLM 的兜底报告：同样覆盖样例里的 11 个模块。"""
    top_names = [
        tag["name"] for tag in (profile.get("top_genres") or profile["top_tags"])
    ]
    avoided_names = [tag["name"] for tag in profile["avoided_tags"]]
    studios = [studio["name"] for studio in profile["top_studios"]]
    traits = profile["persona_tags"][2:]
    entry_year, latest_year = profile["entry_year"], profile["latest_year"]
    entry_work, latest_work = profile["entry_work"], profile["latest_work"]
    total, want, seen = profile["total"], profile["want"], profile["seen"]

    title = f"「{profile['cluster']}系{profile['identity']}」" + (
        f"兼「{traits[0]}」" if traits else ""
    )
    if entry_year and latest_year:
        entry_story = (
            f"按作品发行年看，你的收藏从 {entry_year} 年的《{entry_work}》"
            f"一直延伸到 {latest_year} 年的《{latest_work}》，跨度约 "
            f"{max(0, latest_year - entry_year)} 年"
            "（Bangumi 不提供首次收藏时间，这里只作发行年跨度参考）。"
        )
    else:
        entry_story = "收藏里还没有足够的时间信息，暂时画不出时间线。"

    if want > seen * 1.5:
        advice = "想看远多于看过，先把囤积清单消化一部分，别让收藏夹变成纪念碑。"
    elif profile["dimensions"]["mainstream"] < 40:
        advice = "你偏爱冷门作品，继续按自己的标签挖就好，不必强迫自己补所谓神作。"
    elif profile["dimensions"]["tag_focus"] >= 60:
        advice = "口味很集中，按你的高频标签找同类型新作，命中率会很高。"
    else:
        advice = "口味比较均衡，可以偶尔跳出舒适区，但不必勉强。"

    return {
        "title": title,
        "subtitle": f"基于 {total} 部作品的口味画像",
        "entry_story": entry_story,
        "core_taste": (
            f"你的口味集中在 {'、'.join(top_names[:5]) or '暂不明显'}，"
            f"{profile['identity']}属性最突出。"
        ),
        "dependencies": f"常出现的制作公司：{'、'.join(studios) or '数据不足'}。",
        "favorite_genres": top_names[:8],
        "avoided_genres": avoided_names[:6],
        "persona_tags": profile["persona_tags"],
        "advice": advice,
        "final_quip": (
            f"看了 {total} 部，你的审美没有滑坡，只是越来越像你自己。"
        ),
        "data_note": (
            f"Bangumi {profile['bangumi_total']} 条 + 站内收藏 "
            f"{profile['site_counts'].get('favorites', 0)} 条；看过 {seen} / "
            f"想看 {want} / 抛弃 {profile['drop']}。Bangumi 不提供首次收藏时间，"
            "入坑年份用作品发行年近似；避雷标签来自抛弃 / 低分作品。"
        ),
    }


def build_llm_prompt(
    profile: dict,
    tone: str = "sharp",
    tone_prompt: str = "",
    extra_prompt: str = "",
) -> str:
    """把事实清单与输出要求拼成可直接粘贴给 LLM 的提示词。

    - tone：sharp / gentle 用内置措辞；custom 用用户填的 tone_prompt；
    - extra_prompt：用户追加的大段要求，原样附在硬性要求之后（优先级最高），
      但「不得编造」等安全底线仍然保留在前面，避免被覆盖。
    """
    custom_tone = (tone_prompt or "").strip()[:MAX_TONE_PROMPT]
    if tone == "custom":
        tone_text = custom_tone or "自然、清晰、有个人风格，不要端着说教。"
    else:
        tone_text = TONE_PRESETS.get(tone, TONE_PRESETS["sharp"])
    extra = (extra_prompt or "").strip()[:MAX_EXTRA_PROMPT]
    extra_block = (
        "额外要求（用户自定义，在不违反上面硬性要求的前提下优先满足）：\n"
        f"{extra}\n\n"
        if extra
        else ""
    )
    facts = {
        "总作品数": profile["total"],
        "看过": profile["seen"],
        "想看": profile["want"],
        "抛弃": profile["drop"],
        "类型分布": profile["type_shares"],
        "标签Top": profile["top_tags"],
        "本命类型": profile.get("top_genres") or profile["top_tags"],
        "避雷标签": profile["avoided_tags"],
        "制作公司Top": profile["top_studios"],
        "收藏跨度(按发行年近似)": {
            "最早年份": profile["entry_year"],
            "最早作品": profile["entry_work"],
            "最近年份": profile["latest_year"],
            "最近作品": profile["latest_work"],
        },
        "六维分数": profile["dimensions"],
        "人格标签": profile["persona_tags"],
        "代表作": profile["representative"],
        "站内收藏数": profile["site_counts"].get("favorites", 0),
    }
    return (
        "你是一个懂 ACGN 的「阿宅画像分析师」。请根据下面的事实清单，用中文写一份人格报告。\n\n"
        f"语气要求：{tone_text}\n\n"
        "必须包含这些部分，用「## 部分名」分段：\n"
        "1. 称号（两个词组组合，例如「美少女动物园园长」兼「扭曲情感研究学者」）\n"
        "2. 入坑时间线 / 收藏跨度（按作品发行年近似，不要编造首次收藏时间）\n"
        "3. 核心 XP\n"
        "4. 类型 / 公司依赖\n"
        "5. 本命类型\n"
        "6. 避雷类型\n"
        "7. 二次元人格（3 个标签）\n"
        "8. 建议\n"
        "9. 最后锐评（一句话）\n\n"
        "硬性要求：\n"
        "- 只能引用事实清单里出现的作品名、年份、标签、公司，不得编造。\n"
        "- 数据不足的部分直接说明，不要硬编。\n"
        "- 输出纯文本，不要 Markdown 表格。\n\n"
        f"{extra_block}"
        "事实清单：\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}"
    )


# ---------- LLM 接口预设 ----------
# 国内三家主流接口都兼容 OpenAI 的 /chat/completions 协议，差异只在 base_url 与模型名。
# 只填 PERSONA_LLM_PROVIDER + PERSONA_LLM_API_KEY 即可用；显式填的 base_url / model 优先，
# 因此也能指向任意兼容服务（通义、Moonshot、本地 Ollama 等）。
LLM_PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "console": "https://platform.deepseek.com/api_keys",
    },
    "doubao": {
        "label": "豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        # 方舟没有统一的模型名，必须用控制台创建的推理接入点 ID 或模型名。
        "model": "",
        "console": "https://console.volcengine.com/ark",
    },
    "glm": {
        "label": "GLM（智谱）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "console": "https://open.bigmodel.cn/usercenter/apikeys",
    },
}

# 仅用于「没填 provider」时根据地址推断展示名，不影响调用。
LLM_HOST_LABELS = (
    ("api.deepseek.com", "DeepSeek"),
    ("volces.com", "豆包（火山方舟）"),
    ("bigmodel.cn", "GLM（智谱）"),
    ("dashscope.aliyuncs.com", "通义千问"),
    ("moonshot.cn", "Kimi"),
)


def resolve_llm_config(
    provider: str = "",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens: int = 0,
) -> dict:
    """合并 provider 预设与显式配置，返回统一配置（含是否可用与给用户的提示）。

    优先级：显式 base_url / model > provider 预设。provider 不认识时忽略预设，
    只要 base_url + api_key + model 齐就按「自定义 OpenAI 兼容接口」使用。
    """
    key = (provider or "").strip().lower()
    preset = LLM_PROVIDERS.get(key, {})
    resolved_base = (base_url or "").strip().rstrip("/") or preset.get("base_url", "")
    resolved_model = (model or "").strip() or preset.get("model", "")
    resolved_key = (api_key or "").strip()

    label = preset.get("label", "")
    if not label:
        host = urllib.parse.urlparse(resolved_base).netloc.lower() if resolved_base else ""
        label = next(
            (name for suffix, name in LLM_HOST_LABELS if suffix in host), ""
        ) or ("自定义接口" if host else "未配置")

    ready = bool(resolved_base and resolved_key and resolved_model)
    if not resolved_base:
        hint = "未配置接口；可以直接复制提示词到千问 / 豆包 / DeepSeek 网页版使用。"
    elif not resolved_key:
        hint = f"缺少 API Key（{label}）。"
    elif not resolved_model:
        hint = (
            "豆包需要在火山方舟控制台创建推理接入点，把接入点 ID（ep-…）或模型名"
            "填到 PERSONA_LLM_MODEL。"
        )
    else:
        hint = ""

    return {
        "provider": key,
        "label": label,
        "base_url": resolved_base,
        "api_key": resolved_key,
        "model": resolved_model,
        "max_tokens": int(max_tokens or 0),
        "ready": ready,
        "hint": hint,
        "console": preset.get("console", ""),
    }


def _llm_error_message(exc: urllib.error.HTTPError) -> str:
    """把各家的错误体压成一句可读提示（Key 错 / 模型错 / 限流）。"""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 读不到 body 也不该掩盖原错误
        raw = ""
    detail = ""
    try:
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or "")
        elif isinstance(error, str):
            detail = error
        elif isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("msg") or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        detail = raw
    detail = " ".join(str(detail).split())[:160]

    tips = {
        400: "请求被拒绝（常见：模型名不存在、参数不合法）",
        401: "API Key 无效或未通过鉴权",
        402: "账户余额不足",
        403: "无权限（模型未开通或未实名）",
        404: "接口地址或模型名不对",
        429: "触发限流或额度用尽，稍后再试",
    }
    message = f"LLM 接口返回 HTTP {exc.code}"
    tip = tips.get(exc.code, "")
    if tip:
        message += f"：{tip}"
    if detail:
        message += f"（{detail}）"
    return message


def _llm_content_text(data: dict) -> str:
    """兼容 content 为字符串 / 分段数组两种返回形态。"""
    choices = data.get("choices") or []
    if not choices:
        raise PersonaError("LLM 返回格式不符合预期：缺少 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        ]
        text = "".join(parts)
    else:
        text = str(content or "")
    text = text.strip()
    if text:
        return text
    # 部分思考型模型会把过程放 reasoning_content；content 为空时提示换模型。
    if str(message.get("reasoning_content") or "").strip():
        raise PersonaError(
            "模型只返回了思考过程、正文为空，请换用非思考模型或关闭深度思考"
        )
    raise PersonaError("LLM 返回内容为空")


def call_llm(
    prompt: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    proxy: str = "",
    timeout: float = 90.0,
    max_tokens: int = 0,
) -> str:
    """调用 OpenAI 兼容的 /chat/completions 接口；失败时抛 PersonaError。"""
    base = base_url.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "stream": False,
    }
    if max_tokens > 0:
        body["max_tokens"] = max_tokens
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    opener = _build_opener(proxy)
    try:
        with opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PersonaError(_llm_error_message(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PersonaError(f"LLM 请求失败：{exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PersonaError("LLM 返回不是有效 JSON") from exc
    return _llm_content_text(data)


def build_radar(dimensions: dict) -> dict:
    """六维雷达图坐标（纯 SVG 用，不依赖图表库）。"""
    cx, cy, radius = 150, 118, 80
    count = len(DIMENSION_LABELS)

    def point(index: int, ratio: float) -> tuple[float, float]:
        angle = -math.pi / 2 + 2 * math.pi * index / count
        return cx + radius * ratio * math.cos(angle), cy + radius * ratio * math.sin(angle)

    grid = []
    for ratio in (0.25, 0.5, 0.75, 1.0):
        grid.append(
            " ".join(
                f"{x:.1f},{y:.1f}"
                for x, y in (point(index, ratio) for index in range(count))
            )
        )

    axes, labels, points = [], [], []
    for index, (key, label) in enumerate(DIMENSION_LABELS):
        end_x, end_y = point(index, 1.0)
        axes.append(
            {"x1": cx, "y1": cy, "x2": round(end_x, 1), "y2": round(end_y, 1)}
        )
        label_x, label_y = point(index, 1.18)
        cosine = math.cos(-math.pi / 2 + 2 * math.pi * index / count)
        anchor = "start" if cosine > 0.3 else ("end" if cosine < -0.3 else "middle")
        labels.append(
            {
                "x": round(label_x, 1),
                "y": round(label_y, 1),
                "text": label,
                "anchor": anchor,
            }
        )
        score = max(0.0, min(100.0, float(dimensions.get(key, 0))))
        data_x, data_y = point(index, score / 100)
        points.append(f"{data_x:.1f},{data_y:.1f}")

    return {
        "grid": grid,
        "axes": axes,
        "labels": labels,
        "polygon": " ".join(points),
        "width": 300,
        "height": 240,
    }
