"""御宅成分检测：Bangumi 公开收藏拉取 + 规则画像 + LLM 提示词 / 调用。

设计要点：
- 只用 Bangumi 公开收藏接口（无需 OAuth），私有条目读不到；
- 规则引擎负责所有分数与事实，LLM 只负责把事实写成文案，禁止编造；
- 「避雷类型」来自抛弃（collection_type=5）与低分（rate<=4），
  并从正向高频标签中剔除，避免把喜欢的东西误判成避雷；
- Bangumi 不提供首次收藏时间，「入坑年份」用作品发行年近似，报告里会标注。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

from app import crud
from app.models import BangumiCollection, BilibiliFollow, Content
from app.tagging import (
    GENRE_TAGS,
    TAG_CLUSTERS,
    canonical_genre,
    canonical_studio,
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

# 维度口径（9 维）：题材、热度、口碑、时间、情绪、行为各取一个角度。
# - 题材垂直 tag_focus：前 3 大题材占全部题材标签的比例（0–100，×1 不再放大）。
# - 探索广度 exploration：覆盖的不同题材数 / 40（绝对广度，小样本不再虚高）。
# - 大众度 mainstream：作品热度在**站内库**里的百分位均值（见下面分位标尺）。
# - 冷热跨度 pop_spread：上述百分位的标准差，看你只吃一个热度带还是冷热通吃。
# - 高分率 quality：评分 ≥7 占比（B站评分先折算到 Bangumi 口径）。贝叶斯平滑（先验 50）。
# - 年代跨度 era_span：覆盖的不同年代（十年）数 / 6，考古党的宽度。
# - 追新度 fresh：近五年发行作品占比。
# - 情绪取向 emotion：有情绪信号的作品里，治愈向 vs 致郁向的净差（50 中性）。
# - 完成度 completion：看过 / 收藏。**没有任何未看信号时取中性 50**（B站追番表天然全「看过」）。
#
# 热度必须走「站内分位」而不是固定公式：三个源的原始热度**量纲差两三个数量级**，
# 用同一个 log 公式会让不同源的用户得到完全不同的分数（2026-10-30 实测 18 个
# 真实 B站账号，大众度 18/18 全部顶到 100 并全部拿到「评价党」称号）：
#   - 站内 rating_count（Bangumi 评分人数）：P50≈129
#   - Bangumi collection_total（收藏总数）：约为评分人数 2.2 倍
#   - B站 follow_count（追番人数）：约为站内口径 10^2.68 ≈ 480 倍
# 分位锚点来自线上库 18828 部快照的 log10(rating_count) 经验分位。
POP_PERCENTILE_ANCHORS = [
    (0.00, 0.0),
    (1.32, 10.0),
    (1.62, 25.0),
    (2.11, 50.0),
    (2.75, 75.0),
    (3.32, 90.0),
    (4.62, 100.0),
]

# 各源热度 → 「等效站内评分人数」的折算系数（配对实测 2026-10-30）：
#   bangumi：collection_total / rating_count 约 2.2（2793 对配对）→ ×0.45
#   bilibili：follow_count / 站内 rating_count 中位 10^2.68（2851 对配对）→ ÷480
SOURCE_HEAT_FACTOR = {
    "site": 1.0,
    "bangumi": 0.45,
    "bilibili": 1 / 480.0,
}

# B站评分与 Bangumi 评分的口径差：实测中位差 +2.5（2793 对配对，
# B站 P50=9.6 / Bangumi P50=7.1）。不减这个差，「≥7 分占比」对 B站用户恒为 ~100。
BILIBILI_SCORE_OFFSET = 2.5
DIMENSION_LABELS = [
    ("tag_focus", "题材垂直"),
    ("exploration", "探索广度"),
    ("mainstream", "大众度"),
    ("pop_spread", "冷热跨度"),
    ("quality", "高分率"),
    ("era_span", "年代跨度"),
    ("fresh", "追新度"),
    ("emotion", "情绪取向"),
    ("completion", "完成度"),
]

# 情绪取向：治愈向 / 致郁向标签（取自 GENRE_TAGS，命中即算该作品属于该向）。
HEALING_TAGS = {"治愈", "治愈系", "温馨", "空气系", "萌", "萌系"}
DARK_TAGS = {"致郁", "沉重", "黑深残", "猎奇", "扭曲"}

# 探索广度：题材密度口径（见 _exploration）。
# 每百部作品覆盖 EXPLORATION_DENSITY_REF 种题材即满分；SMOOTH 是平滑项，
# 相当于「先验 100 部作品」，防止收藏极少的人凭 10 部 10 种拿高分。
EXPLORATION_SMOOTH = 100
EXPLORATION_DENSITY_REF = 25
# 年代跨度的归一化分母（达到即 100）。
# 年代跨度参考值（年）：年份标准差达到 12 年记满分。见 :func:`_era_span`。
ERA_SPAN_REF = 12.0
# 题材簇的「主导性」门槛：最大簇占全部题材标签不足这个比例就判为杂食。
# 见 :func:`_cluster`（恋爱簇过大导致称号撞车的修复）。
CLUSTER_DOMINANCE = 0.18

TONE_LABELS = {"sharp": "犀利毒舌", "gentle": "温和中性", "custom": "自定义语气"}

# 文案版本号：**改任何称号 / 毒舌 / 长评 / 收尾锐评的措辞时 +1**。
# 报告里的文案是生成那一刻写死的快照，不加这个版本号，老报告会永远停在旧文案上，
# 只有主动点「重新生成」（要重算整个画像）的人才能看到新写的句子。
# 页面渲染时 _backfill_template_report 发现版本落后，只重算文案字段、不动事实。
COPY_VERSION = 4

# 维度口径版本号：**改任一维度的算法或阈值时 +1**。
# 与 COPY_VERSION 分开是因为两者影响的东西不同：文案版本只管「措辞」，
# 维度版本管「分数与据此命中的称号」。旧的画像快照里存着按老公式算出的
# dimensions，光靠文案版本回填不会让分数变，用户会以为算法没改。
# 页面发现版本落后会提示「重新生成」——重算要重新拉收藏，只能由用户触发。
DIMENSION_VERSION = 4

# 预设语气的具体措辞；custom 走用户自己填的说明。
TONE_PRESETS = {
    "sharp": "犀利毒舌：可以玩梗、吐槽，带点毒舌，但不人身攻击、不贬低用户。",
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
    title_cn = (row.name_cn or "").strip()
    title = title_cn or (row.name or "").strip()
    if not title:
        return None
    return {
        "subject_id": row.subject_id,
        "title": title,
        # 日文原名：title 走了中文名时才带上（搜索增强，与 ingest 同口径）
        "title_original": (row.name or "").strip() if title_cn and row.name and row.name.strip() != title else None,
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
    """站内收藏 / 标注 / 题测作答转成统一作品结构。

    热度（rating_count）与发行年（year）直接从 Content 行携带——题测与站内标注
    之前把这两字段清零，导致维度算出来恒为中性：``mainstream`` 永远 50（人人
    「主流厨」）、``freshness`` 永远 50（追新度测不出）。现在带上，维度才能随
    用户真正标记的作品分化（标记冷门作 → 低主流度 → 冷门挖掘机）。
    ``getattr`` 兜底：极少数没有这两列的旧数据不会崩。
    """
    # 站内标注三态：看过→seen、在看→want（未完、已在追）、抛弃→drop（负向进避雷）。
    # 「未看」是旧口径，保留映射到 interest（只算在册、不算口味）。
    mapped_status = {
        "seen": "seen",
        "want": "want",
        "doing": "want",
        "drop": "drop",
        "unseen": "interest",
    }.get(status, "interest")
    return {
        "title": content.title,
        "type": content.type,
        "year": getattr(content, "year", None),
        "status": mapped_status,
        "tags": [str(tag) for tag in (tags or []) if str(tag).strip()],
        "score": content.rating,
        "popularity": getattr(content, "rating_count", None),
        "rate": 0,
        "source": "site",
    }


# B站追番状态 → 统一状态口径：与 Bangumi 一致——在看 / 看过都算「看过」，
# 想看算「想看」。B站没有「抛弃」信号，避雷只能来自 Bangumi。
# 定义在分析侧（persona）而非拉取侧（bilibili）：拉取只存原始 follow_status。
BILIBILI_FOLLOW_STATUS = {1: "want", 2: "seen", 3: "seen"}


def work_from_bilibili(row: BilibiliFollow) -> dict:
    """B站追番快照转成统一作品结构（与 Bangumi / 站内同口径）。

    season_type 只收 1 番剧 / 4 国创，均按动画；评分用 B站 rating（部分条目
    没有，None 不参与均分）；热度用追番人数。在看 / 看过都按「看过」口径，
    与 Bangumi 的 collection_type 映射一致。
    """
    return {
        "title": (row.title or "").strip(),
        "type": "anime",
        "year": row.year,
        "status": BILIBILI_FOLLOW_STATUS.get(int(row.follow_status or 0), "interest"),
        "tags": [str(tag) for tag in (row.tags or []) if str(tag).strip()],
        "score": row.rating,
        "popularity": row.follow_count or None,
        "rate": 0,
        "source": "bilibili",
        # 原始 follow_status 保留下来：1 想看 / 2 在看 / 3 看过。
        # 「在看」在完成度里要算未看完（见 build_profile 的 completion），
        # 而 status 已把 2/3 统一成 seen，这里必须留原始值，否则信息丢失。
        "follow_status": int(row.follow_status or 0),
    }


# B站标题特有后缀：跨源去重时剥掉（「中文配音」「国语版」不影响作品身份）。
_BILI_TITLE_SUFFIXES = (
    "中文配音", "中配版", "国语版", "普通话版", "日语原声", "原版",
    "4k修复版", "修复版", "完整版",
)


def _cross_source_key(title: str) -> str:
    """跨源去重键：系列归一键（剥季/期/剧场版）再剥 B站特有后缀。

    「CLANNAD 第一季 中文配音」series_title_key 剥不动末尾的「中文配音」，
    先剥 B站 后缀再走一次系列归一，才能对上 Bangumi 的「CLANNAD」。
    """
    key = crud.series_title_key(title or "")
    for _ in range(3):
        changed = False
        for suffix in _BILI_TITLE_SUFFIXES:
            if key.endswith(suffix):
                key = crud.series_title_key(key[: -len(suffix)])
                changed = True
                break
        if not changed:
            break
    return key


def dedupe_works_across_sources(
    bangumi_works: list[dict], bilibili_works: list[dict]
) -> list[dict]:
    """跨源去重：同一作品 Bangumi 与 B站都在时只保留 Bangumi 版（标签更全）。

    站内作品不参与——它们与两个外部源本就按不同主键独立，且 build_profile
    的 _dedupe 只按 (source, title) 去重，管不到跨源。
    """
    merged = list(bangumi_works)
    if not bilibili_works:
        return merged
    seen = {_cross_source_key(work.get("title") or "") for work in bangumi_works}
    seen.discard("")
    for work in bilibili_works:
        key = _cross_source_key(work.get("title") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        merged.append(work)
    return merged


def site_meta_lookup(db, titles: list[str]) -> dict[str, dict]:
    """按标题查站内库的标签与评分：{原标题: {"tags": [...], "rating": float|None}}。

    为什么需要（两个口径各自独立）：
    - **标签**：B站追番接口只给 ``styles``（十几二十种粗标签：奇幻 / 战斗 /
      日常…），Bangumi 与站内库用的是细标签体系（含治愈、致郁、机战等）。不补齐
      的话，「题材垂直 / 探索广度 / 情绪取向」在三个源上测的其实是三件事
      （实测 B站 styles 里 dark 类标签命中为 0，情绪维度对 B站用户恒偏高）。
    - **评分**：B站评分与 Bangumi 评分中位差 2.5 分（9.6 vs 7.1），用固定偏移折算
      只是近似；能对上站内条目时直接采用站内那份 Bangumi 评分，两个源就是同一把尺。
      对不上的才退回「B站 −2.5」（见 :func:`_norm_score`）。

    匹配走系列归一键（剥季数 / 剧场版 / 「中文配音」等后缀）。线上实测 18 个真实
    B站账号 4323 条作品命中 2234 条（51.7%）。查不到就保留原值。
    """
    wanted: dict[str, str] = {}
    for title in titles:
        key = _cross_source_key(title or "")
        if key:
            wanted.setdefault(key, title)
    if not wanted:
        return {}
    cid_of: dict[str, int] = {}
    meta_of: dict[str, tuple[int, float]] = {}
    for cid, title, rating in db.query(Content.id, Content.title, Content.rating).all():
        key = _cross_source_key(title or "")
        if key and key in wanted and key not in cid_of:
            cid_of[key] = cid
            meta_of[key] = (cid, float(rating or 0))
    if not cid_of:
        return {}
    tag_map = crud.list_content_tags_map(db, list(set(cid_of.values())))
    result: dict[str, dict] = {}
    # 只遍历**匹配上**的键：wanted 里没对上站内条目的标题不在 cid_of / meta_of 中
    for key, cid in cid_of.items():
        original = wanted[key]
        _cid, rating = meta_of[key]
        tags = tag_map.get(cid) or []
        if tags or rating:
            result[original] = {"tags": tags, "rating": rating or None}
    return result


def apply_site_meta(works: list[dict], lookup: dict[str, dict]) -> tuple[int, int]:
    """把站内标签与评分套到跨源作品上（站内自身作品不动）。

    返回 ``(补上标签的条数, 采用站内评分的条数)``。评分的来源用 ``score_source``
    标记，:func:`_norm_score` 据此决定还要不要做「B站 −2.5」的近似折算。
    """
    tags_filled = score_filled = 0
    for work in works:
        if work.get("source") == "site":
            continue
        meta = lookup.get(work.get("title") or "")
        if not meta:
            continue
        tags = meta.get("tags") or []
        if tags:
            work["tags"] = list(tags)
            tags_filled += 1
        rating = meta.get("rating")
        if rating:
            work["score"] = float(rating)
            work["score_source"] = "site"
            score_filled += 1
    return tags_filled, score_filled


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
    """题材簇：占比最高的题材；**不够主导就归为杂食**（2026-10-30 加门槛）。

    原实现无条件取最大簇，而「恋爱 / 校园」在站内库是最大标签（1394 + 1002 部），
    于是大批人被判成恋爱簇，主表只在这一簇的 6 个槽里打转——实测「白学家」
    一个称号吃掉 13.7%、「全员 CP 股民」11%，加起来四分之一的人挤在两个称号上。

    加一道主导性门槛：最大簇占全部题材标签不足 ``CLUSTER_DOMINANCE`` 时判为
    「杂食」，把人流摊到杂食簇的槽位上。
    """
    best_cluster, best_score = "杂食", 0
    for cluster, names in TAG_CLUSTERS.items():
        score = sum(tag_counter.get(name, 0) for name in names)
        if score > best_score:
            best_cluster, best_score = cluster, score
    total = sum(tag_counter.values())
    if total and best_score / total < CLUSTER_DOMINANCE:
        return "杂食"
    return best_cluster


def _tag_focus(works: list[dict], genre_counter: Counter) -> float:
    """题材集中度：最爱的 3 个题材**覆盖了多少部作品**（0–100）。

    旧口径是「top3 标签占全部标签的比例」，但标签密度随数据源差很多：
    B站每部平均 2 个 styles、Bangumi 每部 8~10 个标签，同样的口味在
    B站算出 45、在 BGM 算出 30（2026-10-30 实测 BGM 6 个账号全落在 27~34，
    几乎没有区分度）。改成按作品计数，分母是「有题材标签的作品数」，
    三个源就都是「我最爱的三类占我收藏的几成」，语义也更直白。
    """
    top3 = {name for name, _count in genre_counter.most_common(3)}
    if not top3:
        return 0.0
    tagged = [w for w in works if w.get("tags")]
    if not tagged:
        return 0.0
    covered = sum(1 for w in tagged if set(w["tags"]) & top3)
    return _round(covered / len(tagged) * 100)


def _heat_percentile(work: dict) -> float | None:
    """单部作品的热度百分位（0–100）：折算到站内口径后查分位标尺。

    折算见 :data:`SOURCE_HEAT_FACTOR`；无热度数据返回 None（不参与平均）。
    """
    popularity = work.get("popularity")
    if not popularity:
        return None
    factor = SOURCE_HEAT_FACTOR.get(str(work.get("source") or "site"), 1.0)
    try:
        value = float(popularity) * factor
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    log = math.log10(max(1.0, value))
    anchors = POP_PERCENTILE_ANCHORS
    if log <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if log <= x1:
            span = x1 - x0
            return y0 if span <= 0 else y0 + (log - x0) / span * (y1 - y0)
    return anchors[-1][1]


def _mainstream(works: list[dict]) -> float:
    """大众度：收藏热度的**站内百分位均值**（0–100）。

    旧实现是 ``(log10(平均热度) - 2) / 4 * 100``，假设热度落在 10²~10⁶，但真实
    数据里站内 P50 只有 129、B站追番是百万级——于是站内用户普遍算成个位数（甚至
    负数），B站用户则 18/18 全部 >100（公式没有上限）。改成百分位后，分数直接读作
    「你收藏的作品，热度在全站排到第几成」，跨源可比、也不会越界。

    样本不足 5 部取中性 50（收藏两部的热度说明不了口味）。
    """
    percentiles = [p for p in (_heat_percentile(w) for w in works) if p is not None]
    if len(percentiles) < 5:
        return 50.0
    return _round(sum(percentiles) / len(percentiles))


def _exploration(genre_counter: Counter, total: int) -> float:
    """探索广度：题材**密度**——每百部作品覆盖多少种题材（0–100）。

    一路踩过两个坑（都是「拿绝对数量当广度」造成的）：
    1. 香农熵版本测的是均匀度，10 部杂食（54）反而高于 60 部杂食（45），方向反了；
    2. 绝对种类数版本对标签密度和收藏量都敏感——BGM 500 部账号在密集标签下
       覆盖 100+ 种题材，6 个账号**全部顶满 100**（2026-10-30 实测）。
    改成密度并做平滑 ``distinct / (total + 100)``：既不让「只标了 10 部却有 10 种」
    虚高，也让「看了 500 部自然涉猎广」不至于躺赢。参考值 25 种/百部 = 100 分。
    """
    distinct = len(genre_counter)
    if distinct <= 1 or total <= 0:
        return 0.0
    density = distinct / (total + EXPLORATION_SMOOTH) * 100
    return _round(min(100.0, density / EXPLORATION_DENSITY_REF * 100))


def _pop_spread(works: list[dict]) -> float:
    """冷热跨度：热度百分位的标准差，归一到 25 分。

    和「大众度」（百分位均值）互补：大众度看你偏主流还是偏冷门，冷热跨度看你
    只吃一个热度带、还是冷门大热通吃。用百分位而不是原始 log 值，两源才可比
    （B站 log 区间 3.5~7.3、站内 0~4.6，同一个标准差在两源含义不同）。
    样本不足 5 部取中性 50。
    """
    percentiles = [p for p in (_heat_percentile(w) for w in works) if p is not None]
    if len(percentiles) < 5:
        return 50.0
    mean = sum(percentiles) / len(percentiles)
    std = math.sqrt(sum((x - mean) ** 2 for x in percentiles) / len(percentiles))
    return _round(min(100.0, std / 25.0 * 100))


def _era_span(works: list[dict]) -> float:
    """年代跨度：收藏年份的标准差（年），归一到 ``ERA_SPAN_REF`` 年 = 100。

    **2026-10-30 重写**：原实现数「去重年代数 / 6」，值域只有 6 个离散值
    （16.7 / 33.3 / 50 / 66.7 / 83.3 / 100）。实测 28 个账号只有 5 种取值，
    其中 11 个账号同分 66.7——这个维度等于没有分辨率，拿它判称号只会把
    「千人一面」换个地方复发。

    改用年份标准差：连续量，语义直观（"你的收藏在年代上铺得多开"），
    且与 :func:`_pop_spread`（热度百分位标准差）算法同构、便于理解。
    双峰分布（只啃 90 年代 + 只追当季）同样给高分，符合「跨度」直觉。

    参考值 12 年：整段铺满 1970–2026 的收藏标准差约 13 年，取 12 让极端值
    能顶到满分又不至于人人满分。样本不足 5 部取中性 50。
    """
    years = [int(w["year"]) for w in works if w.get("year")]
    if len(years) < 5:
        return 50.0
    mean = sum(years) / len(years)
    std = math.sqrt(sum((y - mean) ** 2 for y in years) / len(years))
    return _round(min(100.0, std / ERA_SPAN_REF * 100))


def _emotion(works: list[dict]) -> float:
    """情绪取向：有情绪信号的作品里，治愈向 vs 致郁向的净差（50 中性）。

    按作品命中而不是按标签次数（一部挂满治愈标签也只算一部）。

    分母只用**命中过情绪标签的作品**，而不是全部有标签作品：实测 B站 styles
    根本没有「致郁」类标签（18 个真实账号、4126 条作品里 dark 命中 0），用全量
    做分母时结果被压在 54~62 的窄带里、几乎没有区分度。改成「在明确表达情绪的
    作品里你偏哪边」之后，治愈党才真的能到高分。

    情绪样本不足 8 部取中性 50——只标了两三部治愈番说明不了取向。

    单向缺失时不能按「情绪样本内占比」算：B站 styles 没有「致郁」类标签，只要
    收藏里有一部治愈番，``(healing - dark) / union`` 就是 100%。所以当某一侧完全
    没有信号时，改用**全部有标签作品**做分母（= 治愈/致郁在口味里的浓度），
    B站用户因此落在 55~70 的合理区间，而不是人人满分。
    """
    tagged = [w for w in works if w.get("tags")]
    healing = sum(1 for w in tagged if set(w["tags"]) & HEALING_TAGS)
    dark = sum(1 for w in tagged if set(w["tags"]) & DARK_TAGS)
    # 两边都命中的作品只算一次（净差会被它抵掉，不该同时计入两端）
    union = sum(
        1 for w in tagged if set(w["tags"]) & (HEALING_TAGS | DARK_TAGS)
    )
    if union < 8:
        return 50.0
    if not dark:
        return _round(min(100.0, 50 + healing / len(tagged) * 100))
    if not healing:
        return _round(max(0.0, 50 - dark / len(tagged) * 100))
    return _round(50 + (healing - dark) / union * 50)


def _norm_score(work: dict) -> float | None:
    """统一到 Bangumi 口径的评分。

    优先用站内库那份 Bangumi 评分（``score_source == "site"``，由
    :func:`apply_site_meta` 补上），两个源就是同一把尺、不用折算。
    对不上站内条目的 B站作品才退回「B站评分 −2.5」的近似：B站评分与 Bangumi
    评分的分布差得很远（实测 P50 9.6 vs 7.1，配对中位差 2.5 分），不折算的话
    「≥7 分占比」对 B站用户恒为 ~100、对 Bangumi 用户约 60%。
    """
    score = work.get("score")
    if not score:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if work.get("score_source") == "site":
        return value
    if str(work.get("source") or "") == "bilibili":
        value -= BILIBILI_SCORE_OFFSET
    return value


def _freshness(works: list[dict]) -> float:
    """追新度：近五年发行的作品占「有年份」作品的比例（0–100）。

    替代原来的「追更强度」（want/seen）——那个维度**判断不了**：数据源里拿不到
    「正在追」这个信号，B 站追番的「在看」（follow_status=2）按既有口径并入
    seen，Bangumi 的想看/在看也常被用户随手标。线上实测：B 站追番 500 条、
    其中 385 条状态是「在看」的人，追更强度算出来是 0，纯噪声（2026-10-28）。
    改用发行年判断「追新还是补老」：三源都有 year，确定可得，也说得出口。

    窗口取**近五年**（含今年）而不是近两年：两年太窄，只收藏了几部当季番的人
    也会被判成「几乎不追新」；五年更贴近「跟不跟这个时代」的直觉。

    样本不足 8 部时取中性 50，避免只收藏了两三部新番就判成「硬核追新党」。
    """
    dated = [w for w in works if w.get("year")]
    if len(dated) < 8:
        return 50.0
    cutoff = datetime.now().year - 4
    recent = [w for w in dated if int(w["year"]) >= cutoff]
    return _round(len(recent) / len(dated) * 100)


def _traits(
    dimensions: dict, want: int, seen: int, quality: float | None
) -> list[str]:
    traits: list[str] = []
    # 特征阈值取「分布中位数附近」，而不是只圈最高那一档：traits 决定 TITLE_MAIN
    # 能命中哪个特征槽，定太高会出现「谁都不带特征 → 全部掉身份兜底」——
    # 实测 6/24 个账号同时兜底成「当季番剧挂机者」（2026-10-30）。
    # 真正稀有的是 TITLE_COMBO（评价党 / 流行厨），那里才用 90 分位的严线。
    #
    # 热度轴（分布：B站 80~93、站内 13~56）
    if dimensions["mainstream"] < 35:
        traits.append("冷门挖掘机")
    # 主流厨抬到 93：站内百分位口径下 85~92 是「常看热门番」的绝大多数人，
    # 全叫主流厨会让同一个「大热」槽称号吃掉近四成账号（实测 9/24 撞同一称号）。
    # 93 才是「收藏热度排在全站前 7%」——真·只吃爆款。
    elif dimensions["mainstream"] >= 93:
        traits.append("主流厨")
    # 神作党留在前面（>=75，稀有）；「口碑党」挪到口味轴之后——
    # 只要收藏里过半作品上 7 分就能叫口碑党，几乎人人命中，会把口味特征挤掉。
    if quality is not None and quality >= 75:
        traits.append("神作党")
    # 口味轴（题材垂直 P50=63、探索广度 P50≈50~66）。
    # 放在普通口碑之前，是因为「看得广 / 只吃一口」才是真区分度：
    # BGM 那批深度用户（探索 58~69、年代跨度 83~100）本应拿「博爱」，
    # 排在高分之后就会被「高分」槽整批接走，深度反而体现不出来（2026-10-30）。
    if dimensions["tag_focus"] >= 63:
        traits.append("偏食")
    # 博爱 60 → 68：模拟样本 exploration 的 p75 就有 62.7，60 时约三成人带「博爱」，
    # 主表「广撒网」槽因此吃掉「真·杂食」「全员 CP 股民」两个称号各 6%。
    if dimensions["exploration"] >= 68:
        traits.append("博爱")
    # 口碑党下限 50 → 58：quality 的 p25 就有 52.4，50 时**近八成人**都带「口碑党」，
    # 主表「高分」槽几乎成了默认出口——「白学家」一个称号吃掉 11.9% 就是这个原因。
    # 58 约是模拟分布的中位数，既能保住「有特征可落」，又不至于人人都有。
    if quality is not None and 58 <= quality < 75:
        traits.append("口碑党")
    # 行为：想看远多于看过
    if seen and want / max(1, seen) >= 1.5:
        traits.append("仓鼠")
    # 上限 3：热度 / 口碑两主轴线优先（已排前两位），口味 / 行为次之
    return traits[:3]


# 标签说明：一个 badge 配一句说明，页面直接展示在标签右侧。
# 四层都要有：题材簇（9）、口碑轴、热度轴、口味轴、行为、主身份（5）。
# 每句只解释「为什么给你贴这个标签」，不复述标签名，也不下评判——
# 二次元人格是自我介绍，不是诊断书（口径见 persona_copy_guide 的边界感一条）。
# 阈值口径见 _traits：口碑轴 quality（≥7 分占比）、热度轴 mainstream、口味轴 tag_focus / exploration。
TRAIT_BLURBS = {
    # 题材簇：收藏里占比最高的那个题材。（2026-10-30 补：这 9 条此前一条都没写，
    # 而它正好是「二次元人格」列表的第一行——页面上出现过「只有标签、右边空白」的怪相。
    # 判据见 _cluster / CLUSTER_DOMINANCE：最大簇不足 18% 判「杂食」。）
    "治愈系": "最多的是日常、空气系：一集下来什么都没发生，你也不亏。",
    "热血系": "打戏燃不燃，决定你要不要追——战斗、少年、王道占了多数。",
    "悬疑系": "剧情不转个弯，你会觉得白看一季。推理、智斗、权谋都算这类。",
    "科幻系": "先看设定再看人：机战、赛博朋克、时间旅行，都在你的单子里。",
    "恋爱系": "别人的感情线你比谁都上心，校园、青春、党争占了收藏大半。",
    "奇幻系": "另一边的世界你住得挺熟：异世界、转生、剑与魔法居多。",
    "搞笑系": "好笑比什么都重要，逻辑往后排——沙雕、吐槽、喜剧都归这一格。",
    "音乐系": "为了一首歌能追完一整季：乐队、偶像、Live 你都吃。",
    "杂食系": "没有哪个题材占到两成，你的收藏本来就是一份拼盘。",
    # 口碑轴：收藏里有评分作品的 Bangumi ≥7 分占比
    "神作党": "八成收藏都在 7 分以上，烂片连进收藏夹的机会都没有。",
    "口碑党": "挑片先看分，7 分以上才点开；6 分以下当它不存在。",
    # 热度轴：收藏作品的平均收藏人数
    "主流厨": "热榜上有什么你基本都清楚，冷门那一栏常年是空的。",
    "冷门挖掘机": "评分人数越少你越有兴趣，热榜对你只是噪音。",
    # 口味轴：题材集中度与题材分布广度
    "偏食": "换过平台换过设备，题材一个没换。",
    "博爱": "这个类型刚补完，下一个类型已经躺在片单里了。",
    # 行为：想看数量 / 看过数量 ≥ 1.5
    "仓鼠": "想看列表长得比看完的快，收藏夹一直在扩建。",
    # 主身份：单一媒介占比过半；均不过半则为杂食党。
    # 这 5 句挂在雷达下方（左栏桌面 300px），可以写完整；手机端由 CSS 隐藏，只留名号。
    # 四句别都用「X 占了一半以上」开头——那是批量填的痕迹，句式打散。
    "番剧党": "动画占了一半以上，新番表和补番列表才是你的主战场。",
    "漫画党": "收藏一半以上是漫画，快乐得自己一页一页翻。",
    "游戏宅": "光看不够，一半以上是得自己上手的游戏。",
    "轻小说党": "比起等改编，你更想直接读原作；收藏一半以上是轻小说。",
    "杂食党": "四种媒介谁都没过半，动画、漫画、游戏、小说你都在碰。",
}


# ==================== 专属称号 ====================
# 五层命中：组合毒舌 → 极端值成就 → 簇×特征主表 → 身份兜底 → 回退拼接。
# 每个称号 = (中文, 代号, 一句毒舌)。代号用于分享卡 / 短链。
# 重点是效果：有梗、贴切、不冒犯，且每个称号命中率都要压得住、不烂大街。


def _dim(profile: dict, key: str, default: float = 50.0) -> float:
    """读维度值，缺失按中性 50 处理。

    历史报告里的 profile 是旧 schema 的快照，线上确有缺 ``quality`` / ``fresh``
    的记录（2026-10-28 实测某账号 dimensions 只有 6 个键）。直接下标会 KeyError，
    把整段文案重建吞掉，用户就一直停在旧文案上。默认 50 与雷达图「缺失维度按
    中性画」同一口径。

    **只适合判「低」（默认 50 天然不满足 <40 之类）**；判「高」请用
    :func:`_dim_at_least`，否则缺维度的老报告会凭一个不存在的 50 拿到
    「评价党」这种需要高分率才成立的称号。
    """
    dims = profile.get("dimensions") or {}
    try:
        return float(dims.get(key, default))
    except (TypeError, ValueError):
        return default


def _dim_at_least(profile: dict, key: str, threshold: float) -> bool:
    """维度**已知且**达到阈值——缺维度一律算不满足，不拿填补值下结论。"""
    dims = profile.get("dimensions") or {}
    if key not in dims:
        return False
    try:
        return float(dims[key]) >= threshold
    except (TypeError, ValueError):
        return False


# 0. 组合毒舌：跨维度冲突，最有记忆点，优先级最高。条件签名 (profile, rarity) -> bool。
# 阈值按 2026-10-30 真实数据校准（大众度改用站内百分位后整体上移）：
# 「评价党 / 流行厨」要求 mainstream >= 90——即收藏热度排在全站前 10% 的人。
# 之前定在 60，实测 18 个真实 B站账号 18/18 全部命中「评价党」，称号等于没有区分度。
# 门槛抬高后，中高主流度的用户会落到 TITLE_MAIN 的分题材「大热」槽，按题材分化。
# 「主流度高」在站内百分位口径下有歧义：看 EVA / CLANNAD 这类经典老番的人，
# 作品热度同样排在全站前列，但他们是「补名作」不是「追当季霸权」。所以再加一道
# 追新度门槛（>=40），把「高热 + 当季」才算真·追热点。实测 BGM 6 个深度账号
# 大众度 53~91、追新度 3~73，不加这道会被误判成「流行厨」（2026-10-30）。
TITLE_COMBO = [
    (lambda p, r: _dim(p, "mainstream") >= 90
     and _dim_at_least(p, "fresh", 40) and _dim(p, "quality") < 55,
     "流行厨", "HAYARI", (
         "当季霸权一部没落，季度末你最喜欢的还是上季那部。",
         "热度榜换一季你换一批，去年追的片名你现在叫不出来。",
         "跟着热度走，热度不认人。",
     )),
    (lambda p, r: _dim(p, "mainstream") >= 90
     and _dim_at_least(p, "fresh", 40) and _dim_at_least(p, "quality", 65),
     "等着被安利", "HYOUKA", (
         "收藏夹里部部 8 分以上，没有一部是你自己挖到的。",
         "等别人挖好了再点开，是你的标准流程。",
         "神作是公认的，口味是借来的。",
     )),
    (lambda p, r: _dim(p, "mainstream") <= 20 and bool(r) and r.get("tier") == "N",
     "伪婆罗门", "NISE-BRAHMIN", (
         "嘴上说热榜没营养，收藏夹里一半是热榜。",
         "你安利的是冷门，自己补的是播放量前二十。",
         "姿势在链顶，品味在链底。",
     )),
    (lambda p, r: _dim(p, "mainstream") <= 20 and _dim(p, "quality") < 50,
     "难民", "NANMIN", (
         "季度末才开番，开到第八部才碰到一部能看完的。",
         "烂归烂你也给它看完了，不然这个季度就白过了。",
         "别人挑番，你捡番。",
     )),
]


# 1. 极端值成就（按优先级，命中第一个即返回）
TITLE_EXTREME = [
    (lambda p: p.get("want", 0) >= p.get("seen", 0) * 3 and p.get("seen", 0) >= 3,
     "赛博貔貅", "PIXIU", (
         "想看列表 327 部，本周看完的还是上周那一部。",
         "加收藏永远比看完快一步，你管这叫先存着。",
         "收藏夹不是列表，是许愿池。",
     )),
    (lambda p: p.get("drop", 0) >= p.get("seen", 0) * 0.5 and p.get("drop", 0) >= 5,
     "三集弃之神", "DROP-GOD", (
         "三集定生死，第四集你通常已经在别的番里了。",
         "劝你熬过前三集的人，最后都被你劝回去了。",
         "慢热是番的问题，不是你的。",
     )),
    (lambda p: _dim(p, "mainstream") <= 10,
     "婆罗门本尊", "BRAHMIN", (
         "你报的片名别人搜不到，评分人数常常是两位数。",
         "安利十次没人接，第十一次你还是说了。",
         "没人看过，也得有人看。",
     )),
    # 判据用「高分率低」而不是 avg_score<6.5：评分统一到 Bangumi 口径后（B站
    # -2.5），B站用户的 avg_score 普遍落在 6.3~7.2，用 6.5 会误判一批只看高分
    # 霸权的人（实测 7/18 命中）。高分率低 = 真的把中低分作品也看完了。
    # 2026-10-30 收紧：drop==0 在模拟样本里是常态（大部分人没有弃番信号），
    # 于是这条实际只由 quality 决定，quality<40 时命中 7.7%。
    # 收到 <34 且 seen>=30：真·看完了大量中低分作品才算，不是「没标弃番」就算。
    (lambda p: p.get("drop", 0) == 0 and p.get("seen", 0) >= 30 and _dim(p, "quality") < 30,
     "有始有终的异类", "FINISH-ALL", (
         "评分 4 分的番，你也看到了第 12 集的片尾曲。",
         "别人弃番的理由，在你这顶多是快进的理由。",
         "烂尾也是尾。",
     )),
    (lambda p: p.get("drop", 0) > 0 and p.get("drop", 0) >= p.get("seen", 0) >= 3,
     "开幕雷击", "EP1-VICTIM", (
         "OP 循环了三十遍，正片停在第三集。",
         "全片的高能都在那 90 秒里，剩下的你不想提。",
         "预告片不是承诺。",
     )),
    (lambda p: _dim_at_least(p, "tag_focus", 90),
     "只吃这一口", "ICHIGEN", (
         "十年下来，四个平台，一个题材。",
         "劝你换口味的人不少，你换了一部同题材的。",
         "只拜一尊佛。",
     )),
]


# 2. 主表：簇 → 特征 → 称号。特征归一到「囤积/冷门/大热/专精/广撒网/高分」六个槽。
def _feature_key(trait: str) -> str | None:
    return {
        "仓鼠": "囤积",
        "冷门挖掘机": "冷门",
        "主流厨": "大热",
        "偏食": "专精",
        "博爱": "广撒网",
        "神作党": "高分",
        "口碑党": "高分",
    }.get(trait)


def _trait_strength(trait: str, profile: dict) -> float:
    """这个特征在本人身上有多突出（0–100），用来在主表里挑最强的那个槽。

    只用于 :func:`assign_title` 的主表命中排序（2026-10-30 加）。各特征取自己
    对应的维度值；越偏离中性 50 越突出，这样「冷门」和「大热」可以用同一把尺。
    """
    if trait in ("神作党", "口碑党"):
        return _dim(profile, "quality", 0.0)
    if trait == "冷门挖掘机":
        return max(0.0, 100.0 - _dim(profile, "mainstream", 50.0))
    if trait == "主流厨":
        return _dim(profile, "mainstream", 0.0)
    if trait == "偏食":
        return _dim(profile, "tag_focus", 0.0)
    if trait == "博爱":
        return _dim(profile, "exploration", 0.0)
    if trait == "仓鼠":
        seen = profile.get("seen") or 0
        want = profile.get("want") or 0
        if not seen:
            return 0.0
        return min(100.0, want / seen * 50.0)
    return 0.0


TITLE_MAIN = {
    # 每支 3 条，各司其职：① 行为特写（具体到能对上号）② 反差 / 自嘲 ③ 金句（≤25 字，可单独截图）。
    # 判据见 .workbuddy/memory/persona_copy_guide.md：没有具体行为的「判定式」文案不算合格。
    "治愈": {
        "囤积": ("钻进被炉", "KOTATSU", (
            "空气系存了 40 部，点开的永远是第 1 集前五分钟。",
            "睡前想看一集放松，一集没看完先睡着了——这也算治愈。",
            "被炉一盖，进度条就不用动了。",
        )),
        "冷门": ("专挖空气系", "KUUKI", (
            "「这有什么好看的？」这一问，你能答 20 分钟。",
            "评分 6 分以下的空气系，你一部一部挖，挖完还写短评。",
            "你在别人划走的地方开矿。",
        )),
        "专精": ("芳文社那口", "KIRARA", (
            "一集 24 分钟，她们泡了 2 杯茶，你截了 87 张图。",
            "剧情按 2 倍速看，萌镜头逐帧看——这不叫看番，叫收图。",
            "推进了半页，你觉得刚好。",
        )),
        "广撒网": ("刀后应激", "IYASHI", (
            "一集虐完，接下来三周只敢点日常番。",
            "不是爱看治愈，是需要它——像需要止痛药。",
            "刀吃多了，自己会找糖。",
        )),
        "高分": ("大魔王的刀", "DAIMAOU", (
            "慢热番你熬到第三季，就为了等它给你一刀。",
         "明知道是催泪弹你还是点开了，看完还要安利给别人。",
         "刀是疗效的一部分。",
        )),
        "大热": ("电子布洛芬", "IYASHI-POP", (
            "榜单上的治愈番你一部不落，冷门的划得比谁都快。",
         "你要的不是好看，是看完能睡着。",
         "治愈是刚需，不是爱好。",
        )),
    },
    "热血": {
        "囤积": ("囤着吃灰", "BURN-CAMP", (
            "台词背得滚熟，正片一部没点开。",
         "存了 43 部热血番，等「有空一口气看完」。",
         "热血放久了也会凉。",
        )),
        "冷门": ("机战还没凉", "MECHA-REF", (
            "萝卜番一年出不了几部，你还在机库里守着。",
            "说机战过时的人，你翻出 90 年代的 OVA 回他。",
            "一年三部，够你活了。",
        )),
        "专精": ("非王道不看", "OU-DO", (
            "套路闭眼能背，OP 一响还是起立。",
         "制作组敢改套路，你敢寄刀片。",
         "不喊招式名，就不算热血。",
        )),
        "高分": ("专收死亡flag", "SHIBOU-FLAG", (
            "角色说「会赢的」，你已经在算他还有几分钟。",
            "「打完这仗就回老家」一出口，你默默暂停截图留念。",
            "话一出口，你就知道要哭了。",
        )),
        "大热": ("追霸权番", "HA-DORI", (
            "每季霸权你都追，OP 一响你就位。",
         "讨论度越高你越上头，冷门机战你懒得点。",
         "燃不燃，弹幕说了算。",
        )),
        "广撒网": ("燃就行", "BURN-OMNI", (
            "热血是你的主菜，日常和悬疑你也照样点。",
         "一部一部追太慢了，你同时开着四部。",
         "燃点不分菜系。",
        )),
    },
    "悬疑": {
        "囤积": ("凶手还在收藏夹", "CASE-HOARDER", (
            "推理番存了 47 部，一部没解。",
         "「真相只有一个」，但你永远没看到那一集。",
         "收藏夹里的凶手最安全。",
        )),
        "冷门": ("冷门本格", "BRAHMIN-D", (
            "热榜上的推理懒得点，没人讨论的那几部你一部不落。",
         "诡计站不站得住，你比评分区先知道。",
         "越没人看的案子，你越想破。",
        )),
        "专精": ("先画平面图", "LOCKED-ROOM", (
            "诡计一出来，进度条就停在那不动了。",
            "截图存档之前，先把平面图画出来。",
            "门一锁，你就精神了。",
        )),
        "高分": ("爱的战士", "AI-NO-SENSHI", (
            "三话一掉头，你连眉头都不皱。",
         "越虐你越放心，敢这么写的一般都不会太差。",
         "虐你，是入场券。",
        )),
        "大热": ("全民推理", "NET-MYST", (
            "本季讨论度最高的推理番你必追，剧透贴你抢着看。",
         "反转够大你就喊神，诡计严不严密你没空验。",
         "剧透完了还是要看。",
        )),
        "广撒网": ("推理也杂食", "MYST-OMNI", (
            "悬疑是主菜，日常和治愈你也照样点。",
         "一部推理不够，你同季还开着两部别的类型。",
         "什么案子都收。",
        )),
    },
    "科幻": {
        "囤积": ("设定比正片熟", "LORE-HOARDER", (
            "一集正片看十分钟，设定集翻了一下午。",
            "官方年表、外传小说、广播剧，能收的全收了。",
            "正片是附件，设定才是本体。",
        )),
        "冷门": ("软科幻不算", "HARD-SF", (
            "现在没人拍硬 SF 了，你把 80 年代的又翻出来看。",
            "软科幻一上场，你先翻白眼再去查物理设定。",
            "硬科幻的荣光，靠你一个人撑着。",
        )),
        "专精": ("机设考据党", "MECHA-NERD", (
            "别人看战斗，你数关节活动范围。",
         "作画崩一帧，你能截图挂出来讲半天。",
         "见不得一点机构崩坏。",
        )),
        "高分": ("凤凰院凶真", "HOUOUIN", (
            "「这一切都是命运石之门的选择」，这话你说过不止一次。",
         "世界线收束、时间悖论，你能跟人聊一小时。",
         "时间线乱了，你才兴奋。",
        )),
        "大热": ("爆款 SF", "SF-POP", (
            "每季讨论度最高的科幻番你都在追。",
         "设定边看边查 Wiki，看完你也说不出它硬在哪。",
         "跟着热度上太空。",
        )),
        "广撒网": ("科幻也杂食", "SF-OMNI", (
            "科幻是主菜，奇幻和日常你也照样点。",
         "一部科幻不够，你同季还追着两部别的。",
         "星图要画全。",
        )),
    },
    "恋爱": {
        "囤积": ("囤狗粮", "KOI", (
            "恋爱番存了 63 部，理由都是「等心情好的时候再看」。",
            "别人的恋爱你看了一百集，自己的进度 0。",
            "粮是别人的，饿是自己的。",
        )),
        "冷门": ("见不得 NTR", "PURE-LOVE", (
            "三角关系一出现，你就开始紧张。",
         "这番没什么人聊，你一个人在小圈子里守着。",
         "只要一对一，别的都不算。",
        )),
        "专精": ("单推到底", "SHIP-MONO", (
            "同人只吃一种配对，别的设定一律不点。",
         "看到别人乱组 CP，你先皱眉再划走。",
         "CP 只有一个，没有备选。",
        )),
        "广撒网": ("全员 CP 股民", "SHIP-STOCK", (
            "一天之内换三个墙头，谁也没耽误。",
            "每个 CP 都买一股，哪个成都不亏。",
            "嗑 CP 如持股，分散投资最稳。",
        )),
        "高分": ("白学家", "HAKUGAKU", (
            "一部恋爱番你能拆出三条感情线，专楼盖到第 3127 楼。",
         "剧里还没告白，你已经把三个人的输赢算到最后一集。",
         "党争没有赢家，只有先后。",
        )),
        "大热": ("冈妈药房", "OKA-YAKU", (
            "明知道要胃疼，还是每集照点不误。",
            "胃药一颗没少吃，追更一集没落下。",
            "冈妈的剧本，是你的处方药。",
        )),
    },
    "奇幻": {
        "囤积": ("转生次数超限", "TENSEI", (
            "每季新番一半是异世界，你照单全收。",
         "转生番看得比主角转生的次数还多。",
         "已经转生 128 次，还想再来一次。",
        )),
        "冷门": ("冷门异界", "DARK-FANT", (
            "这季的异世界你一部没追，追的是没人讨论的那部。",
         "评分 5 分的你也点开，就想看看它能有多离谱。",
         "越没人转生的世界，你越想去。",
        )),
        "专精": ("咒语考据", "MAGIC-NERD", (
            "魔力怎么算、咏唱要多长，你比作者还清楚。",
         "设定前后矛盾，你能写一篇长帖。",
         "体系崩了，比主角死还难受。",
        )),
        "高分": ("背锅侠", "TOKIOMI", (
            "剧情一崩先找责任人，制作组跑不掉。",
         "「都是时臣的错」说顺口了，什么锅都能甩过去。",
         "总得有个人背锅。",
        )),
        "大热": ("每季异世界", "FAN-POP", (
            "最火的那几部异世界，你一部没落下。",
         "设定边看边查 Wiki，看完你也记不住这个世界叫什么。",
         "热度在哪你转生到哪。",
        )),
        "广撒网": ("异世界也杂食", "FAN-OMNI", (
            "异世界是主菜，科幻和日常你也照样点。",
         "一部异世界不够，你同季还开着两部别的。",
         "哪都能转生。",
        )),
    },
    "搞笑": {
        "囤积": ("先截图再说", "SHITPOST", (
            "一集看了十分钟，截图截了三十张。",
         "沙雕番存一堆，表情包随时能甩出来。",
         "相册比追番表长。",
        )),
        "冷门": ("笑点没人懂", "COLD-JOKE", (
            "接不住的冷梗，笑出声的只有自己。",
            "冷门搞笑番的台词你背下来了，没人听得懂。",
            "你的笑点，小众到需要翻译。",
        )),
        "广撒网": ("弹幕比正片好看", "DANMAKU", (
            "笑点最后都落在弹幕里，正片反而快进。",
            "一个人看没意思，得看别人一起笑。",
            "正片是配菜，弹幕才是主食。",
        )),
        "高分": ("梗警察", "MEME-POLICE", (
            "弹幕在刷烂梗，你在心里数这是第 8 次。",
            "一个梗刚刷出来，你先挑它的语病。",
            "梗可以烂，不能错。",
        )),
        "大热": ("沙雕热点", "GAG-POP", (
            "每季讨论度最高的搞笑番你都看，梗甩得比谁都快。",
         "冷门冷梗你划得飞快，不好笑的梗不算梗。",
         "梗是当季的，过期就不好笑。",
        )),
        "专精": ("沙雕以外免谈", "GAG-NERD", (
            "沙雕番存了一仓库，别的类型一部没留。",
         "看一遍就笑太浪费了，你要倒回去找第几秒。",
         "搞笑也分门派。",
        )),
    },
    "音乐": {
        "囤积": ("存了不播", "PLAYLIST", (
            "OST 收了 41 张，日常循环的还是那三首。",
            "你不是在听歌，你在建歌单。",
            "歌单越攒越长，听的永远是开头那几秒。",
        )),
        "冷门": ("冷门 OST", "OST-HUNT", (
            "正片没人看，配乐你循环了 137 遍。",
            "冷门番的 OST，一张张考古出来。",
            "别人收藏番，你收藏 BGM。",
        )),
        "广撒网": ("全曲风 DD", "SONG-DD", (
            "昨天还在听摇滚，今天在循环偶像曲。",
         "说好只推一个，结果又收了三个墙头。",
         "墙头比歌单长。",
        )),
        "高分": ("前奏定生死", "SONG-HUNT", (
            "前奏 3 秒不入耳，直接下一首。",
            "神曲才配进循环，别的听一耳朵就切。",
            "你的歌单没有过场曲。",
        )),
        "大热": ("OP 先会唱", "MUS-POP", (
            "每季讨论度最高的音乐番你都追，一集不落。",
         "主题曲提前三天就会唱，正片还没开播。",
         "打榜不落一场。",
        )),
        "专精": ("只听一种", "MUS-NERD", (
            "二十年了，歌单还没换过口味。",
         "别的类型你划得快，音乐番你逐帧听编曲。",
         "母带比现场真。",
        )),
    },
    "杂食": {
        "广撒网": ("真·杂食", "TRUE-OMNI", (
            "什么类型都点开，什么类型都留不住。",
         "本命是「下一季」，精神状态随新番波动。",
         "一集入教，一集退教。",
        )),
        "囤积": ("什么都想要的人", "ALL-WANT", (
            "标注「想看」的速度，比看完快 5 倍。",
            "收藏夹吃灰的，比看完的多。",
            "想要的都留着，留到不想要。",
        )),
        "冷门": ("冷门杂食", "OMNI-COLD", (
            "啥类型都看，但偏门的你全挖得最透。",
            "主流的你也看，冷门的那桌你才是常客。",
            "偏门的坑，你蹲得最久。",
        )),
        "大热": ("榜单即菜单", "OMNI-POP", (
            "每季讨论度最高的几部你都看，热度就是选片器。",
         "冷门神作划得比谁都快，没人讨论你就不知道它好不好。",
         "榜单前二十你都在场。",
        )),
        "专精": ("杂食偏科生", "OMNI-FOCUS", (
            "啥都看，但有一类存得比别的都多。",
         "说是杂食，其实那桌菜你点得最频。",
         "偏的那一类，才是本命。",
        )),
        "高分": ("先看分再说", "OMNI-SCORE", (
            "从萌番到硬 SF 你都吃得下。",
         "前提是它得有星——6 分以下的连点开都不点。",
         "嘴上杂食，筷子挑食。",
        )),
    },
}


# 2.5 圈内身份：细分标签 / 维度触发，原生身份词（有出处、有梗）。
# 在主表之前命中——细分身份比「簇×特征」更精准、更圈内。
def _has_genre(profile: dict, *names: str, min_share: float = 0.0) -> bool:
    """细分标签是否命中主要题材。

    只看「是否出现在 top_genres 前 8」太松：恋爱 / 校园 / 治愈这类大题材几乎
    每个人的前 8 里都有，实测 18 个真实 B站账号里 12 个被「现充」这一条全部
    抢走，称号等于没分。``min_share`` 要求这些标签在该用户的**全部题材标签**里
    占到指定比例，才认这个人真属于这个细分圈。
    """
    counts = profile.get("genre_counts") or {}
    if not counts:
        return False
    wanted = {canonical_genre(name) or name for name in names}
    hit = sum(
        count
        for name, count in counts.items()
        if (canonical_genre(name) or name) in wanted
    )
    if not hit:
        return False
    if min_share <= 0:
        return True
    total = sum(counts.values())
    return bool(total) and hit / total >= min_share


TITLE_FANDOM = [
    (lambda p: _has_genre(p, "扭曲", "党争", "修罗场", min_share=0.008),
     "战吧吧友", "SENBA", (
         "败犬哭着退场那段，你会倒回去看两遍。",
         "成了的那对你没兴趣，你盯着没成的那个。",
         "糖是别人的，刀是你的。",
     )),
    (lambda p: _has_genre(p, "百合", min_share=0.15),
     "白河豚", "KAWABUTA", (
         "两个女生对视三秒，你已经把一季的剧情补完了。",
         "官方发一粒糖你能嗑一整季，官方不发你自己种。",
         "她们只是牵了个手。",
     )),
    (lambda p: _has_genre(p, "耽美", "乙女", min_share=0.04),
     "同人女", "FUJOSHI", (
         "官方没写的那段，你的文档里有 20 万字。",
         "原作只是原料，你脑子里那版才是正片。",
         "你写的那版，比官方好看。",
     )),
    (lambda p: _has_genre(p, "机战", "萝卜", "机甲", "机器人", min_share=0.10),
     "胶佬", "GUNPLA", (
         "一集看完先查三件事：出没出胶、HG 还是 MG、多少钱。",
         "柜子里站了 47 台，开过盒的只有 6 台。",
         "萝卜才是本体。",
     )),
    (lambda p: _has_genre(p, "乐队", min_share=0.013),
     "乐队男", "BAND-MAN", (
         "番里的和弦你听得出，自己一个音都不会弹。",
         "琴买了三年没拆封，乐队番的 Live 你一段没落下。",
         "乐器是摆的，乐队是真的。",
     )),
    (lambda p: _has_genre(p, "偶像", "Live", min_share=0.030),
     "箱推", "HAKO-OSHI", (
         "本命只有一个，钱包里住着二十个。",
         "成员的生日你背得出，自己的要翻手机。",
         "单推是誓言，箱推是现实。",
     )),
    (lambda p: _has_genre(p, "悬疑", "推理", min_share=0.12),
     "作豚", "FRAME-NERD", (
         "一集 24 分钟，你要看 50 分钟。",
         "这一卡原画是谁，你比 STAFF 表记得清。",
         "你不是在看番，你在验货。",
     )),
    (lambda p: _has_genre(p, "恋爱", "校园", min_share=0.24),
     "现充", "RIAJUU", (
         "校园番看了 40 部，修学旅行那集谁先告白你背得出来。",
         "别人的青春你一部不落，自己的青春一集没开始。",
         "没谈过，但很懂。",
     )),
    (lambda p: _has_genre(p, "治愈", "日常", min_share=0.20),
     "萌豚", "MOE-BUTA", (
         "一集 24 分钟什么都没发生，你在第 3 分钟截了图。",
         "明知道这集什么都没讲，下周这个点你还是打开了。",
         "萌才是正片，剧情是赠品。",
     )),
    # 下面这四条（考据党 / 评分厨 / 婆罗门 / 自宅警备员）**已移到 TITLE_AXIS**：
    # 它们是纯维度判据，不是圈内身份，排在 TITLE_MAIN 之前会把题材分化的人整批
    # 截走（主表 54 个称号因此长期闲置）。见下方 TITLE_AXIS 的说明。
    # 判据：2010 年前的老番要占到收藏的四成、且总量 150 部以上。
    # 只看「最早一部是 2010 年前」不成立——B站追番表里本来就有平台库存的老番，
    # 实测 18 个真实账号 9 个中招（2026-10-30）。改看老番占比才真能筛出老资历。
    # 旧报告没有 early_share 字段，缺失时按 0 处理（不会误命中）。
    (lambda p: (p.get("early_share") or 0) >= 40 and p.get("total", 0) >= 150,
     "老贼", "O-GAI", (
         "入坑早到能当别人前辈，现在只剩一句「现在的番不行了」。",
         "你追的连载，有的完结了，有的作者还没画完。",
         "你入坑那年，这批新番还没开机。",
     )),
]


# 2.5 维度轴（AXIS）：排在 TITLE_MAIN **之后**，专接两类人——
#   (a) 题材特征不突出、但某个维度走极端的人（收紧 traits 阈值后这类人变多了，
#       实测 2026-10-30：不接的话 27.4% 的人全掉进「当季番剧挂机者」这一个兜底）；
#   (b) 原先混在 TITLE_FANDOM 里的四条**纯维度判据**（考据党 / 评分厨 / 婆罗门 /
#       自宅警备员）——它们不是圈内身份，放在主表前会截流，挪到这里既解截流又接人。
# 命名遵循称号命名四律（见 .workbuddy/memory/persona_copy_guide.md 第十节）：
# 是身份不是特征、圈内现成词、带反差、挪用商业/物理/生物词做双关。
# 「没有特点」的判定用哪些维度（不含 mainstream：模拟与真实样本里主流度普遍
# 80+，那是人群共性，不是个人特征，算进去会导致人人都判成「有特点」）。
_MEDIAN_KEYS = ("quality", "exploration", "tag_focus", "emotion", "era_span", "fresh")
_MEDIAN_TOLERANCE = 20.0


def _is_median_profile(profile: dict) -> bool:
    """每个维度都贴着中线（50±20）——「没有特点」本身也是一种特点。"""
    dims = profile.get("dimensions") or {}
    vals = []
    for key in _MEDIAN_KEYS:
        if key not in dims:
            continue
        try:
            vals.append(float(dims[key]))
        except (TypeError, ValueError):
            continue
    if len(vals) < 5:
        return False
    return all(abs(v - 50.0) <= _MEDIAN_TOLERANCE for v in vals)


TITLE_AXIS = [
    (lambda p: _dim_at_least(p, "tag_focus", 85),
     "考据党", "KOUSHOU", (
         "为了确认一个设定，你翻了三本设定集和四个日站。",
         "监督换了你第一个发现，作画崩了你比作画监督先看到。",
         "看番是借口，考据是正事。",
     )),
    (lambda p: _dim_at_least(p, "quality", 78),
     "评分厨", "SCORE-CHU", (
         "收藏前先查分，6.9 和 7.0 在你这是两部番。",
         "分数越低划得越快，划得越快，漏掉的神作越多。",
         "不看评分，看不下去。",
     )),
    (lambda p: _dim(p, "mainstream") <= 30,
     "婆罗门", "BARAMON", (
         "热榜上找不到的东西你这里都有，安利发出去没人理。",
         "一句「这你也看过」，够你高兴一整天。",
         "小众不是标签，是成绩。",
     )),
    (lambda p: p.get("seen", 0) > 0 and p.get("want", 0) / p["seen"] >= 2,
     "自宅警备员", "JITAKU-KEI", (
         "追番表排得比课表满，补番永远排在下个季度。",
         "「先 mark 再说」是你今年说得最多的一句话。",
         "警备员不下班，也不开番。",
     )),
    # ---- 新增：接住 4 个原本「死掉」的维度 ----
    (lambda p: _dim_at_least(p, "pop_spread", 85),
     "不分冷热", "ONSITSU", (
         "上季度霸权你在追，同期三百人评分的冷门你也在追。",
         "鄙视链上你上下都待得住，哪一格都不落户。",
         "热度在你这不是筛选条件。",
     )),
    (lambda p: _dim(p, "pop_spread") <= 15,
     "不上不下", "KOUON", (
         "你的收藏全挤在同一条热度带上，往上够不着，往下懒得看。",
         "冷门推给你你划走，霸权推给你你也划走。",
         "只认这一个温度。",
     )),
    (lambda p: _dim_at_least(p, "emotion", 82),
     "求刀得刀", "HAITOU", (
         "挨一刀就弃番？你把那一集又看了一遍。",
         "完结后你第一件事，是去搜有没有第二季能再刀一次。",
         "眼泪是预约好的。",
     )),
    (lambda p: _dim_at_least(p, "fresh", 62),
     "过季就不认", "TOUKI", (
         "番在最热的时候你在，热度一过你就消失。",
         "补番排在下个季度，而下个季度又有新番了。",
         "热度散了，你就当没看过。",
     )),
    (lambda p: _dim(p, "fresh") <= 12,
     "新番不碰", "ZEPBAN", (
         "本季新番你一部叫不出名字，三年前的倒背得出来。",
         "这季霸权在群里聊得火热，你在补十年前那部。",
         "绝版两个字，是褒义。",
     )),
    (lambda p: _dim_at_least(p, "era_span", 78),
     "从昭和追到令和", "NENPYOU", (
         "上一部是 1986 年的 OVA，下一部是上周才播的。",
         "补番别人按季度，你按年表。",
         "年代对你只是坐标。",
     )),
    (lambda p: 0 < _dim(p, "completion", 50.0) <= 40,
     "跳过 ED", "HANTO", (
         "点开的比看完的多，看完的比记住的多。",
         "你停的位置都差不多，像有一条统一的截止线。",
         "片尾曲是给别人听的。",
     )),
    # 兜底中的兜底：每个维度都贴着中线，谁也不突出。
    # 实测 2026-10-30：收紧主表阈值后有 23.5% 的人掉进身份兜底，他们的共同点就是
    # 「没有特点」——quality 卡在 50~58、exploration 54、emotion 恒 50（无信号）。
    # 与其让他们全挤在「当季番剧挂机者」，不如承认这就是一类人。
    # 不判 mainstream：模拟与真实样本里主流度普遍 80+，那是共性不是特征。
    # 「没特点」这一群人比想象中大（实测占 19%），一个称号装不下（5% 只有 16 人），
    # 所以按追新度拆成两支：跟着季度走的、和停在某个年代不动的。
    (lambda p: _is_median_profile(p) and _dim_at_least(p, "fresh", 35),
     "按季下菜", "FLOAT", (
         "一季开十部，季末记得住的不到三部。",
         "开播跟了，更新跟了，完结的时候人已经在下一季了。",
         "档期换了，口味跟着换。",
     )),
    (_is_median_profile,
     "中位数本人", "MEDIAN", (
         "九个维度算下来，你每一个都落在中间，没有一个值得写进称号。",
         "别人是偏科生，你是均衡型——均衡到想不出该怎么介绍你。",
         "你就是那条平均线。",
     )),
]


# 3.8 兜底细分：身份兜底只有 5 个称号，而「番剧党」一个就吃掉全部兜底人群
# （实测 42/42 全是番剧党）。按追新度拆两支，把压力摊开。
TITLE_FALLBACK = [
    (lambda p: p.get("identity") == "番剧党" and _dim_at_least(p, "fresh", 40),
     "本季在席", "ZAISEKI", (
         "每季开播你都到场，散场了你也不记得看了什么。",
         "新番一部不落，旧番一部不补，出席率是你唯一的成绩。",
         "到场即完成。",
     )),
    (lambda p: p.get("identity") == "番剧党",
     "补番中", "KOUKO", (
         "当季新番一部没开，去年十月档的倒补了 17 部。",
         "越往回补越觉得好看，就越不想开新的了。",
         "新番会过季，库存不会。",
     )),
]


# 3. 身份兜底：簇×特征都命中不了时，按主身份给一个。
TITLE_IDENTITY = {
    "番剧党": ("挂机追番", "SEASON-AFK", (
        "新番准时开，旧番永久搁置，追番表就是个许愿池。",
         "挂在你列表里的番，比你说得出剧情的多三倍。",
         "挂着，就算追了。",
    )),
    "漫画党": ("原作党", "SRC-PARTY", (
        "动画看到第 3 集，原作已经追到最新话。",
         "改编定了档期，你先高兴三秒，然后开始担心。",
         "「原作更好」在你这是陈述句。",
    )),
    "游戏宅": ("不白金不封盘", "PLATINUM", (
        "通关不算完，支线和成就一个都不能少。",
         "存档时长比剧情需要多三倍，多出来的全在刷收集。",
         "奖杯没满，游戏就没结束。",
    )),
    "轻小说党": ("不等动画化", "BUNKO", (
        "动画还没定档，你已经把原作看到结局了。",
         "书架塞到第三层，新到的那摞只能躺地上。",
         "文库本是正片，动画是预告。",
    )),
    "杂食党": ("全都要", "NO-TYPE", (
        "四种媒介的收藏夹你都建过，没有一个满。",
         "问到你本命是什么，得想一会儿才答得出来。",
         "不挑媒介，只挑好看。",
    )),
}


# 多 Variant 毒舌轮转：同一称号给 3 句，按 seed（用户名）稳定取一句。
# 同一用户永远同一句（截图/分享卡一致），不同用户摊开——避免「人人同句」失效。
# 用 md5 而非内置 hash()：后者跨进程加盐，重启后用户会换句子。
def _pick(options, seed: str, *, salt: str = "") -> str:
    items = [o for o in (options or []) if o]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    key = f"{salt}|{seed}"
    idx = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % len(items)
    return items[idx]


def _note_of(note, title: str, seed: str) -> str:
    """note 兼容单句与多句池：字符串原样返回，序列按 seed 轮转。"""
    if isinstance(note, str):
        return note
    return _pick(note, seed, salt=title)


def _seed_of(profile: dict, seed: str = "") -> str:
    """轮转种子：优先用户名；无名（quiz/游客匿名）时退回画像指纹，保证稳定。"""
    if seed:
        return str(seed)
    return "p|{0}|{1}|{2}|{3}".format(
        profile.get("cluster", ""),
        profile.get("identity", ""),
        profile.get("total", 0),
        profile.get("seen", 0),
    )


# 宅宅兜底（最高频之一）：全员最后都会落到这，必须多句。
_OTAKU_NOTES = (
    "别人问爱好，憋半天说「看动漫」，然后开始疯狂安利。",
    "收藏列表整整齐齐，硬盘塞得比系统盘还实在，删谁都心疼。",
    "入坑年份早记不清了，但退坑是绝不可能的。",
)


def _share_line(hero_title: str, hero_note: str, profile: dict, seed: str) -> str:
    """引流复制文案（被真正发到小红书 / QQ 群的那一句）。

    钩子从「称号 + 来测」改成第一人称圈内梗 + 数据锚，称号不炸也能勾人。
    """
    total = int(profile.get("total") or 0)
    note = (hero_note or "").rstrip("。")
    pool = (
        f"我的御宅成分是【{hero_title}】——{note}。你呢？来测",
        f"刚测完，我的御宅成分是【{hero_title}】，看了 {total} 部才修成这个形状。来测测你的",
        f"【{hero_title}】，{note}。不服来测",
    )
    return _pick(pool, seed, salt="share")


def assign_title(
    profile: dict, rarity: dict | None = None, seed: str = ""
) -> tuple[str, str, str]:
    """六层命中返回专属称号 (称号, 代号, 毒舌句)，保证任何画像都有称号、不会空。

    毒舌句支持多 Variant：按 seed（用户名）稳定轮转，同一用户始终同一句。
    """
    traits = profile.get("persona_tags", [])[2:]
    seed = _seed_of(profile, seed)

    def _finish(title: str, code: str, note) -> tuple[str, str, str]:
        return title, code, _note_of(note, title, seed)

    # 0. 组合毒舌（跨维度冲突）
    for cond, title, code, note in TITLE_COMBO:
        if cond(profile, rarity):
            return _finish(title, code, note)
    # 1. 极端值成就
    for cond, title, code, note in TITLE_EXTREME:
        if cond(profile):
            return _finish(title, code, note)
    # 2. 圈内身份（细分标签 / 维度，原生身份词）
    for cond, title, code, note in TITLE_FANDOM:
        if cond(profile):
            return _finish(title, code, note)
    # 3. 簇 × 特征主表：**按特征强度取最强的那个槽**（2026-10-30 改）
    #
    # 原实现按 traits 列表顺序取第一个命中的槽，而 traits 的顺序是写死的
    # （热度 → 口碑 → 口味 → 行为）。问题是「口碑党」「博爱」的门槛在中位数附近，
    # 命中率极高（实测模拟样本里近八成人带「口碑党」），于是主表六个槽里只有
    # 「高分」「广撒网」两个在出货——「白学家」一个称号吃掉 12.2%。
    #
    # 改成比强度：谁在你身上最突出就用谁的槽。quality 65 和 tag_focus 70 的人
    # 会分别落到「高分」「专精」，而不是都被「高分」收走。
    row = TITLE_MAIN.get(profile.get("cluster", ""), {})
    ranked = sorted(
        (t for t in traits if _feature_key(t) in row),
        key=lambda t: _trait_strength(t, profile),
        reverse=True,
    )
    for t in ranked:
        fk = _feature_key(t)
        if fk and fk in row:
            return _finish(*row[fk])
    # 3.5 维度轴（AXIS）：簇×特征都没接住时，看哪个维度走极端。
    # 这一层是「防兜底拥挤」的关键——没有它，收紧主表阈值后所有人会一起掉进
    # 身份兜底那 5 个称号里（实测 27.4% 全挤在「当季番剧挂机者」）。
    for cond, title, code, note in TITLE_AXIS:
        if cond(profile):
            return _finish(title, code, note)
    # 3.8 兜底细分（番剧党太挤，先按追新度拆）
    for cond, title, code, note in TITLE_FALLBACK:
        if cond(profile):
            return _finish(title, code, note)
    # 4. 身份兜底
    ident = profile.get("identity", "")
    if ident in TITLE_IDENTITY:
        return _finish(*TITLE_IDENTITY[ident])
    # 5. 回退：宅宅兜底（最本源的身份，永远有人可当）
    return _finish("宅宅", "OTAKU", _OTAKU_NOTES)


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
    bilibili_total: int = 0,
    notes: list[str] | None = None,
) -> dict:
    """由统一作品结构计算画像：类型、标签、避雷、时间线、维度分数、人格标签。"""
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
            # 一次查表，别再逐厂商遍历别名：STUDIO_ALIASES 三十余家 × 每部作品几十个
            # 标签，原本是 O(N × T × 31)（2026-10-28 改，口径同 tagging.canonical_studio）。
            canonical = canonical_studio(tag)
            if canonical:
                studio_counter[canonical] += 1
    top_studios = [
        {"name": name, "count": count}
        for name, count in studio_counter.most_common(5)
    ]

    year_items = [w for w in interest if w.get("year")]
    entry = min(year_items, key=lambda w: w["year"]) if year_items else None
    latest = max(year_items, key=lambda w: w["year"]) if year_items else None
    # 老番占比：2010 年及以前的作品占「有年份」作品的比例。
    # 「入坑早」不能看最早那一部——B站追番表里必然躺着平台库存的老番，
    # 随手补一部 2008 年的番就会被判成老资历（实测 18 个账号里 9 个中招）。
    early_share = (
        _round(
            sum(1 for w in year_items if int(w["year"]) <= 2010)
            / len(year_items)
            * 100
        )
        if year_items
        else 0.0
    )

    avg_popularity = _avg(
        [w.get("popularity") for w in interest if w.get("popularity")]
    )
    # 评分统一到 Bangumi 口径后再算高分率与均分（B站评分 -2.5，见 _norm_score）
    scored_works = [s for s in (_norm_score(w) for w in interest) if s]
    # 高分率：评分 ≥7 的占比。贝叶斯平滑（先验：12 部、50%）：
    # 10 部全高分只到 ~73，不会被小样本直接顶成 100；样本越多越接近真实。
    high7 = [s for s in scored_works if s >= 7]
    quality = (
        _round((len(high7) + 6) / (len(scored_works) + 12) * 100)
        if scored_works
        else 50.0
    )
    avg_score = _avg(scored_works)

    # 完成度：看完 / 收藏。数据源拿不到「未看完」信号时取中性 50，而不是假性 100。
    #
    # **2026-10-30 重写**：原实现只看 want/hold/drop 三个「未看」信号，而 B站追番表
    # 天然没有这三类（follow_status 只有 1 想看 / 2 在看 / 3 看过），于是 28 个账号里
    # **21 个（75%）恒等于 50**——这个维度等于没测。
    #
    # 修复：B站的「在看」（follow_status=2）本来就是没看完，从 seen 里扣掉后当未看完计。
    # 实测某账号追番 500 条、其中 385 条状态是在看 → 完成度 23%，而不是原来的 50。
    # work_from_bilibili 已保留原始 follow_status，正是为了这里能区分 2 和 3。
    watching = sum(
        1
        for w in interest
        if str(w.get("source")) == "bilibili"
        and int(w.get("follow_status") or 0) == 2
    )
    unseen_signals = len(want) + len(hold) + len(drops) + watching
    completion = (
        _round((len(seen) - watching) / max(1, len(interest)) * 100)
        if unseen_signals
        else 50.0
    )

    dimensions = {
        "tag_focus": _tag_focus(interest, genre_counter),
        "exploration": _exploration(genre_counter, len(interest)),
        "mainstream": _mainstream(interest),
        "pop_spread": _pop_spread(interest),
        "quality": quality,
        "era_span": _era_span(interest),
        "fresh": _freshness(interest),
        "emotion": _emotion(interest),
        "completion": completion,
    }

    identity = _identity(type_shares)
    cluster = _cluster(genre_counter)
    traits = _traits(dimensions, len(want), len(seen), quality)

    return {
        "total": total,
        "seen": len(seen),
        "want": len(want),
        "hold": len(hold),
        "drop": len(drops),
        "negative": len(negative),
        "bangumi_total": bangumi_total,
        "bilibili_total": bilibili_total,
        "site_counts": site_counts,
        "type_counts": dict(type_counter),
        "type_shares": type_shares,
        "top_tags": top_tags,
        "top_genres": top_genres,
        "genre_counts": dict(genre_counter),
        "avoided_tags": avoided_tags,
        "top_studios": top_studios,
        "entry_year": entry["year"] if entry else None,
        "early_share": early_share,
        "latest_year": latest["year"] if latest else None,
        "entry_work": entry["title"] if entry else None,
        "latest_work": latest["title"] if latest else None,
        "avg_score": round(avg_score, 2) if avg_score else None,
        "avg_popularity": int(avg_popularity) if avg_popularity else None,
        "dimensions": dimensions,
        "dimension_version": DIMENSION_VERSION,
        "identity": identity,
        "cluster": cluster,
        "persona_tags": [f"{cluster}系", identity] + traits,
        "representative": _representative(interest),
        "notes": notes or [],
    }


def _compose_long_review(
    profile: dict, rarity: dict | None = None, seed: str = ""
) -> str:
    """规则兜底长评：用事实锚定的毒舌 / 梗文体拼一篇专属成分长文。

    仅在用户没有 LLM 文案时作为报告页主视觉长块；所有结论都来自画像事实，
    不编造（与「LLM 只写文案、规则负责事实」的总原则一致）。
    中间层 else 分支与收尾锐评是普通用户全落的地方，按 seed 轮转圈内自嘲句，
    避免「人人同句」。
    """

    def _pct(v: float) -> int:
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return 0

    seed = _seed_of(profile, seed)
    dims = profile.get("dimensions") or {}
    cluster = profile.get("cluster", "")
    identity = profile.get("identity", "")
    top = [t["name"] for t in (profile.get("top_genres") or profile.get("top_tags") or [])]
    avoided = [t["name"] for t in profile.get("avoided_tags") or []]
    studios = [s["name"] for s in profile.get("top_studios") or []]
    total = int(profile.get("total") or 0)
    want = int(profile.get("want") or 0)
    seen = int(profile.get("seen") or 0)
    drop = int(profile.get("drop") or 0)
    entry_year = profile.get("entry_year")
    latest_year = profile.get("latest_year")
    entry_work = profile.get("entry_work")
    latest_work = profile.get("latest_work")
    top3 = "、".join(top[:3])

    # 样本过小：硬编成分只会说出「1 部看完，翻来覆去还是这几样」这种废话
    # （问答试算常见只标 1–3 部的用户），不如老实说结论站不住。
    if total < 8:
        shell = f"「{cluster}系{identity}」" if (cluster or identity) else "这一档"
        return (
            f"现在只标了 {total} 部，成分还看不出来——勉强算{shell}的轮廓。"
            "多看几部、多标几部再来，结论才站得住。"
        )

    # 入坑线。
    # entry_year 实际是「收藏里最早一部作品的**发行年**」，并不等于用户真正的入坑年。
    # 收藏里躺着 1928 年《米老鼠》这类上古条目时，照字面说「1928 年入坑、整整 98 年
    # 没爬出来」会荒谬到出戏（2026-10-28 线上实际出现）。所以先判年代是否可信：
    # 太老的只用「片单能追到哪一年」陈述事实，不猜资历。
    plausible_entry = entry_year is not None and 2005 <= int(entry_year) <= 2026
    if entry_year and latest_year and entry_work and latest_work:
        span = max(0, int(latest_year) - int(entry_year))
        if not plausible_entry:
            para_intro = (
                f"片单往前能追到 {entry_year} 年的《{entry_work}》，"
                f"最新停在 {latest_year} 年的《{latest_work}》——"
                + (
                    "老的新的都能啃，一点不挑。"
                    if span >= 40
                    else f"横跨 {span} 年的口味，补老番是认真的。"
                )
            )
        elif span <= 1:
            para_intro = (
                f"入坑不算早，但够稳：{entry_year} 年因为《{entry_work}》一头栽进来，"
                f"到现在《{latest_work}》还在追，几年下来坑越挖越深。"
            )
        else:
            para_intro = (
                f"入坑不算晚——{entry_year} 年因为《{entry_work}》掉进坑里，"
                f"一路看到 {latest_year} 年的《{latest_work}》，整整 {span} 年没爬出来。"
            )
    elif entry_work:
        para_intro = (
            f"入坑年份算不太清，但《{entry_work}》显然是个起点，"
            f"收藏里攒下的这些东西说明此人早已深陷二次元。"
        )
    else:
        para_intro = "入坑年份算不清，但收藏里东西不少，显然早已深陷，属于退坑失败的典型。"

    # 口味画像（必须带数字锚：只给判断不给数字，读者不会觉得「被说中」）
    if _pct(dims.get("tag_focus", 0)) >= 78:
        para_taste = (
            f"口味上是个典型的「{cluster}系{identity}」，而且相当专一："
            f"{total} 部看完，翻来覆去还是{top3 or '那几样'}——标签比本人还了解自己。"
        )
    elif _pct(dims.get("exploration", 0)) >= 70:
        para_taste = (
            f"口味上是「{cluster}系{identity}」，但好奇心太重——"
            + (f"{top3}什么都碰，" if top3 else "什么类型都碰，")
            + f"{total} 部的收藏夹像座没收拾的资料馆。"
        )
    else:
        para_taste = (
            f"口味上是「{cluster}系{identity}」，铺得比较开，"
            + (f"{top3}都沾一点，" if top3 else "什么类型都沾一点，")
            + f"{total} 部下来没给自己设墙。"
        )

    # 口碑 / 热度轴
    m = _pct(dims.get("mainstream", 0))
    q = _pct(dims.get("quality", 0))
    if m >= 85 and q >= 65:
        para_axis = (
            "热榜上的你追，8 分以上的你存，两头都不落——"
            "别人还在吵「看主流还是看冷门」，你两边都有票。"
        )
    elif m < 35:
        para_axis = (
            "热榜上的爆款你多半没点，冷门倒是挖了一堆——"
            "「这你也看过？」这句话，你听得最多。"
        )
    elif q >= 80:
        para_axis = (
            "低于 7 分的不看，8 分以下的不存，烂片在你这里约等于浪费生命。"
        )
    else:
        # 中间层（普通用户全落这）：原「追番随缘…全看心情」太通用，换圈内自嘲轮转。
        para_axis = _pick(
            (
                "你的品味很标准，标准到专楼里没人跟你吵——因为你说得都对。",
                "大数据说你是典型观众，典型到制作委员会拿你当 KPI，精神状态稳定得可怕。",
                "追番随缘，不追任何人的脚步，但霸权更新你比谁都准，一集入教的体质你也有。",
            ),
            seed,
            salt="axis",
        )

    # 行为
    if drop and seen and drop >= seen * 0.4:
        para_behav = (
            f"但收藏有点「买书不读」——看过 {seen} 部，直接抛弃 {drop} 部，"
            "三集定律执行得很彻底，弃坑比填坑利索多了。"
        )
    elif seen and want > seen * 1.3:
        para_behav = (
            f"待看的比看完的多出一大截（想看/在看 {want} / 看过 {seen}），"
            "收藏夹像个纪念碑，立了一堆「迟早补」的番，多半还在吃灰。"
        )
    elif _pct(dims.get("exploration", 0)) >= 70:
        para_behav = (
            "加新番的速度永远比看完快，属于「先 mark 再说」的行动派，"
            "收藏夹的尾巴比开头还长。"
        )
    else:
        para_behav = (
            f"{seen} 部看过，{drop} 部抛弃——弃番对你来说不是止损，是认输。"
        )

    quip = _pick(
        (
            f"——{total} 部看完，你最大的成就是再也不会被任何一部骗到。",
            f"——{total} 部下来，口味定型了；好消息是定型了，坏消息也是。",
            f"——{total} 部，够你把「这我不看」讲得理直气壮。",
        ),
        seed,
        salt="quip",
    )
    return "\n\n".join([para_intro, para_taste + para_axis, para_behav, quip])


def build_template_report(
    profile: dict, rarity: dict | None = None, seed: str = ""
) -> dict:
    """不依赖 LLM 的兜底报告：同样覆盖样例里的 11 个模块。

    seed 为轮转种子（用户名），决定多 Variant 毒舌 / 长评 / 分享文案取哪一句。
    """
    top_names = [
        tag["name"] for tag in (profile.get("top_genres") or profile["top_tags"])
    ]
    avoided_names = [tag["name"] for tag in profile["avoided_tags"]]
    studios = [studio["name"] for studio in profile["top_studios"]]
    traits = profile["persona_tags"][2:]
    entry_year, latest_year = profile["entry_year"], profile["latest_year"]
    entry_work, latest_work = profile["entry_work"], profile["latest_work"]
    total, want, seen = profile["total"], profile["want"], profile["seen"]
    seed = _seed_of(profile, seed)

    hero_title, hero_code, hero_note = assign_title(profile, rarity, seed)

    title = (
        f"「{profile['cluster']}系{profile['identity']}」"
        + (" · 兼「" + "」「".join(traits) + "」" if traits else "")
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
        advice = "想看的比看过的多出一截，先把收藏夹里的存货消化一拨，别让它们只当纪念碑。"
    elif _dim(profile, "mainstream") < 35:
        advice = "你偏爱冷门，顺着自己的标签慢慢挖就好，不用勉强去补榜单上的所谓神作。"
    elif _dim_at_least(profile, "tag_focus", 78):
        advice = "口味很集中，照着高频标签找同类型新作，命中率会很高。"
    else:
        advice = "口味比较均衡，偶尔跳一下舒适区也挺好，但不必勉强。"

    return {
        # 文案版本戳：_backfill_template_report 靠它判断老报告要不要换新文案
        "copy_version": COPY_VERSION,
        "title": title,
        "hero_title": hero_title,
        "hero_code": hero_code,
        "hero_note": hero_note,
        "share_line": _share_line(hero_title, hero_note, profile, seed),
        "long_review": _compose_long_review(profile, rarity, seed),
        "subtitle": f"基于 {total} 部作品的御宅成分",
        "entry_story": entry_story,
        "core_taste": (
            f"你的口味集中在 {'、'.join(top_names[:5]) or '暂不明显'}，"
            f"{profile['identity']}属性最突出。"
        ),
        "dependencies": f"常出现的制作公司：{'、'.join(studios) or '数据不足'}。",
        "favorite_genres": top_names[:8],
        "avoided_genres": avoided_names[:6],
        "persona_tags": profile["persona_tags"],
        "trait_blurbs": {t: TRAIT_BLURBS.get(t, "") for t in profile["persona_tags"]},
        "advice": advice,
        # 锐评块：与长评收尾分工——长评收尾带数字，这里给一句不带「N 部」的冷评，
        # 否则两处都在说「看了 N 部 + 审美」，连着读就是重复。
        "final_quip": _pick(
            (
                "别人的口味是爱好，你的口味是证据。",
                "你不是在挑番，你是在反复确认自己喜欢什么。",
                "这份成分表比你自己诚实——它记得你抛弃过的每一部。",
            ),
            seed,
            salt="final_quip",
        ),
        "data_note": (
            f"Bangumi {profile['bangumi_total']} 条"
            + (f" + B站追番 {profile['bilibili_total']} 条" if profile.get("bilibili_total") else "")
            + f" + 站内收藏 {profile['site_counts'].get('favorites', 0)} 条；看过 {seen} / "
            f"想看/在看 {want} / 抛弃 {profile['drop']}。Bangumi 不提供首次收藏时间，"
            "入坑年份用作品发行年近似；避雷标签来自抛弃 / 低分作品。"
        ),
    }


# 稀有度分档：桶就是**称号本身**（share 是百分数）。
#
# 之前按 persona_tags 前两项（主身份 · 题材簇）分桶，但页面上展示给用户的是称号，
# 「稀有度」说的却是另一个东西——模板里那句「稀有度说的是这顶称号有多稀有」的
# 注释一直没兑现。真实样本攒够之前没有占比可算，所以先挂一份先验：
# 336 个模拟画像跑出来的实测占比（scripts/gen_persona_samples.py →
# output/calib/title_freq.json，2026-10-30）。真实计数过线后自动接管。
#
# 只收录**校准样本里出现过**的称号。一次都没出现的不在这里，走 RARITY_UNRATED：
# 那不是「稀有」，是「我们还没测出来」——硬塞进 SSR 会让 64% 的称号都挂最高档，
# 稀有度直接失效。等真实数据攒够，这些称号再按实测占比入档。
TITLE_RARITY_SHARE: dict[str, float] = {
    "FLOAT": 9.23,
    "KOUKO": 6.25,
    "OMNI-SCORE": 5.65,
    "HAKUGAKU": 5.06,
    "FINISH-ALL": 4.46,
    "GUNPLA": 4.46,
    "FUJOSHI": 4.17,
    "RIAJUU": 4.17,
    "BAND-MAN": 3.87,
    "HAITOU": 3.87,
    "NANMIN": 3.57,
    "KAWABUTA": 3.27,
    "MOE-BUTA": 3.27,
    "NO-TYPE": 3.27,
    "SENBA": 2.98,
    "TOUKI": 2.98,
    "FRAME-NERD": 2.68,
    "TRUE-OMNI": 2.68,
    "DROP-GOD": 2.38,
    "HOUOUIN": 2.38,
    "KOI": 2.38,
    "HAKO-OSHI": 2.08,
    "MEDIAN": 2.08,
    "SHIP-STOCK": 2.08,
    "OMNI-COLD": 1.79,
    "ONSITSU": 1.79,
    "SHIBOU-FLAG": 1.49,
    "ZAISEKI": 1.49,
    "NENPYOU": 1.19,
    "ALL-WANT": 0.89,
    "HAYARI": 0.89,
    "KOUON": 0.60,
    "MEME-POLICE": 0.30,
    "PIXIU": 0.30,
}

# 阈值比旧版（2/8/25）整体下调：旧版分的是 32 个成分桶，现在分的是 90+ 个称号，
# 桶多了自然每个桶更瘦，沿用旧阈值会把所有人都判成 SSR。
RARITY_TIERS = (
    (1.0, "SSR", "婆罗门", "万里挑一"),
    (2.5, "SR", "刹帝利", "千里挑一"),
    (5.5, "R", "吠舍", "百里挑一"),
    (101.0, "N", "路人", "满地都是"),
)

# 校准样本里零命中的称号：不编一个百分比，也不假装它稀有。
RARITY_UNRATED = {"tier": "?", "label": "未标定", "note": "图鉴未收录"}

# 等级名号（2026-10-30 用户定：字母 + 名号并存）。
# 字母留给抽卡爽感，名号用御宅种姓梗——「婆罗门」在圈内就是最高档的意思。
# 最低档用「路人」而不是「首陀罗」：大多数用户都落在这一档，按种姓最底层
# 叫他们不合适（此前「达利特认证失败」也因同样的原因被否掉）。
RARITY_LABELS = {tier: label for _limit, tier, label, _note in RARITY_TIERS}
RARITY_LABELS[RARITY_UNRATED["tier"]] = RARITY_UNRATED["label"]

# 真实样本达到多少人之后才敢用真实占比：分母小的时候百分比抖得厉害，
# 「才 3 个人测过」报出来是社死不是炫耀。没到线就一直用校准先验。
REAL_RARITY_MIN_TOTAL = 200

# 称号桶的键前缀。旧的成分桶键长成「番剧党·恋爱」，加前缀才能跟它们分开统计
# （分母只算 T: 开头的行，否则旧数据会把分母撑大、把所有人算得偏稀有）。
TITLE_KEY_PREFIX = "T:"


def rarity_label(tier: str | None) -> str:
    """等级名号；未知等级（旧快照 / 脏数据）回落到「路人」，不留空。"""
    return RARITY_LABELS.get(str(tier or "").upper(), RARITY_LABELS["N"])


def rarity_of(share: float | None) -> dict:
    """占比 -> 分级（tier 字母 + label 名号 + note 白话描述）。

    share 为 None 表示「这个称号在校准样本里没出现过」，走未标定档——
    不把它算成 0%（那会让零命中变成最稀有的一档，与事实相反）。
    """
    if share is None:
        return dict(RARITY_UNRATED)
    for limit, tier, _label, note in RARITY_TIERS:
        if share <= limit:
            return {"tier": tier, "label": rarity_label(tier), "note": note}
    return {"tier": "N", "label": rarity_label("N"), "note": "满地都是"}


def title_rarity(code: str, real_share: float | None = None) -> dict:
    """称号 -> 稀有度。给了真实占比就用真实的，否则退回校准先验。"""
    share = real_share if real_share is not None else TITLE_RARITY_SHARE.get(code)
    return {"code": code, "share": share, **rarity_of(share)}


def attach_rarity(db, profile: dict, code: str = "") -> dict:
    """给画像挂上稀有度并记一次计数。

    桶是称号 itself：没传 code 就现场算一遍（seed 只影响毒舌句轮转，不影响
    命中哪个称号，所以跟结果页展示的称号一定一致）。

    统计只是锦上添花：写库一旦失败就返回空 dict，绝不能让结果页因为一个
    展示位 500。
    """
    if not code:
        try:
            code = assign_title(profile)[1]
        except Exception:  # noqa: BLE001 - 算不出称号也不能让结果页 500
            return {}
    key = (TITLE_KEY_PREFIX + str(code))[:60]
    try:
        count, total = crud.record_persona_label(db, key, total_prefix=TITLE_KEY_PREFIX)
    except Exception:  # noqa: BLE001
        return {}
    use_real = total >= REAL_RARITY_MIN_TOTAL and count > 0
    share = round(count / total * 100, 1) if use_real else TITLE_RARITY_SHARE.get(code)
    return {
        "key": key,
        "code": code,
        "count": count,
        "total": total,
        "share": share,
        "source": "real" if use_real else "prior",
        **rarity_of(share),
    }


def recommend_from_site(
    db, profile: dict, exclude_bangumi_ids: set[int] | None = None, limit: int = 6
) -> list[dict]:
    """按画像里的本命 / 避雷题材，从站内作品库挑推荐（规则算，不联网）。"""
    positive = [
        tag["name"] for tag in (profile.get("top_genres") or profile["top_tags"])
    ][:8]
    avoided = [tag["name"] for tag in profile["avoided_tags"]]
    return crud.recommend_contents(
        db,
        positive_tags=positive,
        avoided_tags=avoided,
        exclude_bangumi_ids=exclude_bangumi_ids,
        limit=limit,
    )


# 登录用户首页推荐：「站内收藏 / 标注」实时信号 vs 「成分检测画像」的切换双闸。
# 站内条数要同时满足「≥ 画像总数 × 比例」和「≥ 画像总数 + 基数」才弃用画像走
# 实时口径——刚同步完 Bangumi 的用户不会因为站内多收藏了两三部就翻推荐
# （比例闸挡小涨幅，基数闸挡小基数下的百分比抖动）。
HOME_TASTE_PROFILE_RATIO = 1.5
HOME_TASTE_PROFILE_BASE = 10

# 「总口味 / 近期口味」双象：近期 = 活动时间落在最近 HOME_TASTE_RECENT_DAYS 天内
# 的收藏 / 标注（批量导入当晚也全算近期），不足 HOME_TASTE_RECENT_MIN 条时回落
# 「按活动时间倒序取最近 MIN 条」——几乎没动静的用户，最近几次动作就是近期口味。
# 近期命中的题材权重为总口味的 HOME_TASTE_RECENT_WEIGHT 倍（最近在迷什么优先）。
# 浏览记录不进算法（浏览≠喜欢，噪音大于信号）。
HOME_TASTE_RECENT_DAYS = 90
HOME_TASTE_RECENT_MIN = 3
HOME_TASTE_RECENT_GENRES = 4
HOME_TASTE_RECENT_WEIGHT = 2.0


def _top_genre_names(profile: dict) -> list[str]:
    """画像的本命题材名（成分检测同款口径：题材标签优先，回落到全部标签）。"""
    return [
        tag["name"]
        for tag in (profile.get("top_genres") or profile.get("top_tags") or [])
    ]


def build_user_home_picks(db, user_id: int, *, limit: int = 8) -> list | None:
    """登录用户的首页推荐作品：画像口径 / 站内实时口径，总 / 近期口味双象。

    与游客的站长口味（crud.list_home_recommendations）是两套独立口径；
    没有任何可用信号时返回 None，由调用方回落站长口味，保证推荐位不空。

    信号决策：
    1. 有成分检测画像、且站内收藏数未超「基数 + 比例」双闸 → 画像口径：
       直接复用报告里的 top_genres / avoided_tags（Bangumi + 站内合并算过，
       含避雷与个人评分，信号最全），排除集取已同步的 Bangumi 条目；
    2. 否则（无画像 / 画像被站内行为稀释）→ 站内实时口径：收藏 / 标注聚合
       一整套 build_profile 当「总口味」，再按活动时间取最近 N 条聚合一套当
       「近期口味」，两组题材加权后交给 recommend_contents；已收藏 / 标注的
       作品不进推荐位；实时口径把旧画像的本命题材并入总口味、避雷标签原样
       继承——站内没有抛弃 / 低分这类负向信号，跑过成分检测的用户的口味与
       雷区不因为切实时而丢失。

    显式口味（题材问卷）暂未开放：positive_weights 的合并位已留好，
    将来问卷落地只需在加权表里并入问卷题材，下游匹配不用动。
    """
    rows = crud.list_user_taste_rows(db, user_id)
    report = crud.get_persona_report(db, user_id)
    payload = (report.payload or {}) if report else {}
    profile = payload.get("profile") if isinstance(payload, dict) else None
    profile = profile if isinstance(profile, dict) else None
    profile_total = int(profile.get("total") or 0) if profile else 0

    if profile and profile_total > 0:
        site_total = len(rows)
        stale = (
            site_total >= profile_total * HOME_TASTE_PROFILE_RATIO
            and site_total >= profile_total + HOME_TASTE_PROFILE_BASE
        )
        if not stale:
            exclude = {
                int(item.subject_id)
                for item in crud.list_bangumi_collections(db, user_id)
            }
            picks = recommend_from_site(
                db, profile, exclude, limit=max(limit * 4, limit)
            )
            if picks:
                return crud.dedupe_series_picks(db, picks, limit=limit)
            # 画像口径零命中（口味太窄）：先试实时口径，不急着回落站长口味

    if not rows:
        return None

    # 总口味：全部收藏 / 标注聚合一整套画像（与成分检测同一 build_profile 口径）
    ordered_rows = sorted(
        rows, key=lambda row: row[4] or datetime.min, reverse=True
    )
    total_works = [
        work_from_site(content, status, tags)
        for content, tags, status, _favorited, _activity in ordered_rows
    ]
    total_profile = build_profile(total_works)
    weighted: dict[str, float] = {}
    # 旧画像（被稀释但仍有参考价值）的本命题材并入总口味，权重 1.0
    if profile:
        for tag in _top_genre_names(profile)[:8]:
            weighted.setdefault(tag, 1.0)
    for tag in _top_genre_names(total_profile)[:8]:
        weighted.setdefault(tag, 1.0)
    # 近期口味：时间窗内的活动聚合第二套画像，命中的题材权重抬高
    cutoff = datetime.now() - timedelta(days=HOME_TASTE_RECENT_DAYS)
    recent_rows = [
        row for row in ordered_rows if (row[4] or datetime.min) >= cutoff
    ]
    if len(recent_rows) < HOME_TASTE_RECENT_MIN:
        recent_rows = ordered_rows[:HOME_TASTE_RECENT_MIN]
    if len(recent_rows) >= HOME_TASTE_RECENT_MIN:
        recent_profile = build_profile(
            [
                work_from_site(content, status, tags)
                for content, tags, status, _favorited, _activity in recent_rows
            ]
        )
        for tag in _top_genre_names(recent_profile)[:HOME_TASTE_RECENT_GENRES]:
            weighted[tag] = HOME_TASTE_RECENT_WEIGHT
    if not weighted:
        return None

    avoided = [tag["name"] for tag in (profile or {}).get("avoided_tags") or []]
    exclude = {
        int(content.bangumi_id)
        for content, _tags, _status, _favorited, _activity in rows
        if content.bangumi_id is not None
    }
    picks = crud.recommend_contents(
        db,
        positive_tags=list(weighted),
        avoided_tags=avoided,
        exclude_bangumi_ids=exclude,
        limit=max(limit * 4, limit),
        positive_weights=weighted,
    )
    if not picks:
        return None
    skip = {content.id for content, _t, _s, _f, _a in rows}
    return crud.dedupe_series_picks(db, picks, limit=limit, skip_content_ids=skip)


def build_llm_prompt(
    profile: dict,
    tone: str = "sharp",
    tone_prompt: str = "",
    extra_prompt: str = "",
    recommendations: list | None = None,
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
        "维度分数": profile["dimensions"],
        "人格标签": profile["persona_tags"],
        "代表作": profile["representative"],
        "站内收藏数": profile["site_counts"].get("favorites", 0),
    }
    if recommendations:
        # 让 LLM 的「建议 / 推荐」围绕规则筛好的候选展开，不要自己编片单
        facts["站内推荐候选(按本命题材命中筛出，优先推荐这些)"] = [
            f"{item['title']}（命中：{'、'.join(item['matched'][:3])}）"
            for item in recommendations[:8]
        ]
    return (
        "你是一个懂 ACGN 的「御宅成分分析师」。请根据下面的事实清单，用中文写一份人格报告。\n\n"
        f"语气要求：{tone_text}\n\n"
        "必须包含这些部分，用「## 部分名」分段：\n"
        "1. 称号（请直接沿用事实清单里的「人格标签」成分词——神作党 / 口碑党 / 主流厨 / "
        "冷门挖掘机 / 偏食 / 博爱 / 仓鼠——再叠加一个你写的俏皮称号；"
        "例如「热血系番剧党」兼「神作党」兼「冷门挖掘机」）\n"
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
    temperature: float = 0.8,
) -> str:
    """调用 OpenAI 兼容的 /chat/completions 接口；失败时抛 PersonaError。

    temperature 默认 0.8（画像文案要有点人味）；资讯翻译这类要求稳定的场景传低值。
    """
    base = base_url.rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
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
    """维度雷达图坐标（纯 SVG 用，不依赖图表库）。"""
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
        # 缺失维度按中性 50 画：旧报告没有「追新度」，当 0 会出一个假缺角
        score = max(0.0, min(100.0, float(dimensions.get(key, 50))))
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
                # 数值直接画在雷达轴侧（报告页不再单列一排 chips）
                "value": int(round(score)),
            }
        )
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


# ---------- 分享卡（竖版 SVG，直接下载，不引入图片库） ----------
#
# 结果页是「死胡同」：算完只能自己看，没有任何可以晒出去的东西。这里把画像压成
# 一张竖版卡，纯字符串拼 SVG——卡片内容只有文字与多边形，不需要 Pillow / cairosvg
# 这类图片依赖，浏览器与社交平台都能直接显示（2026-10-28）。
#
# 参数走 base64url 而不是存库：整份 payload 塞不进 URL，分享卡也不需要作品明细，
# 只带「人格标签 + 维度值 + 稀有度」三项，URL 一百多字节。


def encode_card(
    profile: dict,
    rarity: dict | None = None,
    hero_title: str = "",
    hero_code: str = "",
    quip: str = "",
    total: int = 0,
) -> str:
    """把画像压成分享卡参数（base64url 编码）。

    早期版本只带人格标签 + 维度 + 稀有度，分享卡因此只能展示干巴巴的口味标签，
    把整套「称号 / 罗马音代号 / 毒舌暴击句」全丢了——而那偏偏是引流钩子。
    这里补上 hero 三件套（称号、代号、一句话暴击）与作品总数（2026-10-28 重做）。
    """
    dims = profile.get("dimensions") or {}
    data: dict = {
        "t": [str(tag) for tag in (profile.get("persona_tags") or [])][:4],
        # 旧报告缺新维度（冷热跨度/年代跨度/情绪取向）时按中性 50，别显示成 0
        "d": [
            round(float(dims[key]) if dims.get(key) is not None else 50.0, 1)
            for key, _label in DIMENSION_LABELS
        ],
    }
    if rarity and rarity.get("tier"):
        # share 为 None = 未标定，必须原样传下去。这里早年写成 `or 0`，
        # 结果「未标定」被当成 0%（0% 在分档里是最高档 SSR），意思完全反了。
        raw_share = rarity.get("share")
        data["r"] = [
            str(rarity.get("tier")),
            None if raw_share is None else round(float(raw_share), 1),
            int(rarity.get("total") or 0),
        ]
    if hero_title:
        data["h"] = [str(hero_title)[:20], str(hero_code)[:24], str(quip)[:40]]
    if total:
        data["n"] = int(total)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_card(token: str) -> dict | None:
    """还原分享卡参数；畸形输入一律返回 None——这个 URL 是对公网开放的。"""
    text = (token or "").strip()
    if not text or len(text) > 2000:
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, UnicodeEncodeError, binascii.Error):
        return None
    if not isinstance(data, dict):
        return None
    tags, raw_dims = data.get("t"), data.get("d")
    if not isinstance(tags, list) or not isinstance(raw_dims, list):
        return None

    clean_dims: list[float] = []
    for value in raw_dims[: len(DIMENSION_LABELS)]:
        try:
            clean_dims.append(max(0.0, min(100.0, float(value))))
        except (TypeError, ValueError):
            clean_dims.append(0.0)
    while len(clean_dims) < len(DIMENSION_LABELS):
        clean_dims.append(0.0)

    rarity = data.get("r")
    hero = data.get("h")
    hero_fields = (
        [str(x)[:40] for x in hero]
        if isinstance(hero, list) and len(hero) == 3
        else None
    )
    raw_total = data.get("n")
    total = int(raw_total) if isinstance(raw_total, int) else 0
    return {
        "tags": [str(tag)[:20] for tag in tags][:4],
        "dims": clean_dims,
        # 只接受合法形状，避免把任意 JSON 直接拼进 SVG
        "rarity": rarity if isinstance(rarity, list) and len(rarity) == 3 else None,
        "hero_title": hero_fields[0] if hero_fields else "",
        "hero_code": hero_fields[1] if hero_fields else "",
        "quip": hero_fields[2] if hero_fields else "",
        "total": total,
    }


# 稀有度徽章配色：SSR 金 > SR 蓝 > R 灰 > N 浅灰 > 未标定 虚线灰。稀有的炫耀、普通的弱化。
TIER_COLORS = {
    "SSR": "#E0A800",
    "SR": "#2b9ae0",
    "R": "#7b8896",
    "N": "#aab4bf",
    "?": "#c3ccd5",
}


def _share_text(share: float | None) -> str:
    """占比文案。小于 1% 要留一位小数——早年用 :.0f，0.3% 显示成「0%」。"""
    if share is None:
        return ""
    value = float(share)
    if value < 1:
        return f"{value:.1f}%"
    if value < 10:
        return f"{value:.1f}%"
    return f"{value:.0f}%"


def build_share_card(data: dict) -> str:
    """生成 600×800 竖版分享卡 SVG。

    重做重点（2026-10-28）：
    - 视觉核心从「口味标签」换成真正的**称号**（hero_title）+ 罗马音代号（hero_code），
      这是整套人格检测的引流钩子，之前被 encode_card 丢掉了。
    - 补一行**毒舌暴击句**（hero_note），让人想截图转发。
    - 稀有度不再当众社死：样本没到 REAL_RARITY_MIN_TOTAL 就用校准先验（模拟画像
      实测占比），不暴露「才 N 人测过」。稀有档（SSR/SR/R）报「仅 X% 同好」的炫耀
      口径，大众档（N）只轻描「大众路线」，校准样本零命中的走「未标定 · 校准样本
      未收录」——不编百分比，也不把它算成最稀有的一档。
    """
    tags = data.get("tags") or []
    dims = data.get("dims") or []
    radar = build_radar(
        {
            key: (dims[index] if index < len(dims) else 50)
            for index, (key, _label) in enumerate(DIMENSION_LABELS)
        }
    )

    hero_title = data.get("hero_title") or (tags[0] if tags else "御宅成分")
    hero_code = data.get("hero_code") or ""
    quip = data.get("quip") or ""
    tag_line = " · ".join(tags) if tags else ""
    total = int(data.get("total") or 0)

    grid = "".join(
        f'<polygon points="{ring}" fill="none" stroke="#dbe4ee" stroke-width="1"/>'
        for ring in radar["grid"]
    )
    axes = "".join(
        f'<line x1="{axis["x1"]}" y1="{axis["y1"]}" '
        f'x2="{axis["x2"]}" y2="{axis["y2"]}" stroke="#dbe4ee" stroke-width="1"/>'
        for axis in radar["axes"]
    )
    labels = "".join(
        f'<text x="{label["x"]}" y="{label["y"]}" text-anchor="{label["anchor"]}" '
        f'dominant-baseline="middle" font-size="12" fill="#5b6b7a">{html.escape(label["text"])}</text>'
        for label in radar["labels"]
    )

    # 稀有度徽章：字母进胶囊、名号跟在后面（光有字母没人记得住是哪一档）。
    # 描述行：未标定的不编百分比，稀有的报占比，大众档只轻描一句。
    badge = ""
    desc = ""
    rarity = data.get("rarity")
    if rarity:
        tier, share, _rtotal = rarity
        tier = str(tier)
        color = TIER_COLORS.get(tier, "#7b8896")
        word = rarity_label(tier)
        pct = _share_text(share)
        if tier == RARITY_UNRATED["tier"] or share is None:
            desc = f"{word} · 校准样本未收录"
        elif tier in ("SSR", "SR", "R") and pct:
            desc = f"{word} · 仅 {pct} 同好"
        else:
            desc = f"{word} · 大众路线"
        # 胶囊宽度跟着「字母 + 名号」走，右边缘固定在 590
        width = 40 + len(word) * 13 + 14
        x0 = 590 - width
        pill = (
            f'<rect x="{x0}" y="32" width="{width}" height="30" rx="15" '
            f'fill="{color}"'
            + (
                f' stroke="#9aa7b4" stroke-width="1" stroke-dasharray="3 2"'
                if tier == RARITY_UNRATED["tier"]
                else ""
            )
            + "/>"
        )
        badge = (
            pill
            + f'<text x="{x0 + 12}" y="53" font-size="15" font-weight="bold" '
            f'fill="#ffffff">{html.escape(tier)}</text>'
            + f'<text x="{x0 + 18 + len(tier) * 11}" y="52" font-size="12" '
            f'fill="#ffffff" opacity="0.92">{html.escape(word)}</text>'
        )

    # 维度紧凑两栏（左 0/2/4…，右 1/3/5…）。9 维 → 5 行，行距压到 26 才不压页脚。
    score_rows = ""
    cols = ((56, 286), (320, 556))
    for index, (_key, label) in enumerate(DIMENSION_LABELS):
        col = 0 if index % 2 == 0 else 1
        y = 646 + (index // 2) * 26
        lx, vx = cols[col]
        val = dims[index] if index < len(dims) else 50
        score_rows += (
            f'<text x="{lx}" y="{y}" font-size="13" fill="#5b6b7a">{html.escape(label)}</text>'
            f'<text x="{vx}" y="{y}" text-anchor="end" font-size="13" '
            f'font-weight="bold" fill="#2b9ae0">{val:.0f}</text>'
        )

    q = (quip[:32] + "…") if len(quip) > 32 else quip
    if not q:
        q = "这成分，一看就是老宅了。"

    footer = (
        f"基于 {total} 部作品 · 由站内收藏与标注算出"
        if total
        else "由站内收藏与标注算出"
    )

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 800" width="600" height="800">'
        '<rect width="600" height="800" fill="#ffffff"/>'
        '<rect x="10" y="10" width="580" height="780" rx="18" fill="none" stroke="#eef3f8" stroke-width="2"/>'
        '<rect x="10" y="10" width="580" height="6" rx="3" fill="#2b9ae0"/>'
        f'<text x="40" y="50" font-size="15" font-weight="bold" fill="#2b9ae0">御宅成分检测</text>'
        f"{badge}"
        f'<text x="590" y="84" text-anchor="end" font-size="12" fill="#7b8896">{html.escape(desc)}</text>'
        f'<text x="40" y="134" font-size="46" font-weight="bold" fill="#1f2d3a">{html.escape(hero_title)}</text>'
        + (f'<text x="40" y="172" font-size="17" font-weight="bold" fill="#2b9ae0">{html.escape(hero_code)}</text>' if hero_code else "")
        + (f'<text x="40" y="200" font-size="13" fill="#8a97a4">{html.escape(tag_line)}</text>' if tag_line else "")
        + f'<text x="40" y="232" font-size="15" fill="#3f4d5a">{html.escape(q)}</text>'
        + f'<g transform="translate(60,255) scale(1.6)">{grid}{axes}'
        f'<polygon points="{radar["polygon"]}" fill="rgba(43,154,224,0.28)" '
        f'stroke="#2b9ae0" stroke-width="2"/>{labels}</g>'
        f"{score_rows}"
        f'<text x="40" y="786" font-size="11" fill="#9aa7b4">{html.escape(footer)}</text>'
        "</svg>"
    )
