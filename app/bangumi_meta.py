"""Bangumi 元数据补全：客观信息 / 制作人员 / 角色声优。

为什么单独一层：
- 「题材标签」存 `content_tags`，是主观、可筛选的口味词；
- 「制作信息」是人/组织，有明确职位，只展示不参与标签筛选；
- 「客观信息」是单值事实（媒介 / 原作载体 / 话数 / 放送星期），只展示。

数据来源（实测 v0 接口）：
- ``GET /v0/subjects/{id}``：platform、eps、date、infobox（导演 / 脚本 / 音乐 /
  动画制作 / 原作 / 放送星期 / 播放结束 / 官方网站 / 别名 …）；
- ``GET /v0/subjects/{id}/characters``：角色 + 演员数组，**第一位是原版配音**
  （后面是中配 / 英配），因此只取第一位，翻配不入库；
- ``GET /v0/subjects/{id}/persons``：247 条职员表，含原画 / 作画监督等长尾。
  长尾先存成候选（tier=candidate），本站累计 ≥10 部作品才转正展示——
  不维护「知名名单」，避免名单过时，数据越多越准。

补全时机：详情页首次访问按需补（失败静默）+ 后台脚本对 engaged 作品批量补。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import crud
from app import persona
from app.config import settings
from app.models import Content, ContentCredit

# 主创：写进现有实体体系（entities / content_entity_links），
# 详情页「关系」区块与 /creators/{name} 索引页复用，不再单开一套。
ENTITY_ROLE_MAP = {
    "导演": ("导演", "person"),
    "系列构成": ("系列构成", "person"),
    "音乐": ("音乐", "person"),
    "人物设定": ("角色设计", "person"),
    "原作": ("原作", "person"),
    "动画制作": ("动画制作", "company"),
}

# 实体体系没有覆盖、但读者会关心的职位，放 content_credits（只展示，不做索引）。
EXTRA_ROLES = ("副导演", "脚本", "美术监督", "音响监督")

# 长尾职位（来自 persons 接口）：只收知名 / 有作品积累的人。
LONG_ROLES = (
    "总作画监督",
    "作画监督",
    "机械作画监督",
    "动作作画监督",
    "原画",
    "分镜",
    "演出",
)

# 长尾职员转正门槛：在本站累计出现 ≥10 部作品才展示（数据驱动，不维护名单）。
LONG_TIER_MIN_WORKS = 10
# 原文简介入库上限（Bangumi 个别条目简介极长，避免把整页撑爆）
SUMMARY_MAX_LEN = 4000

# 客观信息里值得展示的键（其余 infobox 一律丢弃，避免「没人关心的信息占服务器」）。
FACT_KEYS = ("放送星期", "播放结束", "放送开始", "官方网站", "别名", "制作", "企画")

_SOURCE_RULES = (
    ("漫画", "漫画原作"),
    ("轻小说", "小说原作"),
    ("小说", "小说原作"),
    ("游戏", "游戏原作"),
    ("原创", "原创"),
)

_ANNOTATION_RE = re.compile(r"[（(].*?[）)]")
_SPLIT_RE = re.compile(r"[、；;/／]")


def _clean_name(value: str) -> str:
    return _ANNOTATION_RE.sub("", value).strip()


def infobox_map(payload: dict) -> dict[str, list[str]]:
    """把 infobox 摊平成 {key: [值...]}，兼容字符串与 [{v: ...}] 两种形态。"""
    result: dict[str, list[str]] = {}
    for item in payload.get("infobox") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = item.get("value")
        values: list[str] = []
        if isinstance(value, str):
            values = [part.strip() for part in _SPLIT_RE.split(value) if part.strip()]
        elif isinstance(value, list):
            for entry in value:
                text = entry.get("v") if isinstance(entry, dict) else entry
                text = str(text or "").strip()
                if text:
                    values.append(text)
        if key and values:
            result.setdefault(key, []).extend(values)
    return result


def classify_source_material(raw_values: list[str]) -> str | None:
    """原作载体归类：漫画原作 / 小说原作 / 游戏原作 / 原创。"""
    joined = " ".join(raw_values)
    if not joined:
        return None
    for keyword, label in _SOURCE_RULES:
        if keyword in joined:
            return label
    return None


def media_label(payload: dict) -> str | None:
    platform = str(payload.get("platform") or "").strip()
    if not platform:
        return None
    mapping = {
        "TV": "TV",
        "剧场版": "剧场版",
        "OVA": "OVA",
        "OAD": "OVA",
        "WEB": "Web",
        "Web": "Web",
        "游戏": "游戏",
        "小说": "小说",
        "漫画": "漫画",
    }
    return mapping.get(platform, platform[:20])


def parse_year(date_text) -> int | None:
    head = str(date_text or "")[:4]
    return int(head) if head.isdigit() else None


def clean_summary(raw) -> str:
    """Bangumi 简介清洗：去首尾空白、压掉内部连续空行，超长截断。

    返回空串表示这部没有原文简介（调用方据此不写库、详情页不显示切换入口）。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    # Bangumi 的 summary 常带成对换行，HTML 里会变成额外空行；压成单行更贴近阅读习惯
    text = re.sub(r"\s*\n\s*", " ", text)
    return text[:SUMMARY_MAX_LEN]


def build_meta(
    subject: dict,
    characters: list | None = None,
    persons: list | None = None,
) -> dict:
    """把 Bangumi 原始响应归一成可入库结构（纯函数，便于测试）。"""
    info = infobox_map(subject)

    # 主创 -> 实体关联（导演 / 系列构成 / 音乐 / 角色设计 / 原作 / 动画制作）
    entity_roles: list[dict] = []
    for key, (role, entity_type) in ENTITY_ROLE_MAP.items():
        for raw_name in info.get(key, []):
            name = _clean_name(raw_name)
            if name:
                entity_roles.append(
                    {"role": role, "name": name[:120], "entity_type": entity_type}
                )

    # 实体体系未覆盖的职位 -> content_credits（只展示）
    credits: list[dict] = []
    order = 0
    for role in EXTRA_ROLES:
        for raw_name in info.get(role, []):
            name = _clean_name(raw_name)
            if not name:
                continue
            credits.append(
                {"person_id": None, "role": role, "name": name, "detail": None,
                 "sort_order": order, "tier": "main"}
            )
            order += 1

    for person in persons or []:
        if not isinstance(person, dict):
            continue
        role = str(person.get("relation") or "").strip()
        name = str(person.get("name") or "").strip()
        if role not in LONG_ROLES or not name:
            continue
        # 长尾先全部记成候选，等本站作品数够了再转正（见 crud.promote_staff_candidates）
        credits.append(
            {
                "person_id": person.get("id"),
                "role": role,
                "name": name,
                "detail": None,
                "sort_order": order,
                "tier": "candidate",
            }
        )
        order += 1

    cast: list[dict] = []
    for index, character in enumerate(characters or []):
        if not isinstance(character, dict):
            continue
        actors = character.get("actors") or []
        if not actors:
            continue
        primary = actors[0] if isinstance(actors[0], dict) else {}
        actor = str(primary.get("name") or "").strip()
        character_name = str(character.get("name") or "").strip()
        if not actor or not character_name:
            continue
        cast.append(
            {
                "character": character_name[:100],
                "character_id": character.get("id"),
                "person_id": primary.get("id"),
                "actor": actor[:100],
                "sort_order": index,
            }
        )
        if len(cast) >= 12:  # 详情页只展示前几位，多存无益
            break

    studios = [name for name in info.get("动画制作", []) if name][:3]

    facts: dict[str, str] = {}
    for key in FACT_KEYS:
        values = info.get(key)
        if not values:
            continue
        text = " / ".join(values)
        if key == "别名":
            text = " / ".join(values[:3])
        facts[key] = text[:500]

    episodes = subject.get("eps") or subject.get("total_episodes")
    try:
        episodes = int(episodes) if episodes else None
    except (TypeError, ValueError):
        episodes = None

    return {
        "media": media_label(subject),
        "source_material": classify_source_material(info.get("原作", [])),
        "episodes": episodes,
        "year": parse_year(subject.get("date")),
        "studios": studios,
        "entity_roles": entity_roles,
        "credits": credits,
        "cast": cast,
        "facts": facts,
        # Bangumi 条目自带的简介（原始语言，多为日文）→ 详情页语言切换的「原文」。
        # 与中文简介相同的条目由 sync_content_meta 挡掉，不会出现两份一样的文字。
        "summary_original": clean_summary(subject.get("summary")),
    }


# ---------- 网络 ----------


def _api_get(path: str, *, proxy: str = "", timeout: float = 12.0,
             retries: int = 2, sleep=time.sleep):
    opener = persona._build_opener(proxy or settings.bangumi_proxy)
    base = settings.bangumi_meta_api_base or settings.bangumi_api_base
    url = f"{base.rstrip('/')}{path}"
    return persona._get_json(
        opener, url, settings.bangumi_user_agent, timeout, retries=retries, sleep=sleep
    )


def fetch_subject_meta(bangumi_id: int, *, proxy: str = "", timeout: float = 12.0,
                       retries: int = 2, sleep=time.sleep) -> dict:
    """拉一部作品的客观信息 + 制作人员 + 角色声优（2 次请求）。"""
    subject = _api_get(f"/v0/subjects/{bangumi_id}", proxy=proxy, timeout=timeout,
                       retries=retries, sleep=sleep)
    characters = _api_get(f"/v0/subjects/{bangumi_id}/characters", proxy=proxy,
                          timeout=timeout, retries=retries, sleep=sleep)
    persons = _api_get(f"/v0/subjects/{bangumi_id}/persons", proxy=proxy,
                       timeout=timeout, retries=retries, sleep=sleep)
    return {"subject": subject, "characters": characters, "persons": persons}


# ---------- 按需补全的调度判断（详情页只读库，不在这里发请求） ----------


def meta_is_stale(content: Content, *, now: datetime | None = None) -> bool:
    """这部作品现在需不需要补全（纯判断，不联网、不写库）。

    三个条件缺一不可：有 Bangumi 来源、没在失败冷却期内、以及没补过或超过 TTL。

    冷却为什么必须：``meta_fetched_at`` 只在成功时写入，失败时保持 NULL。
    只按它判断的话，Bangumi 不可达期间**每次**打开详情页都会重新发起请求；
    旧实现同步等待，线上实测每页卡 12.5 秒。失败时间单独记一列，冷却期内直接放弃。
    """
    if content.bangumi_id is None:
        return False
    now = now or datetime.now()
    if content.meta_failed_at is not None:
        cooldown = timedelta(hours=settings.bangumi_meta_retry_cooldown_hours)
        if now - content.meta_failed_at < cooldown:
            return False
    if content.meta_fetched_at is None:
        return True
    return (now - content.meta_fetched_at).days >= settings.bangumi_meta_ttl_days


def mark_meta_failed(db, content: Content, *, now: datetime | None = None) -> None:
    """记录一次补全失败，开启冷却窗口（成功补全会清掉这一列）。"""
    content.meta_failed_at = now or datetime.now()
    db.commit()


# ---------- 入库 ----------


def sync_content_meta(
    db,
    content: Content,
    *,
    proxy: str = "",
    timeout: float = 12.0,
    retries: int = 2,
    sleep=time.sleep,
    payload: dict | None = None,
) -> dict:
    """补全一部作品并写入；payload 可注入（测试 / 脚本复用已拉数据）。"""
    if content.bangumi_id is None:
        return {}
    raw = payload or fetch_subject_meta(
        int(content.bangumi_id), proxy=proxy, timeout=timeout, retries=retries, sleep=sleep
    )
    persons = raw.get("persons") or []
    meta = build_meta(
        raw.get("subject") or {},
        raw.get("characters") or [],
        persons,
    )

    if meta["media"]:
        content.media = meta["media"]
    if meta["source_material"]:
        content.source_material = meta["source_material"]
    if meta["episodes"]:
        content.episodes = meta["episodes"]
    if meta["year"] and not content.year:
        content.year = meta["year"]
    # 原文简介：只在为空、且确实与中文简介不同时写入。
    # 不覆盖已有值（可能是人工维护）；相同则跳过，否则详情页会出现两份一样的文字、
    # 语言切换入口也变得没有意义。
    original_summary = meta.get("summary_original") or ""
    if (
        original_summary
        and not (content.summary_original or "").strip()
        and original_summary != (content.summary or "").strip()
    ):
        content.summary_original = original_summary
    content.meta_fetched_at = content.meta_fetched_at or datetime.now()
    # 补全成功即清除失败标记：下一轮 TTL 到期前不再重复请求。
    content.meta_failed_at = None
    db.commit()

    crud.replace_content_studios(db, content.id, meta["studios"])
    # 主创补进现有实体体系；已有关联的作品不动（避免覆盖手工维护的关联）
    if not crud.has_content_entity_recognition(db, content.id):
        for item in meta["entity_roles"]:
            crud.add_content_entity_link(
                db, content.id, item["name"], item["role"], item["entity_type"]
            )
    crud.replace_content_credits(db, content.id, meta["credits"])
    crud.replace_content_cast(db, content.id, meta["cast"])
    crud.replace_content_facts(db, content.id, meta["facts"])

    # 本次补进来的候选可能刚好凑满 10 部，就地转正；其余等批量任务统一处理
    names = [item["name"] for item in meta["credits"] if item.get("tier") == "candidate"]
    if names:
        crud.promote_staff_candidates(
            db, names=names, threshold=LONG_TIER_MIN_WORKS
        )
    return meta
