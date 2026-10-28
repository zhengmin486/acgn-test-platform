"""多源资讯每日同步：按自然日软配额把新资讯灌进站内。

口径（用户定稿）：
- **一天 30 条软配额**：全站自然日新增上限，不分源；cron 每 4 小时跑一次也按当天累计。
  软配额：**核心内容优先、不强行填满、不为凑数量展示低价值资讯**，且限制单一主体类型
  挤占全部位置（综合动态有封顶）。
- **官宣豁免**（2026-10-22 用户定稿）：动画化 / 制作決定 / 续作決定 这类重大官宣走
  ``NEWS_EXEMPT_MAX_PER_DAY`` 独立额度，**不占**上面那条配额——否则当天配额被普通稿
  灌满后，最该有的官宣会因为 RSS feed 过期而永久丢失。判定见
  ``news_classify.detect_major_announcement``，落 ``Article.is_major`` 供按天统计额度。
- **分类 / 事件 / 优先级 三维度分离**（详见 scripts/news_classify）：
  主体类型（动画 / 漫画 / 轻小说 / 游戏 / 综合动态）回答「属于哪个 ACGN 主体」；
  事件类型回答「发生了什么」；priority_score 回答「该排多前」。
  音乐 / 创作者 / 业界动态不另立主体，按关联作品归入四类。
- **优先级**：日本动画 / 漫画 / 轻小说 与 Galgame 顶级优先；日本二游 = 国产二游 同权；
  日本其他游戏次之；国产非二游适当降权；欧美普通内容低优先级、重大事件可破格提升
  （不凭标题关键词随意判定）；官方 PR 明显压低（不进主 feed）。
- **硬广过滤**：只挡最露骨广告（scripts/news_filters）；营销活动（联动 / 周边 / 活动）
  保留；galge 不挡，成人内容只在展示层按全局开关隐藏。
- **多源**：中文源（机核 / 游民星空 ACG）直接入库；日文源需要自动翻译，
  没有可用 LLM Key 时整源跳过；
- **不碰作品表**：只写 articles（+ 标签关联），不导入 contents。

与旧单源导入（scripts/import_articles.py + Aniplex）的关系：
旧流程仍保留给管理端「官方资讯」手动同步；本模块是 cron 用的日常管线。
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
import time as _time  # 别名导入：本模块 from datetime import time 会占用 time 这个名字
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import article_media, article_tags, crud, news_llm  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import Article  # noqa: E402
from scripts import news_classify  # noqa: E402
from scripts import news_filters  # noqa: E402
from scripts.news_sources import (  # noqa: E402
    SOURCES,
    NewsCandidate,
    NewsSourceError,
    SourceSpec,
    extract_body,
    fetch_candidates,
    hydrate,
    hydrate_head_image,
    parse_og_image,
)

# ---------- 取数优先级 ----------
# 旧版「语言 + 题材」两维排序已废弃（见 scripts/news_classify）。现在按 priority_score
# 归集后再填每日软配额：priority_score 由 主体 + 游戏子类 + 地区 + 事件 + 来源类型 叠加，
# 日本二游 = 国产二游 同权、galge 最高、欧美普通受控、欧美重大可破格、官方 PR 折叠。
# 源的默认 category / region / source_type 来自 SourceSpec，逐条再按标题 / 摘要细化。


def _classify(candidate: NewsCandidate) -> dict:
    """对一条候选做完整分类，返回落库所需的各维度（三维度分离）。

    结果缓存在候选对象自身的 ``classification`` 字段：同一条候选在排序、软配额、落库
    阶段会被多次取用。（不可用全局 dict 按 id() 缓存——对象被回收后 id 会被复用，串号。）
    """
    cached = candidate.classification
    if cached is not None:
        return cached
    subject = news_classify.classify_subject_from_text(
        candidate.title, candidate.summary, fallback=candidate.source.category
    )
    region = news_classify.detect_region(
        candidate.source.region, candidate.title, candidate.summary
    )
    subcategory = news_classify.classify_subcategory(
        candidate.title, candidate.summary, subject
    )
    event_type = news_classify.classify_event_type(candidate.title, candidate.summary)
    source_type = candidate.source.source_type
    priority_score = news_classify.compute_priority_score(
        category=subject,
        subcategory=subcategory,
        region=region,
        event_type=event_type,
        source_type=source_type,
        text=f"{candidate.title}\n{candidate.summary}",
    )
    is_adult = news_classify.flag_adult(candidate.title, candidate.summary)
    major, major_hit = news_classify.detect_major_announcement(
        candidate.title, candidate.summary
    )
    result = {
        "category": subject,
        "subcategory": subcategory,
        "event_type": event_type,
        "region": region,
        "source_type": source_type,
        "priority_score": priority_score,
        "is_adult": is_adult,
        # 重大官宣（配额豁免）：强度 0=普通，>0 走独立豁免额度，不占每日配额。
        # major_hit 是命中的词，写进运行明细便于事后校验词表误伤。
        "major": major,
        "major_hit": major_hit,
    }
    candidate.classification = result
    return result


def _candidate_sort_key(candidate: NewsCandidate) -> tuple:
    """池内排序键：priority_score 降序、其次最新发布。"""
    published = candidate.published_at or datetime.min
    score = _classify(candidate)["priority_score"]
    return (score, published)


def _exempt_sort_key(candidate: NewsCandidate) -> tuple:
    """豁免池排序键：官宣强度降序，其次 priority_score、发布时间。

    豁免额度有限（默认 10 条/天），额度不够时先给最硬的官宣（アニメ化 > 续作 > 双信号）。
    """
    published = candidate.published_at or datetime.min
    info = _classify(candidate)
    return (info["major"], info["priority_score"], published)


# 主体类别的抓取 / 保底优先级（2026-10-23 用户定稿：动画 → 漫画 → 轻小说 → 游戏）。
# 不在表里的类别（综合动态等）排在这四类之后，官方 PR 源仍然垫底。
_CATEGORY_ORDER: tuple[str, ...] = (
    news_classify.SUBJECT_ANIME,
    news_classify.SUBJECT_MANGA,
    news_classify.SUBJECT_LN,
    news_classify.SUBJECT_GAME,
)


def _ordered_sources(
    sources: tuple[SourceSpec, ...], priority_keys: str | None = None
) -> list[SourceSpec]:
    """抓源顺序：**置顶源 → 类别优先级（动画 → 漫画 → 轻小说 → 游戏 → 其它）→ 官方 PR**。

    为什么不能按注册表顺序（2026-10-23）：原来是「按注册表顺序逐源抓 + 池子够数就
    break」，而三个纯游戏源（4Gamer / インサイド / GameSpark）排在 3~5 位、单个 feed
    就有 20~40 条 → 抓到第 5 个源池子就满了直接停，后面的动画 / 轻小说 / 中文源连请求
    都发不出去。线上结果：近 3 天游戏稿 60 条反超动画 47 条，而 priority 前 15 名其实
    全是动画官宣（排序没坏，是「先到先得」把配额喂给了游戏源）。

    ``priority_keys``（``NEWS_PRIORITY_SOURCES``，逗号分隔）里的源按书写顺序排到最前，
    2026-10-23 起默认 ``acgch`` 第一个抓——它是日本媒体的中文编译、覆盖四个类别、
    标题规范，先抓它得到的稿子质量最稳。

    其余源按类别优先级排（``_CATEGORY_ORDER``），同类内保持注册表顺序；官方 PR 源仍然
    垫底（不参与主 feed 配额竞争）。配合 `run_import_daily` 的类别保底与 break 前的
    欠额检查，既保证动画优先，也不会让后面的类别饿死。
    """
    if priority_keys is None:
        priority_keys = settings.news_priority_sources
    pr = [spec for spec in sources if spec.source_type == news_classify.SOURCE_TYPE_PR]
    rest = [spec for spec in sources if spec.source_type != news_classify.SOURCE_TYPE_PR]

    head: list[SourceSpec] = []
    seen_keys: set[str] = set()
    for key in (priority_keys or "").split(","):
        key = key.strip()
        if not key or key in seen_keys:
            continue
        for spec in rest:
            if spec.key == key:
                head.append(spec)
                seen_keys.add(key)
                break
    head_ids = {id(spec) for spec in head}
    tail = [spec for spec in rest if id(spec) not in head_ids]

    def _rank(spec: SourceSpec) -> int:
        try:
            return _CATEGORY_ORDER.index(spec.category)
        except ValueError:
            return len(_CATEGORY_ORDER)  # 综合动态等其它类别排在这四类之后

    return head + sorted(tail, key=_rank) + pr


def _has_pending_category_floor(
    upcoming: list[SourceSpec],
    pool: list[NewsCandidate],
    deficit: dict[str, int],
) -> bool:
    """后面还没抓的源里，是否还有「类别欠额未被池子满足」的情况。

    `run_import_daily` 的早退优化是「池子候选数够配额就停」。但动画源一个大 feed 就能
    喂满配额，直接停会让漫画 / 轻小说 / 游戏源一次请求都发不出去。这里在 break 前补一道
    检查：只要未抓的源里还有欠额类别、且池内该类候选不足欠额，就继续抓（多几次请求，
    换类别覆盖）。`deficit` 为空（关闭保底 / 当天最后一轮）时恒为 False，行为与旧版一致。
    """
    if not deficit:
        return False
    pool_counts: dict[str, int] = {}
    for candidate in pool:
        category = _classify(candidate)["category"]
        pool_counts[category] = pool_counts.get(category, 0) + 1
    return any(
        deficit.get(spec.category, 0) > pool_counts.get(spec.category, 0)
        for spec in upcoming
    )


# 单个源在「本轮候选池」里最多贡献多少（占本轮额度的比例）。
# 0.34 ≈ 至少 3 个源才能填满一轮，防止某个大 feed 一个人灌满池子。
# 只在启用源 ≥ 3 时生效：单源 / 双源场景（手工 --source 补量、测试）没有别的源可用，
# 再封顶只会让配额填不满。只约束入池条数，不改 priority_score，也不影响官宣豁免扫描。
_PER_SOURCE_POOL_RATIO = 0.34
# 封顶生效所需的最少源数量：少于这个数就不封顶（没有别的源可选）。
_PER_SOURCE_CAP_MIN_SOURCES = 3
# 无限制源（SourceSpec.unrestricted，2026-10-25 用户定稿 acgch 全抓）的列表抓取条数。
# 其它源的 limit 由剩余配额推导；无限制源不看配额，RSS 恒 50 条，取 200 留余量。
_UNRESTRICTED_FETCH_LIMIT = 200

# RSS 正文补全阈值（2026-10-23）：feed 的 description 短于这个长度就视为「只有导语」，
# 去抓一次详情页补正文。アニメ！アニメ！ ≈200 字、リスアニ！ ≈50 字，都在此线以下；
# 已经带全篇正文的 feed（> 阈值）不额外请求。
_RSS_FEED_BODY_MIN = 240
# 详情页提取结果至少要比现有正文长这么多才替换，避免两版长度抖动导致来回覆盖。
_RSS_BODY_GAIN_MIN = 40
# 详情页之间的抓取间隔（秒）。2026-10-25 之前是 0——一轮 7 源 × 40~50 篇详情
# 瞬间连续发出，风控一开就是批量 429/连接重置，表现为「整源抓不到」。
# 只对**发出去的请求**计间隔（feed 自带全文时不抓详情、不 sleep）。
DETAIL_SLEEP_SECONDS = 1.0


# 同一主体类型在每日配额里最多占多少条，避免单一类别挤占全部位置（兼顾类别覆盖）。
# 2026-10-21 用户定稿「综合可以和游戏同权」：综合动态的封顶取消，与动画 / 漫画 /
# 轻小说 / 游戏同级竞争配额；机制保留（要重新封顶只改这张表）。
_PER_SUBJECT_CAP: dict[str, int] = {}

# 同一事件的重复报道判定阈值：标题字符二元组 Jaccard 相似度超过该值视为同一事件，
# 只保留优先级更高的那条（避免同一条新闻占掉多个位置）。
_EVENT_DUP_THRESHOLD = 0.62
_PUNCT_RE = re.compile(r"[\s　\"'“”‘’《》〈〉「」『』()（）\[\]【】|｜:：,，。.、!！?？~～\-—_/\\]+")


def _event_fingerprint(title: str) -> set[str]:
    """标题的字符二元组集合，用于判定「同一事件的不同报道」。"""
    normalized = _PUNCT_RE.sub("", (title or "").lower())
    if len(normalized) < 2:
        return {normalized}
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def _is_same_event(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    union = len(a | b)
    if union == 0:
        return False
    return len(a & b) / union >= _EVENT_DUP_THRESHOLD


def select_quota(
    pool: list[NewsCandidate],
    remaining: int,
    exempt_remaining: int = 0,
    category_floor: dict[str, int] | None = None,
) -> tuple[list[NewsCandidate], int]:
    """软配额挑选：官宣走豁免额度 → 类别保底 → 其余按 priority_score 填满。

    返回 (选中的候选, 因同事件重复被合并掉的条数)。不足额度时不强行填满。

    - 豁免条目（major > 0）**不消耗** ``remaining``，只消耗 ``exempt_remaining``，
      且不受 ``_PER_SUBJECT_CAP`` 类别封顶限制——官宣是"必须要有"，不是"多给一格"；
    - 豁免额度用完（或配置为 0）时官宣**回落到普通配额**继续竞争，不是直接丢弃：
      关掉豁免只该让它"跟普通稿一样排队"，而它有 +60 加成，实际仍排最前；
    - **类别保底**（2026-10-23）：``category_floor`` 是 ``{类别: 还缺几条}``。先按类别
      各取够欠额（类别内部仍按 priority_score 取最优），剩余的 ``remaining`` 再全局按权重
      填满。这样即便动画源一次能喂满配额，漫画 / 轻小说 / 游戏也不会一条都进不来；
      某类别池里实在不够时，它的空位会被第二阶段按正常权重补给别人（不浪费额度）。
    - 同事件去重跨两个池生效，且豁免条目先入场（三家报道同一条「制作決定」时，
      留下的是官宣强度 / 优先级更高的那条）。
    """
    ordered = sorted(pool, key=_candidate_sort_key, reverse=True)
    exempt_pool = sorted(
        (c for c in ordered if _classify(c)["major"]), key=_exempt_sort_key, reverse=True
    )
    normal_pool = [c for c in ordered if not _classify(c)["major"]]
    selected: list[NewsCandidate] = []
    chosen: set[int] = set()  # id() 去重：候选对象在本函数内不会被替换
    per_subject: dict[str, int] = {}
    fingerprints: list[set[str]] = []
    duplicates = 0
    exempt_used = 0
    normal_used = 0

    def _take(candidate: NewsCandidate, subject: str) -> bool:
        """同事件去重后收下这条候选；重复时返回 False（由调用方决定是否计数）。"""
        fingerprint = _event_fingerprint(candidate.title)
        if any(_is_same_event(fingerprint, seen) for seen in fingerprints):
            return False
        fingerprints.append(fingerprint)
        per_subject[subject] = per_subject.get(subject, 0) + 1
        chosen.add(id(candidate))
        selected.append(candidate)
        return True

    for candidate in exempt_pool:
        if exempt_used >= exempt_remaining and normal_used >= remaining:
            break  # 两条预算都满了
        subject = _classify(candidate)["category"]
        if not _take(candidate, subject):
            duplicates += 1
            continue
        if exempt_used < exempt_remaining:
            exempt_used += 1
        else:
            normal_used += 1  # 豁免额度用完 → 回落占普通配额

    # 阶段一：类别保底（只针对普通稿；官宣已入场，不重复占用保底名额）。
    for subject, need in (category_floor or {}).items():
        if need <= 0:
            continue
        taken = 0
        for candidate in normal_pool:
            if taken >= need or normal_used >= remaining:
                break
            if id(candidate) in chosen:
                continue
            if _classify(candidate)["category"] != subject:
                continue
            cap = _PER_SUBJECT_CAP.get(subject)
            if cap is not None and per_subject.get(subject, 0) >= cap:
                continue
            if _take(candidate, subject):
                taken += 1
                normal_used += 1
            # 同事件重复不在这里计数：第二阶段还会再判一次，避免重复计入统计。

    # 阶段二：剩余额度全局按 priority_score 填满（某类保底不够时，空位在这里被别人补上）。
    for candidate in normal_pool:
        if normal_used >= remaining:
            break
        if id(candidate) in chosen:
            continue
        subject = _classify(candidate)["category"]
        cap = _PER_SUBJECT_CAP.get(subject)
        if cap is not None and per_subject.get(subject, 0) >= cap:
            continue
        if not _take(candidate, subject):
            duplicates += 1
            continue
        normal_used += 1
    return selected, duplicates


DETAILS_MAX = 40  # 明细最多回传多少条（与 sync_tasks 的风格一致）


@dataclass
class DailyNewsSummary:
    """一次每日同步的汇总；字段名贴近 sync_tasks.TaskResult，便于映射。"""

    mode: str  # apply / preview
    quota: int
    run_quota: int = 0  # 单轮上限（0 = 不限制），实际本轮额度 = min(日剩余, run_quota)
    exempt_quota: int = 0  # 重大官宣豁免额度（条/天），不占 quota
    category_floor: int = 0  # 每个主体类型每天的保底条数（0 = 关闭）
    final_run: bool = False  # 是否当天最后一轮（不再为类别保底预留额度）
    category_deficit: dict[str, int] = field(default_factory=dict)  # 本轮为哪些类别预留了几条
    per_category: dict[str, int] = field(default_factory=dict)  # 本轮各类别入库条数
    added_before: int = 0  # 当天此前已入库
    exempted_before: int = 0  # 当天此前已用掉的豁免额度
    exempted: int = 0  # 本轮走豁免额度入库的官宣条数
    requested: int = 0  # 本轮抓到的候选数
    existing: int = 0  # 库中已有（URL 或标题命中）
    added: int = 0
    failed: int = 0
    # 详情页（正文/头图补全）抓取失败条数。2026-10-25 起单独计数：以前这类失败
    # 静默退化成导语，运维看不到；但**不并入 failed**——failed 的既有语义是
    # 「源级抓取失败」（cron 告警与管理端展示都用它），混入会让一个坏源显示成
    # 几十条失败。per_source_stats 里也有每源的「正文详情页失败 N」。
    detail_failed: int = 0
    skipped_sources: int = 0  # 因缺翻译能力跳过的日文源（保留字段，当前策略下为 0）
    untranslated_sources: int = 0  # 无翻译能力、按日文原文入库的日文源个数
    translate_failed: int = 0  # 翻译失败退回原文的条数
    filtered: int = 0  # 被硬广过滤掉（不入主 feed）的条数
    reviewed: int = 0  # 低置信度、照常入库但待人工复核的条数
    deduped: int = 0  # 同事件重复报道被合并掉的条数
    per_source: dict[str, int] = field(default_factory=dict)
    # 每源链路统计（候选/新增/过滤/失败）：cron 日志靠它定位「某源零产出」——
    # 2026-10-21 教训：5 个源整轮零产出且失败计数无处可见，排查无从下手。
    per_source_stats: dict[str, str] = field(default_factory=dict)
    details: list[dict] = field(default_factory=list)
    message: str = ""

    @property
    def pending(self) -> int:
        return 0 if self.mode == "apply" else self.added

    def describe(self) -> str:
        mode_label = "正式导入（--apply）" if self.mode == "apply" else "预览（dry-run）"
        if self.category_floor <= 0:
            floor_label = "类别保底：关闭"
        elif self.final_run:
            floor_label = (
                f"类别保底：每类每天 {self.category_floor} 条"
                "（当天最后一轮：不再预留，剩余额度按正常权重补满）"
            )
        elif self.category_deficit:
            detail = "；".join(
                f"{category} {count} 条" for category, count in self.category_deficit.items()
            )
            floor_label = f"类别保底：每类每天 {self.category_floor} 条（本轮预留：{detail}）"
        else:
            floor_label = (
                f"类别保底：每类每天 {self.category_floor} 条（今日各类均已达标，不再预留）"
            )
        lines = [
            f"模式：{mode_label}",
            f"今日配额：{self.quota} 条（已入库 {self.added_before} 条，软配额不强行填满）",
            f"单轮上限：{self.run_quota if self.run_quota > 0 else '不限'} 条（本轮实际按剩余额度抓）",
            f"官宣豁免额度：{self.exempt_quota} 条（此前已用 {self.exempted_before} 条，"
            f"本轮豁免 {self.exempted} 条，不占上面配额）",
            floor_label,
            f"本轮候选：{self.requested} 条",
            f"已存在跳过：{self.existing} 条",
            f"硬广过滤：{self.filtered} 条",
            f"同事件合并：{self.deduped} 条",
            f"新增：{self.added} 条",
            f"待人工复核（低置信度，已入库）：{self.reviewed} 条",
            f"跳过的日文源：{self.skipped_sources} 个",
        ]
        if self.untranslated_sources:
            lines.append(f"日文源按原文入库（未配置翻译 Key）：{self.untranslated_sources} 个")
        if self.translate_failed:
            lines.append(f"翻译失败（保留原文）：{self.translate_failed} 条")
        if self.per_source:
            lines.append(
                "分源：" + "；".join(f"{key} {count}" for key, count in self.per_source.items())
            )
        if self.per_category:
            lines.append(
                "分类型："
                + "；".join(f"{key} {count}" for key, count in self.per_category.items())
            )
        if self.per_source_stats:
            lines.append("分源链路：")
            for key, stat in self.per_source_stats.items():
                lines.append(f"  - {key}: {stat}")
        return "\n".join(lines)


def _today_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min)


def count_added_today(db, now: datetime) -> int:
    """当天已入库的资讯条数（自然日，按 created_at）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Article)
            .where(Article.created_at >= _today_start(now))
        )
        or 0
    )


def count_added_today_by_category(db, now: datetime) -> dict[str, int]:
    """当天已入库资讯按主体类型的条数分布（自然日，按 created_at）。

    类别来自 `Article.category`（入库时由 news_classify 判定），用于算「今天这个类别
    还差几条到保底」。cron 每轮是独立进程，所以只能查库，不能靠内存计数。
    """
    rows = db.execute(
        select(Article.category, func.count())
        .where(Article.created_at >= _today_start(now))
        .group_by(Article.category)
    ).all()
    return {str(category or ""): int(count) for category, count in rows}


def count_exempted_today(db, now: datetime) -> int:
    """当天已用掉的官宣豁免额度（自然日，按 created_at + is_major）。

    豁免额度按**天**计，而 cron 每轮是独立进程，所以必须查库——靠内存计数的话
    每轮都会重新计满，一天 3 轮就等于上限翻三倍。

    注：豁免额度用完后「回落占普通配额」的官宣同样带 is_major，也会被这里数进去——
    它们照样是官宣，额度封顶对它们照样有效（否则第 11 条之后还能继续豁免，等于没有防刷）。
    """
    return int(
        db.scalar(
            select(func.count())
            .select_from(Article)
            .where(
                Article.created_at >= _today_start(now),
                Article.is_major.is_(True),
            )
        )
        or 0
    )


def _media_key(url: str) -> str:
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:16]


def _strip_placeholder_lines(text: str) -> str:
    """剥掉 ``[图]``/``[媒体]`` 占位行，只留可读文本。

    用途：desc 为空的源（アニメ！アニメ！ / LN News）在详情页提取后才回填
    summary，而正文开头常常紧跟几张图——不剥的话列表卡片摘要以一串 URL 开头
    （2026-10-25 修）。
    """
    return "\n".join(
        line
        for line in (text or "").splitlines()
        if not line.startswith("[图]") and not line.startswith("[媒体]")
    ).strip()


def _build_body(candidate: NewsCandidate, *, translated: bool) -> str:
    """拼正文：中文源用摘要正文；已翻译的日文源用译文摘要。

    **不往正文里写「原文链接：URL」**：详情页模板已经有一个「原文链接 ↗」入口，
    正文再塞一条长 URL 会撑破排版（还会和正文段落混在一起，看着像乱码）。
    translated=False 时也不加「译文由自动翻译生成」尾注——无翻译 Key 的日文源
    是按原文入库的，加这句会误导读者。
    """
    body = (candidate.body or candidate.summary or "").strip()
    if not body:
        body = candidate.title
    if translated:
        body = f"{body}\n（译文由自动翻译生成）"
    return body


def _translate_candidates(
    candidates: list[NewsCandidate], *, translate
) -> list[NewsCandidate]:
    """就地翻译标题与摘要，并把 candidate.translated 置真（正文尾注据此判断）。

    翻译失败的条目被丢弃：有翻译能力时仍按「日文必须翻译后才入库」处理，
    避免同一站里中英日混排。
    """
    if not candidates:
        return []
    texts: list[str] = []
    for candidate in candidates:
        texts.append(candidate.title)
        texts.append(candidate.summary or candidate.title)
    result = translate(texts)
    if result is None:
        return []
    kept: list[NewsCandidate] = []
    for index, candidate in enumerate(candidates):
        title = result[index * 2]
        summary = result[index * 2 + 1]
        if not title:
            continue  # 译文缺失按失败处理，不写日文原文进站
        candidate.title = title[:200]
        candidate.summary = (summary or candidate.summary)[:200]
        candidate.translated = True
        kept.append(candidate)
    return kept


def _load_game_ip_aliases() -> list[str]:
    """读人工 IP 补丁表（``NEWS_GAME_WHITELIST_FILE``）：每行 ``主名 | 别名 | ...``。

    文件不存在时返回空列表（只靠站内数据做白名单，不报错）。相对路径按项目根目录解析
    ——cron 是从项目目录启动的，但手动从别处运行时也要能找到。
    """
    raw_path = Path(settings.news_game_whitelist_file)
    path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
    if not path.exists():
        return []
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.extend(part.strip() for part in line.split("|") if part.strip())
    return names


def run_import_daily(
    session_factory,
    *,
    apply: bool = False,
    quota: int | None = None,
    run_quota: int | None = None,
    exempt_quota: int | None = None,
    category_floor: int | None = None,
    sources: tuple[SourceSpec, ...] | None = None,
    fetch=None,
    translate=None,
    sleep=None,
    now: datetime | None = None,
) -> DailyNewsSummary:
    """按自然日配额抓取多源资讯并入库。

    fetch：``fetch(url) -> text``，测试注入假网络；translate：``translate(list[str])``，
    传 None 时按 LLM 配置决定（没有 Key 就跳过日文源）；sleep：``sleep(seconds)``，
    测试传 no-op，默认 ``time.sleep``（详情页之间的限速间隔）。

    四条预算并行：
    - ``quota``：普通资讯的每日上限（``NEWS_DAILY_QUOTA``）；
    - ``run_quota``：单轮上限（``NEWS_RUN_QUOTA``，0 = 不限），本轮额度取
      ``min(当日剩余, run_quota)``——避免一天的第一轮就把额度吃光；
    - ``category_floor``：**类别保底**（``NEWS_CATEGORY_FLOOR``，0 = 关闭）。按
      「动画 → 漫画 → 轻小说 → 游戏」的顺序，从本轮额度里为每类预留到「今天够
      floor 条」为止；额度不足以覆盖全部保底时，先满足顺序靠前的类别。保底只保证
      「有那么几条」，各类内部仍按 priority_score 取最优。
      当天**最后一轮**（``NEWS_FINAL_RUN_HOUR`` 起，现为 16 点那轮）不再预留、
      也不为欠额额外扫源：剩余额度全部按正常权重补上，避免为凑保底空转。
    - ``exempt_quota``：重大官宣（动画化 / 制作決定 / 続作決定）的豁免额度
      （``NEWS_EXEMPT_MAX_PER_DAY``），**不占**普通配额。
      普通配额跑满后本轮不再早退，而是继续做一次「豁免扫描」：只抓各源列表，
      命中官宣词才抓详情页入库，因此成本约为每源一次列表请求。

    **无限制源**（``SourceSpec.unrestricted``，2026-10-25 用户定稿 acgch「没有任何
    限制包括配额」）完全在四条预算之外：不过滤（news_filters 一层都不走）、不占
    配额、不受单源入池上限与类别封顶、不在同事件去重里跟别的源竞争，列表全量拉、
    有多少新条目就全抓。只保留按 URL / 标题的存量去重（幂等）。无限制源的候选也不
    参与「凑够就停」的池子计数——否则排第一的源会把池子撑满、饿死后面的源。
    """
    from scripts import import_articles as legacy  # 复用同一套抓取实现与 UA

    clock = now or datetime.now()
    limit_total = quota if quota is not None else settings.news_daily_quota
    limit_run = run_quota if run_quota is not None else settings.news_run_quota
    limit_exempt = (
        exempt_quota if exempt_quota is not None else settings.news_exempt_max_per_day
    )
    limit_floor = (
        category_floor if category_floor is not None else settings.news_category_floor
    )
    summary = DailyNewsSummary(
        mode="apply" if apply else "preview",
        quota=limit_total,
        run_quota=limit_run,
        exempt_quota=limit_exempt,
        category_floor=limit_floor,
    )
    fetch = fetch or legacy.fetch_page
    # 详情页限速：包一层 fetch，每个**成功发出的请求**之后按 DETAIL_SLEEP_SECONDS
    # 间隔休息（feed 自带全文时不抓详情，自然也不 sleep）。测试传 sleep=no-op。
    rest = sleep or _time.sleep
    detail_sleep = DETAIL_SLEEP_SECONDS

    def paced_fetch(url: str) -> str:
        text = fetch(url)
        if detail_sleep > 0:
            rest(detail_sleep)
        return text

    can_translate = translate is not None or news_llm.translate_available()
    if translate is None and can_translate:
        translate = news_llm.translate_texts

    db = session_factory()
    pending: list[Article] = []  # 本轮新写入的文章，收尾统一做标签识别
    # 本轮已入池稿件的摘要指纹（跨源累计，见上面「复制粘贴通稿」说明）
    seen_summary_prefixes: set[str] = set()
    try:
        # 游戏准入闸门：只对「泛游戏源」（NEWS_GAME_FILTER_SOURCES）生效，
        # 词表每轮加载一次（作品名/别名/系列/角色/创作者，见 crud.list_acgn_game_terms）。
        game_filter_sources = {
            key.strip()
            for key in settings.news_game_filter_sources.split(",")
            if key.strip()
        }
        # 词表除了游戏闸门，也供 should_skip_article 的「普通游戏的联名周边」
        # 规则做 ACG 证据（2026-10-25），所以不再看闸门开关，每轮都加载一次
        # （实测 5583 条 0.14s，每轮一次，可忽略）。
        game_terms: list[str] = crud.list_acgn_game_terms(
            db, manual_aliases=_load_game_ip_aliases()
        )
        summary.added_before = count_added_today(db, clock)
        remaining = max(0, limit_total - summary.added_before)
        # 单轮闸门：即便当天还剩 100 条，本轮也只吃 run_quota 条（0 = 不限）。
        if limit_run > 0:
            remaining = min(remaining, limit_run)
        summary.exempted_before = count_exempted_today(db, clock)
        exempt_remaining = max(0, limit_exempt - summary.exempted_before)

        # 类别保底：今天每类还差几条，就按「动画 → 漫画 → 轻小说 → 游戏」的顺序
        # 从本轮额度里预留几条。额度覆盖不了全部保底时先满足顺序靠前的类别。
        # 当天最后一轮不再预留（也不再为欠额额外扫源）：剩余额度全部按正常权重补上。
        summary.final_run = clock.hour >= settings.news_final_run_hour
        category_deficit: dict[str, int] = {}
        if limit_floor > 0 and remaining > 0 and not summary.final_run:
            added_by_category = count_added_today_by_category(db, clock)
            budget = remaining
            for category in _CATEGORY_ORDER:
                need = max(0, limit_floor - added_by_category.get(category, 0))
                need = min(need, budget)
                if need > 0:
                    category_deficit[category] = need
                    budget -= need
        summary.category_deficit = category_deficit

        # 归集所有源的候选，再统一按 priority_score 填软配额。
        # 官方 PR 源排在最后；凑够配额后不再抓取低优先级源（尤其游民星空详情页）。
        ordered_sources = _ordered_sources(sources or SOURCES)
        # 停用开关（NEWS_DISABLED_SOURCES，逗号分隔 key）：不改代码即可下线一个源。
        disabled = {
            key.strip() for key in settings.news_disabled_sources.split(",") if key.strip()
        }
        if disabled:
            ordered_sources = [spec for spec in ordered_sources if spec.key not in disabled]
        # 无限制源不受配额约束：哪怕今日配额已满，也照样把它抓完（2026-10-25）。
        # 因此「配额满 → 本轮不再抓取」的早退在有无限制源时不成立。
        has_unrestricted = any(spec.unrestricted for spec in ordered_sources)
        if remaining <= 0 and exempt_remaining <= 0 and not has_unrestricted:
            if limit_run > 0 and summary.added_before < limit_total:
                summary.message = (
                    f"今日已入库 {summary.added_before} 条（日上限 {limit_total} 条），"
                    f"但本轮额度 {limit_run} 条已用尽，等下一轮继续"
                )
            else:
                summary.message = (
                    f"今日已入库 {summary.added_before} 条，达到每日上限 {limit_total} 条，本轮不再抓取"
                )
            return summary

        # 单源入池上限：至少 3 个源才凑得满一轮，防止一个 20~40 条的大 feed 独吞候选池。
        # 只有 1~2 个源可用时（手工 --source 补量）不封顶，否则配额根本填不满。
        needed_total = remaining if remaining > 0 else exempt_remaining
        if len(ordered_sources) >= _PER_SOURCE_CAP_MIN_SOURCES:
            source_cap = max(1, math.ceil(needed_total * _PER_SOURCE_POOL_RATIO))
        else:
            source_cap = -1  # 负数 = 不封顶
        pool: list[NewsCandidate] = []
        for index, source in enumerate(ordered_sources):
            # 无限制源即使配额已满也要抓（它的候选不占配额，见下面的绕过点）；
            # 其余源保持「只捞官宣」模式，配额满就 break。
            if remaining <= 0 and exempt_remaining <= 0 and not source.unrestricted:
                break
            # 没有翻译能力时不再整源跳过（用户 2026-10-21 决定：宁可上日文原文，
            # 也不要空着）。LLM 翻译接口保留——配好 NEWS_LLM_API_KEY 后自动改回
            # 「先翻译再入库」，无需改代码。
            untranslated_ja = source.lang == "ja" and not can_translate
            if untranslated_ja:
                summary.untranslated_sources += 1
            try:
                candidates = fetch_candidates(
                    source, fetch=fetch,
                    # 无限制源全量拉列表（不看配额）；其余源按剩余额度的两倍拉，留挑选余地。
                    limit=_UNRESTRICTED_FETCH_LIMIT if source.unrestricted
                    else max((remaining + exempt_remaining) * 2, 10),
                )
            except (NewsSourceError, RuntimeError, OSError, TimeoutError) as exc:
                # RuntimeError 覆盖旧导入器的 ArticleFetchError（也是 RuntimeError 子类）
                summary.failed += 1
                summary.per_source_stats[source.key] = f"抓取失败：{str(exc)[:70]}"
                summary.details.append(
                    {"status": "failed", "title": f"{source.label} 抓取失败", "reason": str(exc)[:160]}
                )
                continue

            summary.requested += len(candidates)
            fresh = _filter_new(db, candidates)
            summary.existing += len(candidates) - len(fresh)
            source_filtered = 0
            detail_failures = 0  # 本源自提取正文的详情页失败数（源尾统计用）
            hydrated: list[NewsCandidate] = []
            normal_hydrated = 0  # 本源的普通稿入池数（官宣不计，它走豁免额度）
            for candidate in fresh:
                is_major_candidate = news_classify.is_major_announcement(
                    candidate.title, candidate.summary
                )
                # 普通配额已满：非官宣直接丢，连详情页都不抓——豁免扫描的成本
                # 因此只有「每源一次列表请求」，跟配额未满时的全量抓取不是一个量级。
                # 无限制源例外：它的候选不占配额，配额满不满都要抓（2026-10-25）。
                if remaining <= 0 and not is_major_candidate and not source.unrestricted:
                    continue
                # 单源入池上限（source_cap < 0 表示本轮不封顶，见上面的说明）。
                # 无限制源同样不受此限——用户要的就是「这个源全抓」。
                if (
                    source_cap >= 0
                    and not is_major_candidate
                    and not source.unrestricted
                    and normal_hydrated >= source_cap
                ):
                    continue
                try:
                    hydrate(candidate, fetch=paced_fetch)
                except (NewsSourceError, RuntimeError, OSError, TimeoutError) as exc:
                    summary.failed += 1
                    summary.details.append(
                        {"status": "failed", "title": candidate.title, "reason": f"详情页失败：{exc}"[:160]}
                    )
                    continue
                # RSS 源正文提取：feed 的 description 常常就是 1~2 句导语
                # （アニメ！アニメ！ ≈200 字、リスアニ！ ≈50 字），**不能当正文**。
                # 2026-10-23 修的坑：原来只在 `not candidate.body` 时才抓详情页，而
                # parse_feed 已经把 description 塞进 body → 条件几乎永不成立，
                # 详情页从不请求，文章详情就只有那一两句（用户报「没有文章详情」）。
                # 现在只要正文短于阈值就抓一次，提取结果明显更长才替换（幂等、不缩水）。
                # 2026-10-25：extract_body 传 candidate.url 作 base_url——详情页里的
                # 相对图片路径按源站域名补全（旧版补到 Aniplex 域名，正文一片裂图）；
                # 详情页抓取失败现在计入 failed 并留原因，不再静默退化成导语。
                detail_html = ""
                feed_body = (candidate.body or "").strip()
                if source.kind == "rss" and len(feed_body) < _RSS_FEED_BODY_MIN:
                    try:
                        detail_html = paced_fetch(candidate.url)
                    except (NewsSourceError, RuntimeError, OSError, TimeoutError) as exc:
                        # 2026-10-25：以前这里静默 `detail_html = ""`，网络失败与
                        # 「页面里没有正文」输出相同，运维无从判断是哪一环断了。
                        summary.detail_failed += 1
                        detail_failures += 1
                        summary.details.append(
                            {
                                "status": "failed",
                                "title": candidate.title,
                                "reason": f"正文详情页失败：{str(exc)[:120]}",
                            }
                        )
                        detail_html = ""
                    extracted = extract_body(detail_html, candidate.url) if detail_html else ""
                    if len(extracted) > len(feed_body) + _RSS_BODY_GAIN_MIN:
                        candidate.body = extracted
                    elif not feed_body:
                        candidate.body = ""  # 无摘要又提不出正文：留空，别拿噪音充数
                if not candidate.summary:
                    # desc 为空的源（アニメ！アニメ！ / LN News）到这里才有正文可截。
                    # 先剥掉 [图]/[媒体] 占位行——否则列表卡片摘要会以一堆 URL 开头。
                    candidate.summary = _strip_placeholder_lines(candidate.body or "")[:200]
                # RSS 源（4Gamer / アニメ！アニメ！）的 feed 不带图，列表会全空；
                # 缺图时优先复用已抓的详情页读 og:image，否则再单独抓一次。
                if not candidate.image_url and source.kind == "rss":
                    if detail_html:
                        found = parse_og_image(detail_html)
                        if found:
                            candidate.image_url = found
                    else:
                        hydrate_head_image(candidate, fetch=paced_fetch)
                # 收录判定（news_filters 三层过滤）：硬广 / 非 ACGN 主体 /
                # VOD 导流 / 抽奖 / 商业促销。不再看来源白名单，也不看来源类型。
                # 分类先算一次（缓存在候选对象上，后面的游戏闸门与落库复用）：
                # 「综合放行分组」（日音 / 日偶 / VTuber）只在 category 为综合动态时生效，
                # 不先分类就只能对全部类别放行，那就不是「综合资讯放行」了。
                candidate_info = _classify(candidate)
                if not source.unrestricted:
                    # 收录判定（news_filters 三层过滤）：硬广 / 非 ACGN 主体 /
                    # VOD 导流 / 抽奖 / 商业促销。不再看来源白名单，也不看来源类型。
                    # 分类先算一次（缓存在候选对象上，后面的游戏闸门与落库复用）：
                    # 「综合放行分组」（日音 / 日偶 / VTuber）只在 category 为综合动态时生效，
                    # 不先分类就只能对全部类别放行，那就不是「综合资讯放行」了。
                    skip_reason = news_filters.should_skip_article(
                        candidate.title, candidate.summary, candidate_info["category"],
                        source_type=candidate_info["source_type"],
                        url=candidate.url,
                        source_key=source.key,
                        game_terms=game_terms,
                    )
                    if skip_reason:
                        summary.filtered += 1
                        source_filtered += 1
                        summary.details.append(
                            {
                                "status": "filtered",
                                "title": candidate.title,
                                "reason": skip_reason[:160],
                            }
                        )
                        continue
                    # 复制粘贴的通稿引言（2026-10-24）：参展 / 联名通稿自带同一段活动引言，
                    # 一批里能把整个源占掉一大半（机核 feed 20 条里 8 条摘要同是核聚变展会词）。
                    # 放在内容过滤**之后**——先被硬广 / 促销拒掉的稿件不该占用指纹，
                    # 否则一条广告通稿就能把后面同引言的正经稿全挡掉。
                    prefix_key = news_filters.summary_prefix_key(candidate.summary)
                    if (
                        len(prefix_key) >= news_filters.REPEAT_SUMMARY_MIN_LEN
                        and prefix_key in seen_summary_prefixes
                    ):
                        summary.filtered += 1
                        source_filtered += 1
                        summary.details.append(
                            {
                                "status": "filtered",
                                "title": candidate.title,
                                "reason": "复制粘贴通稿（同批摘要开头重复）",
                            }
                        )
                        continue
                    # 游戏类准入闸门（2026-10-23）：只对泛游戏源的**游戏类**稿子生效。
                    # 动画 / 漫画 / 轻小说稿与其它源完全不受影响——这是「不改变其他类别
                    # 既有准入逻辑」的实现方式。拒绝计入 filtered，降分改的是分类缓存里的
                    # priority_score（后续 select_quota 与落库都读同一份，不用另算）。
                    if source.key in game_filter_sources:
                        game_info = candidate_info
                        if game_info["category"] == news_classify.SUBJECT_GAME:
                            verdict = news_filters.judge_game_admission(
                                candidate.title,
                                candidate.summary,
                                subcategory=game_info["subcategory"],
                                terms=game_terms,
                            )
                            if not verdict.allowed:
                                summary.filtered += 1
                                source_filtered += 1
                                summary.details.append(
                                    {
                                        "status": "filtered",
                                        "title": candidate.title,
                                        "reason": f"游戏准入：{verdict.reason}"[:160],
                                    }
                                )
                                continue
                            if verdict.penalty:
                                game_info["priority_score"] += verdict.penalty
                                summary.details.append(
                                    {
                                        "status": "adjusted",
                                        "title": candidate.title,
                                        "reason": (
                                            f"游戏准入：{verdict.reason}"
                                            f"（{verdict.penalty} 分）"
                                        )[:160],
                                    }
                                )
                    # 通过全部过滤后才登记摘要指纹：被拒的稿件不占位（见上面去重说明）。
                    if len(prefix_key) >= news_filters.REPEAT_SUMMARY_MIN_LEN:
                        seen_summary_prefixes.add(prefix_key)
                # 第三层：低置信度不丢弃——照常入库、记入待复核明细，
                # 由人工校准后再决定是补规则还是维持收录（见 news_filters.review_reason）。
                # 无限制源也照记：它只是不被丢弃，标记照样对运维可见。
                review = news_filters.review_reason(candidate.title, candidate.summary)
                if review:
                    summary.reviewed += 1
                    summary.details.append(
                        {
                            "status": "review",
                            "title": candidate.title,
                            "reason": review[:160],
                        }
                    )
                hydrated.append(candidate)
                if not is_major_candidate:
                    normal_hydrated += 1

            if source.lang == "ja" and translate is not None:
                translated = _translate_candidates(hydrated, translate=translate)
                summary.translate_failed += len(hydrated) - len(translated)
                hydrated = translated

            pool.extend(hydrated)
            summary.per_source_stats[source.key] = (
                f"候选 {len(candidates)}｜新增候选 {len(fresh)}｜"
                f"过滤 {source_filtered}｜入池 {len(hydrated)}"
                + (f"｜正文详情页失败 {detail_failures}" if detail_failures else "")
                + ("｜日文原文" if untranslated_ja else "")
                + ("｜无限制全量" if source.unrestricted else "")
            )
            # 凑够就停：配额未满时沿用老优化（高优先级源凑够后低优先级源不再抓）；
            # 配额已满时进入「只捞官宣」模式，必须扫完所有源——官宣在哪个源里事先不知道，
            # 漏掉就补不回来（feed 会过期）。这是豁免唯一新增的请求成本：每源一次列表请求。
            #
            # 但「凑够」不能只看池子条数：动画源一个就能喂满 50 条，若就此 break，
            # 排在后面的漫画 / 轻小说 / 游戏源连请求都发不出去，类别保底永远落空。
            # 所以还要看后面未抓的源里有没有「欠额未满足」的类别（2026-10-23）。
            # 无限制源的候选同样不算「凑够」——acgch 是第一个抓的源，它的条目会把池子
            # 撑到 needed，把后面的源全饿死（2026-10-25）。
            needed = remaining if remaining > 0 else exempt_remaining
            quota_pool_size = sum(1 for c in pool if not c.source.unrestricted)
            # 后面还有无限制源时不能停：它的候选不占配额，这里一 break 它就一条都抓不到。
            upcoming_has_free = any(
                spec.unrestricted for spec in ordered_sources[index + 1:]
            )
            if quota_pool_size >= needed and not upcoming_has_free:
                if remaining <= 0 or not _has_pending_category_floor(
                    ordered_sources[index + 1 :], pool, category_deficit
                ):
                    break  # 已凑够，低优先级源不必再抓

        if not pool:
            if remaining <= 0:
                # 配额已满但豁免额度还有：本轮已进入「只捞官宣」模式却一无所获。
                summary.message = (
                    f"今日已入库 {summary.added_before} 条，达到每日上限 {limit_total} 条，"
                    f"豁免额度尚余 {exempt_remaining} 条，但本轮各源无新的重大官宣"
                )
            else:
                summary.message = (
                    f"今日已入库 {summary.added_before} 条，剩余配额 {remaining} 条，"
                    f"但本轮各源无新增候选"
                )
            if summary.per_source_stats:
                summary.message += "（" + "；".join(
                    f"{k} {v}" for k, v in summary.per_source_stats.items()
                ) + "）"
            return summary

        # 软配额：官宣走豁免额度并置顶，其余按 priority_score 排序 + 类别封顶 + 同事件去重，
        # 不足不强行填满。
        # 无限制源（2026-10-25）不参与这一套：从池子里摘出来全量追加——不占 remaining、
        # 不受 _PER_SUBJECT_CAP 类别封顶、不在同事件去重里跟别的源竞争。用户对这个源的
        # 要求是「RSS 有多少新条目就全抓」，配额体系一点不掺和。
        free_pool = [c for c in pool if c.source.unrestricted]
        selected, deduped = select_quota(
            [c for c in pool if not c.source.unrestricted],
            remaining, exempt_remaining, category_deficit,
        )
        selected.extend(free_pool)
        summary.deduped = deduped
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()
        for candidate in selected:
            if candidate.url in seen_urls or candidate.title in seen_titles:
                summary.existing += 1
                continue
            seen_urls.add(candidate.url)
            seen_titles.add(candidate.title)
            info = _classify(candidate)
            reason = (
                f"{candidate.source.label}｜{info['category']}"
                f"{'／' + info['subcategory'] if info['subcategory'] else ''}"
                f"｜{info['region']}｜{info['event_type']}｜score={info['priority_score']}"
            )
            if info["major"]:
                reason = f"豁免·{info['major_hit']}｜{reason}"
            if not apply:
                summary.added += 1  # 预览：记「可写入」
                summary.per_source[candidate.source.key] = (
                    summary.per_source.get(candidate.source.key, 0) + 1
                )
                summary.per_category[info["category"]] = (
                    summary.per_category.get(info["category"], 0) + 1
                )
                # 官宣扣豁免额度（用完则回落扣普通配额），普通稿扣每日配额。
                if info["major"] and exempt_remaining > 0:
                    exempt_remaining -= 1
                    summary.exempted += 1
                else:
                    remaining -= 1
                summary.details.append(
                    {"status": "pending", "title": candidate.title, "reason": reason}
                )
                continue

            article = _persist(db, candidate, now=clock)
            if article is None:
                summary.failed += 1
                summary.details.append(
                    {"status": "failed", "title": candidate.title, "reason": "写入失败"}
                )
                continue
            summary.added += 1
            summary.per_source[candidate.source.key] = (
                summary.per_source.get(candidate.source.key, 0) + 1
            )
            summary.per_category[info["category"]] = (
                summary.per_category.get(info["category"], 0) + 1
            )
            if candidate.source.unrestricted:
                pass  # 无限制源不扣任何额度（2026-10-25）：日配额 / 豁免额度都不占
            elif info["major"] and exempt_remaining > 0:
                exempt_remaining -= 1
                summary.exempted += 1
            else:
                remaining -= 1
            pending.append(article)
            summary.details.append(
                {"status": "added", "title": candidate.title, "reason": reason}
            )

        if apply and pending:
            db.flush()  # 先分配 id，供标签关联行引用
            article_tags.link_articles(db, pending, extra_names_map={})
        if apply:
            db.commit()
        summary.message = (
            f"今日入库 {summary.added_before + summary.added} 条 / 上限 {limit_total} 条，"
            f"本轮新增 {summary.added} 条"
        )
        if summary.exempted:
            summary.message += f"（官宣豁免 {summary.exempted} 条，不占配额）"
        if summary.per_source_stats:
            summary.message += "（" + "；".join(
                f"{k} {v}" for k, v in summary.per_source_stats.items()
            ) + "）"
        return summary
    finally:
        db.close()


def _filter_new(db, candidates: list[NewsCandidate]) -> list[NewsCandidate]:
    """按 original_url 与 title 双重去重（与旧导入器口径一致，不覆盖存量）。"""
    urls = [candidate.url for candidate in candidates]
    titles = [candidate.title for candidate in candidates]
    existing_urls: set[str] = set()
    existing_titles: set[str] = set()
    if urls:
        existing_urls = {
            row for row in db.scalars(select(Article.original_url).where(Article.original_url.in_(urls)))
        }
    if titles:
        existing_titles = {
            row for row in db.scalars(select(Article.title).where(Article.title.in_(titles)))
        }
    fresh: list[NewsCandidate] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for candidate in candidates:
        if candidate.url in existing_urls or candidate.title in existing_titles:
            continue
        if candidate.url in seen_urls or candidate.title in seen_titles:
            continue  # 同批多条重复（源站偶发）只留第一条
        seen_urls.add(candidate.url)
        seen_titles.add(candidate.title)
        fresh.append(candidate)
    return fresh


def _persist(db, candidate: NewsCandidate, *, now: datetime) -> Article | None:
    """写一条 Article（含图片本地化）；失败返回 None，标签识别由调用方批量收尾。

    用 SAVEPOINT 包裹：单条写入失败只回滚该条，不影响本批已成功写入的其它文章。
    """
    info = _classify(candidate)
    try:
        with db.begin_nested():
            localized = article_media.localize_media(
                _build_body(candidate, translated=candidate.translated),
                candidate.image_url,
                key=_media_key(candidate.url),
            )
            article = Article(
                title=candidate.title[:200],
                category=info["category"],
                summary=(candidate.summary or candidate.title)[:400],
                body=localized.body,
                image_url=localized.head_image,
                ip_name=None,
                source=candidate.source.label,
                original_url=candidate.url,
                is_published=True,
                published_at=candidate.published_at or now,
                subcategory=info["subcategory"],
                event_type=info["event_type"],
                region=info["region"],
                source_type=info["source_type"],
                priority_score=info["priority_score"],
                is_adult=info["is_adult"],
                # 重大官宣标记：豁免额度按天统计靠它（cron 每轮独立进程，内存计数无效）。
                is_major=bool(info["major"]),
            )
            db.add(article)
            db.flush()
        return article
    except Exception:  # noqa: BLE001 - 单条写入失败不该中断整轮（由调用方计数）
        db.rollback()  # 兜底：极少数 savepoint 未释放时清理
        return None


# ---------- 命令行入口（手工补量 / 排障用；cron 走 scripts/run_sync.py） ----------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按自然日配额抓取多源资讯（默认预览，--apply 才写入）"
    )
    parser.add_argument("--apply", action="store_true", help="正式写入数据库")
    parser.add_argument(
        "--quota", type=int, default=None, metavar="N",
        help=f"当日新增上限（默认取配置 NEWS_DAILY_QUOTA={settings.news_daily_quota}）",
    )
    parser.add_argument(
        "--run-quota", type=int, default=None, metavar="N",
        help="单轮入库上限（默认取配置 NEWS_RUN_QUOTA="
             f"{settings.news_run_quota}，0 表示不限制）",
    )
    parser.add_argument(
        "--category-floor", type=int, default=None, metavar="N",
        help="每个主体类型（动画/漫画/轻小说/游戏）每天的保底条数（默认取配置 "
             f"NEWS_CATEGORY_FLOOR={settings.news_category_floor}，0 表示关闭）",
    )
    parser.add_argument(
        "--exempt-quota", type=int, default=None, metavar="N",
        help="重大官宣豁免额度（默认取配置 "
             f"NEWS_EXEMPT_MAX_PER_DAY={settings.news_exempt_max_per_day}，不占每日配额）",
    )
    parser.add_argument(
        "--source", default=None, metavar="KEY",
        help="只跑指定源（可用：" + " / ".join(spec.key for spec in SOURCES) + "）",
    )
    parser.add_argument(
        "--database-url", default=settings.database_url, help="数据库连接串"
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    sources = SOURCES
    if args.source:
        picked = tuple(spec for spec in SOURCES if spec.key == args.source)
        if not picked:
            print(f"错误：未知的源 {args.source}")
            return 2
        sources = picked
    engine = build_engine(args.database_url, busy_timeout=60.0)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        summary = run_import_daily(
            factory,
            apply=args.apply,
            quota=args.quota,
            run_quota=args.run_quota,
            category_floor=args.category_floor,
            exempt_quota=args.exempt_quota,
            sources=sources,
        )
        for item in summary.details[:DETAILS_MAX]:
            print(f"[{item['status']}] {item['title']} - {item.get('reason', '')}")
        print(summary.describe())
        print(summary.message)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
