"""多源资讯每日同步：按自然日软配额把新资讯灌进站内。

口径（用户定稿）：
- **一天 30 条软配额**：全站自然日新增上限，不分源；cron 每 4 小时跑一次也按当天累计。
  软配额：**核心内容优先、不强行填满、不为凑数量展示低价值资讯**，且限制单一主体类型
  挤占全部位置（综合动态有封顶）。
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
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import article_media, article_tags, news_llm  # noqa: E402
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
    fetch_candidates,
    hydrate,
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
    subject = news_classify.normalize_subject(candidate.source.category)
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
    result = {
        "category": subject,
        "subcategory": subcategory,
        "event_type": event_type,
        "region": region,
        "source_type": source_type,
        "priority_score": priority_score,
        "is_adult": is_adult,
    }
    candidate.classification = result
    return result


def _candidate_sort_key(candidate: NewsCandidate) -> tuple:
    """池内排序键：priority_score 降序、其次最新发布。"""
    published = candidate.published_at or datetime.min
    score = _classify(candidate)["priority_score"]
    return (score, published)


def _ordered_sources(sources: tuple[SourceSpec, ...]) -> list[SourceSpec]:
    """抓源顺序：非官方 PR 源优先，官方 PR 源最后（PR 不参与主 feed 配额竞争）。"""
    return sorted(
        sources,
        key=lambda spec: 1 if spec.source_type == news_classify.SOURCE_TYPE_PR else 0,
    )


# 同一主体类型在每日配额里最多占多少条，避免单一类别挤占全部位置（兼顾类别覆盖）。
# 综合动态是兜底主体，压低占比；其余四类（动画 / 漫画 / 轻小说 / 游戏）不单独封顶。
_PER_SUBJECT_CAP = {"综合动态": 6}

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
    pool: list[NewsCandidate], remaining: int
) -> tuple[list[NewsCandidate], int]:
    """软配额挑选：core-first，兼顾类别覆盖与同事件去重。

    返回 (选中的候选, 因同事件重复被合并掉的条数)。不足 remaining 时不强行填满。
    """
    ordered = sorted(pool, key=_candidate_sort_key, reverse=True)
    selected: list[NewsCandidate] = []
    per_subject: dict[str, int] = {}
    fingerprints: list[set[str]] = []
    duplicates = 0
    for candidate in ordered:
        if len(selected) >= remaining:
            break
        info = _classify(candidate)
        subject = info["category"]
        cap = _PER_SUBJECT_CAP.get(subject)
        if cap is not None and per_subject.get(subject, 0) >= cap:
            continue
        fingerprint = _event_fingerprint(candidate.title)
        if any(_is_same_event(fingerprint, seen) for seen in fingerprints):
            duplicates += 1
            continue
        fingerprints.append(fingerprint)
        per_subject[subject] = per_subject.get(subject, 0) + 1
        selected.append(candidate)
    return selected, duplicates


DETAILS_MAX = 40  # 明细最多回传多少条（与 sync_tasks 的风格一致）


@dataclass
class DailyNewsSummary:
    """一次每日同步的汇总；字段名贴近 sync_tasks.TaskResult，便于映射。"""

    mode: str  # apply / preview
    quota: int
    added_before: int = 0  # 当天此前已入库
    requested: int = 0  # 本轮抓到的候选数
    existing: int = 0  # 库中已有（URL 或标题命中）
    added: int = 0
    failed: int = 0
    skipped_sources: int = 0  # 因缺翻译能力跳过的日文源
    translate_failed: int = 0  # 翻译失败退回原文的条数
    filtered: int = 0  # 被硬广过滤掉（不入主 feed）的条数
    deduped: int = 0  # 同事件重复报道被合并掉的条数
    per_source: dict[str, int] = field(default_factory=dict)
    details: list[dict] = field(default_factory=list)
    message: str = ""

    @property
    def pending(self) -> int:
        return 0 if self.mode == "apply" else self.added

    def describe(self) -> str:
        mode_label = "正式导入（--apply）" if self.mode == "apply" else "预览（dry-run）"
        lines = [
            f"模式：{mode_label}",
            f"今日配额：{self.quota} 条（已入库 {self.added_before} 条，软配额不强行填满）",
            f"本轮候选：{self.requested} 条",
            f"已存在跳过：{self.existing} 条",
            f"硬广过滤：{self.filtered} 条",
            f"同事件合并：{self.deduped} 条",
            f"新增：{self.added} 条",
            f"跳过的日文源：{self.skipped_sources} 个",
        ]
        if self.translate_failed:
            lines.append(f"翻译失败（保留原文）：{self.translate_failed} 条")
        if self.per_source:
            lines.append(
                "分源：" + "；".join(f"{key} {count}" for key, count in self.per_source.items())
            )
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


def _media_key(url: str) -> str:
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()[:16]


def _build_body(candidate: NewsCandidate, *, translated: bool) -> str:
    """拼正文：中文源用摘要正文，日文源用译文摘要；尾部都带原文链接。"""
    body = (candidate.body or candidate.summary or "").strip()
    if not body:
        body = candidate.title
    if not body.endswith(candidate.url):
        body = f"{body}\n\n原文链接：{candidate.url}"
    if translated:
        body = f"{body}\n（译文由自动翻译生成，原文见链接）"
    return body


def _translate_candidates(
    candidates: list[NewsCandidate], *, translate
) -> list[NewsCandidate]:
    """就地翻译标题与摘要；翻译失败的条目被丢弃（日文必须翻译后才入库）。"""
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
        kept.append(candidate)
    return kept


def run_import_daily(
    session_factory,
    *,
    apply: bool = False,
    quota: int | None = None,
    sources: tuple[SourceSpec, ...] | None = None,
    fetch=None,
    translate=None,
    now: datetime | None = None,
) -> DailyNewsSummary:
    """按自然日配额抓取多源资讯并入库。

    fetch：``fetch(url) -> text``，测试注入假网络；translate：``translate(list[str])``，
    传 None 时按 LLM 配置决定（没有 Key 就跳过日文源）。
    """
    from scripts import import_articles as legacy  # 复用同一套抓取实现与 UA

    clock = now or datetime.now()
    limit_total = quota if quota is not None else settings.news_daily_quota
    summary = DailyNewsSummary(mode="apply" if apply else "preview", quota=limit_total)
    fetch = fetch or legacy.fetch_page

    can_translate = translate is not None or news_llm.translate_available()
    if translate is None and can_translate:
        translate = news_llm.translate_texts

    db = session_factory()
    pending: list[Article] = []  # 本轮新写入的文章，收尾统一做标签识别
    try:
        summary.added_before = count_added_today(db, clock)
        remaining = max(0, limit_total - summary.added_before)
        if remaining <= 0:
            summary.message = (
                f"今日已入库 {summary.added_before} 条，达到每日上限 {limit_total} 条，本轮不再抓取"
            )
            return summary

        # 归集所有源的候选，再统一按 priority_score 填软配额。
        # 官方 PR 源排在最后；凑够配额后不再抓取低优先级源（尤其游民星空详情页）。
        ordered_sources = _ordered_sources(sources or SOURCES)
        pool: list[NewsCandidate] = []
        for source in ordered_sources:
            if remaining <= 0:
                break
            if source.lang == "ja" and not can_translate:
                summary.skipped_sources += 1
                continue
            try:
                candidates = fetch_candidates(
                    source, fetch=fetch, limit=max(remaining * 2, 10)
                )
            except (NewsSourceError, RuntimeError, OSError, TimeoutError) as exc:
                # RuntimeError 覆盖旧导入器的 ArticleFetchError（也是 RuntimeError 子类）
                summary.failed += 1
                summary.details.append(
                    {"status": "failed", "title": f"{source.label} 抓取失败", "reason": str(exc)[:160]}
                )
                continue

            summary.requested += len(candidates)
            fresh = _filter_new(db, candidates)
            summary.existing += len(candidates) - len(fresh)
            hydrated: list[NewsCandidate] = []
            for candidate in fresh:
                try:
                    hydrate(candidate, fetch=fetch)
                except (NewsSourceError, RuntimeError, OSError, TimeoutError) as exc:
                    summary.failed += 1
                    summary.details.append(
                        {"status": "failed", "title": candidate.title, "reason": f"详情页失败：{exc}"[:160]}
                    )
                    continue
                if not candidate.summary:
                    candidate.summary = (candidate.body or "")[:200]
                # 硬广过滤：只挡最露骨广告（营销活动 / galge 一律保留）。
                skip_reason = news_filters.should_skip_article(
                    candidate.title, candidate.summary, candidate.source.category
                )
                if skip_reason:
                    summary.filtered += 1
                    summary.details.append(
                        {
                            "status": "filtered",
                            "title": candidate.title,
                            "reason": skip_reason[:160],
                        }
                    )
                    continue
                hydrated.append(candidate)

            if source.lang == "ja" and translate is not None:
                translated = _translate_candidates(hydrated, translate=translate)
                summary.translate_failed += len(hydrated) - len(translated)
                hydrated = translated

            pool.extend(hydrated)
            if len(pool) >= remaining:
                break  # 高优先级源已凑够，低优先级源不必再抓

        if not pool:
            summary.message = (
                f"今日已入库 {summary.added_before} 条，剩余配额 {remaining} 条，"
                f"但本轮各源无新增候选"
            )
            return summary

        # 软配额：按 priority_score 排序 + 类别覆盖封顶 + 同事件去重，不足不强行填满。
        selected, deduped = select_quota(pool, remaining)
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
            if not apply:
                summary.added += 1  # 预览：记「可写入」
                summary.per_source[candidate.source.key] = (
                    summary.per_source.get(candidate.source.key, 0) + 1
                )
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
                _build_body(candidate, translated=candidate.source.lang == "ja"),
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
            factory, apply=args.apply, quota=args.quota, sources=sources
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
