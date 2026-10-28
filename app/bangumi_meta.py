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

# 首屏（main）：每部作品只有个位数的一把手职位。分镜 / 演出 / 作画监督
# 这类按集数换人的职位虽然在专业读者眼里很重要，但一部 TV 动辄几十条，
# 全塞首屏轨道会把「查看全部」撑成一堵墙——它们进折叠区（long）。
STAFF_MAIN_ROLES = frozenset(
    {
        "导演",
        "副导演",
        "系列构成",
        "脚本",
        "剧本",
        "原作",
        "原案",
        "人物原案",
        "人物设定",
        "总作画监督",
        "音乐",
        "美术监督",
        "色彩设计",
        "摄影监督",
        "剪辑",
        "音响监督",
        "主题歌演出",
        "制片人",
        "动画制片人",
        "制作人",
        "制作总指挥",
        "企画",
        "程序",
        "美工",
        "游戏设计师",
    }
)

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

# ---- Staff 表：infobox 里的全部人事键 ----
#
# 为什么要全收：infobox 才是 Bangumi 条目页真正展示的职员表（导演 / 系列构成 /
# 脚本 / 人物设定 / 音乐 / 美术监督 … 一直到录音助理），而 persons 接口只有
# 原画级别的长尾。旧实现只把 4 个「额外职位」写进 credits，核心主创被塞进
# 实体关系区，于是详情页 Staff 常年只剩两三条、甚至为空。
#
# 三个键集分工：
# - STAFF_COMPANY_KEYS：公司/组织，交给「制作」行与实体关系（person 之外）；
# - STAFF_SKIP_KEYS：客观事实（话数 / 放送 / 平台 / 售价 …），只进 content_facts；
# - 其余键一律视为人事职位，按 STAFF_ROLE_ORDER 的顺序落 content_credits。
STAFF_COMPANY_KEYS = frozenset(
    {
        "动画制作",
        "製作",
        "制作",
        "开发",
        "发行",
        "出版社",
        "音乐制作",
        "3DCG",
        "录音工作室",
        "摄影工作室",
        "动画工作室",
        "制作会社",
    }
)

STAFF_SKIP_KEYS = frozenset(
    {
        "中文名",
        "平台",
        "游戏类型",
        "游玩人数",
        "发行日期",
        "售价",
        "发售日期",
        "价格",
        "播放时长",
        "碟片数量",
        "website",
        "Website",
        "官方网站",
        "话数",
        "放送开始",
        "放送星期",
        "播放电视台",
        "其他电视台",
        "播放结束",
        "Copyright",
        "别名",
        "版本",
        "商品编号",
        "ASIN",
        "ISBN",
        "页数",
        "定价",
        "出版社",
        "又名",
    }
)

# 「原作」键既可能是人名（はまじあき），也可能只是载体（漫画 / 游戏 / 原创）。
# 后者由 contents.source_material 承载，不该在 Staff 表里出现一个叫「漫画」的人。
STAFF_NON_PERSON_VALUES = frozenset(
    {"漫画", "轻小说", "小说", "游戏", "原创", "动画", "同人", "画集", "音乐"}
)

# 展示顺序（参考 Bangumi 条目页信息卡 / MAL Staff 段）：核心主创在前，
# 制作执行类在后。未列出的职位按 infobox 原始顺序排在最后。
STAFF_ROLE_ORDER = (
    "导演",
    "副导演",
    "系列构成",
    "副系列构成",
    "脚本",
    "剧本",
    "分镜",
    "演出",
    "原作",
    "原案",
    "人物原案",
    "人物设定",
    "副人物设定",
    "总作画监督",
    "作画监督",
    "作画监督助理",
    "主动画师",
    "原画",
    "机械设定",
    "概念设计",
    "道具设计",
    "美术监督",
    "美术设计",
    "美术板",
    "色彩设计",
    "色彩设计助理",
    "摄影监督",
    "CG 导演",
    "剪辑",
    "音响监督",
    "音效",
    "音乐",
    "音乐制作人",
    "音乐监督",
    "主题歌演出",
    "插入歌演出",
    "主题歌作词",
    "主题歌作曲",
    "主题歌编曲",
    "OP・ED 分镜",
    "OP・ED 演出",
    "制片人",
    "动画制片人",
    "副制片人",
    "助理制片人",
    "制作人",
    "制作总指挥",
    "企画",
    "制作统括",
    "制作管理",
    "制作进行",
    "设定制作",
    "文艺制作",
    "宣传",
    "宣传制片人",
    "录音",
    "录音助理",
    "音响制作担当",
    "设定",
    "设定协力",
    "特效",
    "程序",
    "美工",
    "游戏设计师",
    "制作事务",
    "制作协力",
    "企画协力",
    "原作协力",
    "CG 制作进行",
    "制作管理助理",
)
STAFF_ROLE_RANK = {role: index for index, role in enumerate(STAFF_ROLE_ORDER)}

# 角色重要度：Bangumi characters 接口的 relation 字段。接口返回顺序不是
# 重要度序（旁白 / 闲角常常排在最前），必须自己排——否则主角被挤出首屏。
CAST_RELATION_RANK = {"主角": 0, "配角": 1, "客串": 2, "旁白": 3, "闲角": 4}
# 单部作品保存的配音关系上限：主角 + 主要配角足够，长尾留库但也别无限堆。
CAST_MAX_ROWS = 60

# 长尾职员转正门槛：在本站累计出现 ≥10 部作品才展示（数据驱动，不维护名单）。
LONG_TIER_MIN_WORKS = 10
# 原文简介入库上限（Bangumi 个别条目简介极长，避免把整页撑爆）
SUMMARY_MAX_LEN = 4000

# 客观信息里值得展示的键（其余 infobox 一律丢弃，避免「没人关心的信息占服务器」）。
# 「企画」不在此列：它是人名，归 Staff 表；「制作」是委员会（公司），留在补充资料。
FACT_KEYS = ("放送星期", "播放结束", "放送开始", "官方网站", "别名", "制作")

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


def _staff_tier(role: str) -> str:
    """职位分档：一把手的少数职位进首屏，其余（按集换人的、执行 / 助理类）进折叠区。"""
    return "main" if role in STAFF_MAIN_ROLES else "long"


def _merge_split_brackets(values: list[str]) -> list[str]:
    """把被分隔符切碎的括号项拼回去。

    infobox_map 按「、」切分值，但「主題歌演出 = 結束バンド (青山吉能、長谷川育美…)」
    这种括号里带顿号的会被切成四段，前两段括号还不闭合。这里按括号是否配平
    把相邻碎片合回一项，再做姓名清洗。
    """
    merged: list[str] = []
    buffer = ""
    for value in values:
        buffer = f"{buffer}、{value}" if buffer else value
        if buffer.count("(") == buffer.count(")") and buffer.count("（") == buffer.count("）"):
            merged.append(buffer)
            buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


def build_staff_rows(info: dict[str, list[str]]) -> list[dict]:
    """把 infobox 里全部人事键摊成 Staff 表行（纯函数，便于测试）。

    - 公司/组织键（动画制作 / 发行 / 开发 …）与客观事实键跳过，它们各有归属；
    - 已知职位按 STAFF_ROLE_ORDER 排序，未知职位保留 infobox 原序排在后面；
    - 同一 (职位, 人名) 只留一条——infobox 偶有同名键重复。
    """
    pairs: list[tuple[str, str]] = []
    for key, values in info.items():
        if key in STAFF_SKIP_KEYS or key in STAFF_COMPANY_KEYS or key.startswith("平台:"):
            continue
        for raw_name in _merge_split_brackets(values):
            name = _clean_name(raw_name)
            if not name or len(name) > 100:
                continue
            if key == "原作" and name in STAFF_NON_PERSON_VALUES:
                continue
            pairs.append((key, name))

    indexed = list(enumerate(pairs))
    indexed.sort(
        key=lambda pair: (STAFF_ROLE_RANK.get(pair[1][0], len(STAFF_ROLE_RANK)), pair[0])
    )

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for order, (_index, (role, name)) in enumerate(indexed):
        if (role, name) in seen:
            continue
        seen.add((role, name))
        rows.append(
            {
                "person_id": None,
                "role": role[:30],
                "name": name,
                "detail": None,
                "sort_order": order,
                "tier": _staff_tier(role),
            }
        )
    return rows


def build_cast_rows(characters: list | None) -> list[dict]:
    """角色-声优行：只取原版配音，按 Bangumi 的 relation（主角 > 配角 > …）排序。

    characters 接口的返回顺序**不是**重要度序：旁白 / 闲角常排在最前面，
    直接截前 N 条会把主角挤掉（实测「孤独摇滚」主角排在第 5 位之后）。
    """
    rows: list[dict] = []
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
        relation = str(character.get("relation") or "").strip()[:10]
        rows.append(
            {
                "character": character_name[:100],
                "character_id": character.get("id"),
                "person_id": primary.get("id"),
                "actor": actor[:100],
                "relation": relation,
                "sort_order": index,
                "_rank": CAST_RELATION_RANK.get(relation, 2),
                "_index": index,
            }
        )

    rows.sort(key=lambda item: (item["_rank"], item["_index"]))
    rows = rows[:CAST_MAX_ROWS]
    for order, row in enumerate(rows):
        row["sort_order"] = order
        row.pop("_rank", None)
        row.pop("_index", None)
    return rows


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

    # Staff 表：infobox 的全部人事键（核心主创 + 制作执行）
    credits: list[dict] = build_staff_rows(info)
    order = len(credits)

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

    cast = build_cast_rows(characters)

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
                       retries: int = 2, sleep=time.sleep, with_persons: bool = True) -> dict:
    """拉一部作品的客观信息 + 制作人员 + 角色声优（3 次请求）。

    ``with_persons=False`` 时跳过 /persons 长尾职员表：批量回填 Staff 主创时
    这一步只产候选（要累计 10 部作品才转正），省掉 1/3 的请求时间。
    """
    subject = _api_get(f"/v0/subjects/{bangumi_id}", proxy=proxy, timeout=timeout,
                       retries=retries, sleep=sleep)
    characters = _api_get(f"/v0/subjects/{bangumi_id}/characters", proxy=proxy,
                          timeout=timeout, retries=retries, sleep=sleep)
    persons: list = []
    if with_persons:
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
    with_persons: bool = True,
) -> dict:
    """补全一部作品并写入；payload 可注入（测试 / 脚本复用已拉数据）。"""
    if content.bangumi_id is None:
        return {}
    raw = payload or fetch_subject_meta(
        int(content.bangumi_id), proxy=proxy, timeout=timeout, retries=retries,
        sleep=sleep, with_persons=with_persons,
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
    # 主创补进现有实体体系；已有关联的作品不动（避免覆盖手工维护的关联）。
    # 注意这里只认「实体关联」本身：旧实现用 has_content_entity_recognition，
    # 它把「有同系列」也算作已识别，于是系列作品的导演 / 音乐永远不会入库。
    if crud.count_content_entity_links(db, content.id) == 0:
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
