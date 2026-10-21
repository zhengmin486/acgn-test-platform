"""从 Bangumi 官方数据归档（github.com/bangumi/Archive）选品。

为什么用归档而不是 API / 网页（2026-10-18）：
    - v0 排行榜接口全 404，网页 browser 页需要代理且本机代理对 Bangumi 间歇性断连；
    - 归档是官方每周三全量导出（subject.json 含 rank 类别内排名 / score 评分 /
      tags 标签 / infobox 原始 wiki 字符串 / summary 简介），GitHub 直连国内可用，
      **完全不依赖代理**；
    - rank 是官方「类别内排名」，比收藏数/评分更适合做「优先抓高分」的依据。

数据形态：dump.zip 内为 JSON Lines（每行一个 JSON 对象），subject.json 可能
数百 MB，所以**流式逐行读**（zipfile.open 直接返回文件对象），不整包载入内存。

type 语义：Archive README 写「1=漫画,2=动画,3=音乐,4=游戏,6=三次元」，但实测
API v0 的 type=1 是「书籍」（小说与漫画混在一起）。**以实际数据为准做双保险**：
先统计 type 分布再决定过滤集合，脚本默认按 API 语义（1=novel+comic 的书籍区）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fill_schedule_works import resolve_base_url  # noqa: E402
from scripts.import_bangumi import (  # noqa: E402
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    build_opener,
    fetch_related_subjects,
    is_galge_payload,
    resolve_book,
    resolve_content_type,
)

SUBJECT_MEMBER_RE = "subject"


def parse_wiki_infobox(raw: str) -> list[dict[str, str]]:
    """把 Bangumi 原始 wiki 信息卡字符串转成 API 的 [{key, value}] 结构。

    wiki 格式（实例见 /subject/1/edit）：
        |别名: {{xx: a}} 或
        |中文名: 新世纪福音战士
        |话数: 26
    每行一个 ``|key: value``（或 ``|key=value``），值可以跨行（多行属于上一个键，
    直到下一个 ``|`` 开头的行）。解析失败的单行跳过，绝不抛异常——识别是增强
    不是关键路径。

    返回值与 app/entity_links.parse_infobox 期望的 API 结构一致。
    """
    if not raw:
        return []
    pairs: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            body = stripped[1:]
            for sep in (":", "：", "="):
                if sep in body:
                    key, _, value = body.partition(sep)
                    break
            else:
                current = None
                continue
            key = key.strip()
            value = value.strip()
            if not key:
                current = None
                continue
            current = {"key": key, "value": value}
            pairs.append(current)
        elif current is not None:
            current["value"] += "\n" + stripped
    return pairs


def build_payload(entry: dict) -> dict | None:
    """把归档条目转成与 Bangumi API /v0/subjects/{id} 等价的 payload；不可用返回 None。

    归档字段形态（实测 2026-10-13 dump）：``score`` 直接是 float、``rank`` 是
    独立整数（0 表示无排名）、``score_details`` 是 {分数段: 人数}——与 API 的
    ``rating {score, rank, total}`` 嵌套结构不同，这里负责重新组装。

    - rating 构造：score 必须有效；rank 归档里 0 表示无排名，不回填为有效值；
    - tags：归档格式为 [{name, count}]（部分），直接透传；
    - images：归档**不含**封面地址，留空——封面由封面脚本/按需下载单独补；
    - infobox：wiki 字符串 -> API 结构，解析失败给空列表（识别自动跳过）。
    """
    score = entry.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or score <= 0:
        return None

    name = (entry.get("name") or "").strip()
    name_cn = (entry.get("name_cn") or "").strip()
    if not name_cn and not name:
        return None
    summary = (entry.get("summary") or "").strip()
    if not summary:
        return None

    rank = entry.get("rank")
    rank_value = rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else 0
    details = entry.get("score_details") or {}
    total = sum(int(v) for v in details.values()) if isinstance(details, dict) else 0

    tags = entry.get("tags")
    tags = tags if isinstance(tags, list) else []

    raw_infobox = entry.get("infobox") or ""
    infobox = (
        raw_infobox
        if isinstance(raw_infobox, list)
        else parse_wiki_infobox(raw_infobox if isinstance(raw_infobox, str) else "")
    )

    return {
        "id": entry.get("id"),
        "type": entry.get("type"),
        "name": name,
        "name_cn": name_cn,
        "summary": summary,
        "rating": {"score": float(score), "rank": rank_value, "total": total},
        "tags": tags,
        "infobox": infobox,
        "date": entry.get("date") or "",
        "nsfw": bool(entry.get("nsfw")),
        "platform": entry.get("platform") or "",
    }


def iter_subjects(dump_path: Path, member: str = "subject.jsonlines"):
    """流式读取归档里的 subject 条目，返回迭代器。

    官方归档成员名是 ``subject.jsonlines``（941MB 解压后），JSON Lines 格式。
    """
    with zipfile.ZipFile(dump_path) as archive:
        names = [n for n in archive.namelist() if n.endswith(member)]
        if not names:
            raise RuntimeError(f"{dump_path} 里找不到 {member}：{archive.namelist()[:10]}")
        with archive.open(names[0]) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    yield entry


def select_from_archive(
    dump_path: Path,
    per_type: dict[str, int],
    *,
    max_rank: int = 3000,
    year_from: str = "",
) -> tuple[list[dict], Counter, int]:
    """按 rank 从归档选品，返回 (payload 清单, 类型统计, 扫描总行数)。

    - 只取本站四类型、非 nsfw、rank > 0（无排名的不算热门）；
    - rank 越小越热门；``max_rank`` 兜底防止把排名几万的也捞进来；
    - ``year_from``（如 "2000"）按发行年份过滤——空日期条目一并排除；
    - 每类取 rank 最小的 ``per_type[类型]`` 条。
    """
    by_type_ranks: dict[str, list[tuple[int, int]]] = {t: [] for t in per_type}
    type_counter: Counter = Counter()
    scanned = 0

    for entry in iter_subjects(dump_path):
        scanned += 1
        if entry.get("nsfw"):
            continue
        # resolve_content_type：type=2→anime、4→game；type=1 书籍按 platform
        # 拆 comic(1001)/novel(1002)；type=6 是三次元真人剧（Archive README
        # 写「6=漫画」是错的），type=3 音乐——都返回 None 被排除。
        content_type = resolve_content_type(entry)
        if content_type is None:
            continue
        want = per_type.get(content_type, 0)
        if want <= 0:
            continue
        type_counter[content_type] += 1

        # 归档的 rank 是顶层整数（0 = 无排名），不是嵌套在 score 里
        rank = entry.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0 or rank > max_rank:
            continue

        if year_from:
            date = str(entry.get("date") or "")
            if date[:4] < year_from:
                continue

        heap = by_type_ranks[content_type]
        heap.append((rank, entry["id"]))
        heap.sort()
        del heap[want:]  # 只留头部，内存可控

    results: list[dict] = []
    for content_type, pairs in by_type_ranks.items():
        wanted_ids = {sid for _rank, sid in pairs}
        if not wanted_ids:
            continue
        for entry in iter_subjects(dump_path):
            if entry.get("id") not in wanted_ids:
                continue
            if entry.get("nsfw"):
                continue
            payload = build_payload(entry)
            if payload is None:
                continue
            payload["_tier_rank"] = dict((sid, r) for r, sid in pairs).get(entry["id"], 0)
            payload["_content_type"] = content_type
            results.append(payload)
    results.sort(key=lambda p: (p["_content_type"], p["_tier_rank"], p["id"]))
    return results, type_counter, scanned


def natural_quota(total: int) -> dict[str, int]:
    """自然比例配额（用户 2026-10-18 定）：50% 动画 / 20% 游戏 / 18% 小说 / 12% 漫画。"""
    return {
        "anime": int(total * 0.50),
        "game": int(total * 0.20),
        "novel": int(total * 0.18),
        "comic": int(total * 0.12),
    }


# ---------- 收录口径过滤（2026-10-21 用户定稿） ----------
# - 小说只留轻小说（classify_book 判成 literature / 判不出文库信号的一律剔除）；
# - 游戏：galge（tags 可判，不出网）直接收；非 galge 查镜像关联条目
#   （/v0/subjects/{id}/subjects），确认有动画/漫画相关 -> payload 打
#   ``_site_related_ip=True`` 标记（服务端 map_subject 凭此放行）；
#   未命中或查询失败 -> 剔除（宁缺勿错：不推明知会被服务端跳过的候选）。
# 服务端 map_subject 有同一套规则的兜底闸口，标记不可伪造绕过业务约束
# 之外的场景（随机导入 / 手动 run_sync 的游戏走总闸）。

ANIME_TYPE = 2  # Bangumi 动画；关联条目列表的 item.type 命中即算动画相关


def has_anime_manga_relation(rel_payload) -> bool:
    """关联条目列表里是否有动画（type=2）或漫画（关系文案带「漫画/动画」）。

    端点响应的 item 结构未完全定死（见 app.entity_links.parse_related_subjects
    的防御性注释），这里只认 dict；type 缺失时退回关系文案兜底——
    游戏条目上出现「动画化 / 漫画化」即说明有对应改编。
    """
    if not isinstance(rel_payload, list):
        return False
    for item in rel_payload:
        if not isinstance(item, dict):
            continue
        relation = str(item.get("relation") or "")
        if item.get("type") == ANIME_TYPE:
            return True
        if "动画" in relation or "漫画" in relation:
            return True
    return False


def apply_ingest_policy(
    payloads: list[dict],
    *,
    base_url: str | None = None,
    opener=None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = DEFAULT_USER_AGENT,
    sleep=time.sleep,
    related_fetcher=None,
) -> tuple[list[dict], dict]:
    """按站点收录口径过滤选品结果，返回 ``(保留的 payload, 统计)``。

    只对小说 / 游戏动手（动画、漫画原样保留）；非 galge 游戏逐个查镜像
    关联条目，命中动画 / 漫画就打 ``_site_related_ip`` 标记，查询失败按
    「无相关」剔除——宁缺勿错，与补库 / 选品的一贯口径一致。
    """
    base = base_url or resolve_base_url()
    opener = opener or build_opener(None)
    fetch = related_fetcher or fetch_related_subjects

    kept: list[dict] = []
    stats = {
        "novel_dropped": 0,      # 文学 / 判不出文库信号的小说
        "game_galge": 0,         # galge（tags 命中）直接收
        "game_related": 0,       # 非 galge 但有动画/漫画相关（打标记收）
        "game_dropped": 0,       # 非 galge 且无相关
        "game_fetch_failed": 0,  # 关联查询失败（该候选按剔除计）
    }
    for payload in payloads:
        content_type = payload.get("_content_type")
        if content_type == "novel":
            _type, kind = resolve_book(payload)
            if kind != "light_novel":
                stats["novel_dropped"] += 1
                continue
            kept.append(payload)
        elif content_type == "game":
            if is_galge_payload(payload):
                stats["game_galge"] += 1
                kept.append(payload)
                continue
            subject_id = payload.get("id")
            try:
                related = fetch(
                    int(subject_id),
                    opener=opener,
                    base_url=base,
                    timeout=timeout,
                    retries=retries,
                    user_agent=user_agent,
                    sleep=sleep,
                )
            except Exception:  # noqa: BLE001 - 查询失败按「无相关」处理，不中断选品
                related = None
                stats["game_fetch_failed"] += 1
            if has_anime_manga_relation(related):
                marked = dict(payload)
                marked["_site_related_ip"] = True
                kept.append(marked)
                stats["game_related"] += 1
            else:
                stats["game_dropped"] += 1
        else:
            kept.append(payload)
    return kept, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从官方归档按 rank 选品")
    parser.add_argument("--dump", default=str(PROJECT_ROOT / ".workbuddy/archive/dump.zip"))
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument(
        "--type",
        choices=["anime", "game", "novel", "comic"],
        default=None,
        help="只选某一类型（--total 全部给该类型）；缺省按自然比例分配",
    )
    parser.add_argument("--out", default=str(PROJECT_ROOT / ".workbuddy/pilot/selection.json"))
    parser.add_argument("--max-rank", type=int, default=3000)
    parser.add_argument(
        "--year-from",
        default="",
        help="只收录发行年份 >= 该值的条目（如 2000）；空日期一并排除",
    )
    args = parser.parse_args(argv)

    dump = Path(args.dump)
    if not dump.is_file():
        print(f"归档不存在：{dump}", file=sys.stderr)
        return 1

    per_type = natural_quota(args.total)
    if args.type:
        per_type = {k: 0 for k in per_type}
        per_type[args.type] = args.total
    print(f"配额：{per_type}")
    payloads, type_counter, scanned = select_from_archive(
        dump,
        per_type,
        max_rank=args.max_rank,
        year_from=str(args.year_from) if args.year_from else "",
    )
    print(f"扫描 {scanned} 行；候选分布：{dict(type_counter)}；选中 {len(payloads)} 条")

    # 收录口径过滤（2026-10-21）：小说只留轻小说，游戏只留 galge / 有动画漫画相关
    if payloads:
        payloads, policy_stats = apply_ingest_policy(payloads)
        print(
            "收录口径过滤：剔除文学小说 "
            f"{policy_stats['novel_dropped']}、普通游戏 {policy_stats['game_dropped']}"
            f"（galge 收 {policy_stats['game_galge']}、"
            f"标记动画/漫画相关游戏 {policy_stats['game_related']}）"
        )
        if policy_stats["game_fetch_failed"]:
            print(
                f"  ⚠ {policy_stats['game_fetch_failed']} 个游戏的关联条目查询失败，"
                "按「无动画/漫画相关」剔除（宁缺勿错）"
            )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payloads, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写出 {out}")

    for payload in payloads[:12]:
        print(
            f"  rank={payload['_tier_rank']:<5} {payload['_content_type']:<6}"
            f" #{payload['id']} {payload['name_cn'] or payload['name'][:24]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())