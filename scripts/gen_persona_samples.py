"""生成模拟画像样本，用于校准称号命中分布（不入库、只读站内快照）。

背景：真实账号只有 28 个，验证不了「单个称号命中率 ≤5%」这种目标
（5% = 1.4 人，样本根本不够）。所以从站内库快照（output/calib/site_dump.json.gz，
18828 部、15481 部带题材标签）按人群原型抽样，造 300 个模拟画像跑称号分布。

用法：
    .venv/Scripts/python.exe scripts/gen_persona_samples.py

产物：output/calib/sim_profiles.json —— [{name, profile, title, code}, ...]
注意：模拟画像的分布不等于线上真实分布，只用来看「判据有没有把人分开」，
不能用来推算真实占比。
"""

from __future__ import annotations

import gzip
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import persona  # noqa: E402

DUMP = os.path.join("output", "calib", "site_dump.json.gz")
OUT = os.path.join("output", "calib", "sim_profiles.json")
FREQ_OUT = os.path.join("output", "calib", "title_freq.json")
SEED = 20261030
PER_ARCHETYPE = 12


def load_pool():
    with gzip.open(DUMP, "rt", encoding="utf-8") as f:
        rows = json.load(f)
    # 只要带题材标签的：没标签的作品对簇/题材判据没有贡献
    pool = [r for r in rows if r.get("g") and r.get("y")]
    pool.sort(key=lambda r: r.get("rc") or 0)
    return pool


def by_hot(pool, lo_pct, hi_pct):
    """按热度分位取子集：rc 排好序后切片。"""
    n = len(pool)
    return pool[int(n * lo_pct) : int(n * hi_pct)]


def by_year(pool, lo, hi):
    return [r for r in pool if lo <= int(r["y"]) <= hi]


def by_tag(pool, *tags):
    wanted = set(tags)
    return [r for r in pool if wanted & set(r["g"])]


def by_score(pool, lo, hi):
    return [r for r in pool if r.get("r") and lo <= float(r["r"]) <= hi]


def make_work(row, status, source="site", follow_status=0, rng=None):
    """转统一作品结构。

    ``rng`` 给定时模拟站内匹配率：线上实测只有 **51.7%** 的作品能对上站内条目、
    拿到细标签，其余只有 B站那种十几种的粗 styles。若每部都挂满站内细标签
    （全库 8939 种），题材种类数会虚高约 7 倍，exploration 直接顶满。
    """
    # 每部最多挂 2 个题材标签。校准依据：真实账号 300 部收藏的题材种类约 42 种
    # （由 exploration p50=41.7 反推 density=10.4 → distinct≈42）；挂 3 个时模拟
    # 的 distinct 能到 70，exploration 虚高到 70，判据阈值会按错的分布定。
    tags = list(row["g"])[:2]
    if rng is not None:
        # 65% 只挂 1 个粗标签：线上站内匹配率是 51.7%，但未匹配的那些作品在真实
        # 数据里也不只是 1 个标签（B站 styles 有十几种）。调到 65% 才能让模拟的
        # 题材种类数（distinct）落到真实量级（真实约 42 种 / 300 部）。
        if rng.random() < 0.65:
            tags = [rng.choice(tags)] if tags else []
    return {
        "title": row["t"],
        "type": row["ty"],
        "year": row["y"],
        "status": status,
        "tags": tags,
        "score": row.get("r"),
        "popularity": row.get("rc"),
        "rate": 0,
        "source": source,
        "follow_status": follow_status,
    }


def weight(row):
    """抽样权重：真实追番表不是均匀抽样，而是压倒性地偏向「热门 + 近年」。

    均匀抽会让 exploration / pop_spread / era_span 三个维度全部顶满
    （实测 p50 分别 95 / 100 / 100，而真实账号是 41.7 / 50 / —)，
    阈值会被调歪。所以按「热度对数 × 年代档位」加权。
    """
    import math

    rc = row.get("rc") or 0
    # 幂次而非对数：追番表是「平台热门番」的集合，热门与冷门的被收概率差几个
    # 数量级，用 log 抹平后热度仍然太分散（pop_spread 直接顶满 100）。
    wh = (rc + 1) ** 0.6
    y = int(row["y"])
    if y >= 2021:
        wy = 3.0
    elif y >= 2015:
        wy = 2.0
    elif y >= 2005:
        wy = 1.0
    else:
        wy = 0.4
    return wh * wy


def build(rng, pool, *, size, subset=None, statuses=None, source="site", mix=1.0):
    """按 (部数, 候选子集, 状态分配) 组装一个画像的 works。

    ``mix`` 是「纯度」：1.0 表示全部从目标子集抽。真实用户不会只看单一题材，
    单题材原型必须掺噪声（实测纯抽时 tag_focus 中位数能到 60+，而 28 个真实
    账号的 tag_focus 最高只有 55.4），否则样本失真、阈值会被调歪。
    """
    subset = subset if subset is not None else pool
    if len(subset) < size:
        size = len(subset)
    n_target = int(size * mix)
    n_target = min(n_target, len(subset))
    w = [weight(r) for r in subset]
    picked = rng.choices(subset, weights=w, k=n_target)
    if mix < 1.0 and len(picked) < size:
        w2 = [weight(r) for r in pool]
        picked += rng.choices(pool, weights=w2, k=size - len(picked))
    works = []
    for row in picked:
        if statuses:
            status = statuses(rng)
        else:
            status = "seen"
        fs = 2 if (source == "bilibili" and status == "seen") else 3
        works.append(make_work(row, status, source=source, follow_status=fs, rng=rng))
    return works


# ---- 人群原型 ----
# 每个原型：名 -> (候选池构造, 部数范围, 状态分配, 源)
def archetypes(pool):
    hot = by_hot(pool, 0.80, 1.0)
    cold = by_hot(pool, 0.0, 0.35)
    mid = by_hot(pool, 0.45, 0.60)
    new = by_year(pool, 2021, 2026)
    old = by_year(pool, 1970, 2010)
    hi = by_score(pool, 7.8, 10.0)
    lo = by_score(pool, 0.0, 6.2)
    half = len(pool) // 2
    era_wide = pool  # 跨年代：从全库均匀抽

    mono = lambda *t: by_tag(pool, *t)  # noqa: E731
    return {
        # 热度轴
        "hot_light": (hot, (60, 120), None),
        "cold_light": (cold, (60, 120), None),
        "hot_heavy": (hot, (200, 400), None),
        "cold_heavy": (cold, (150, 300), None),
        "mid_heavy": (mid, (200, 400), None),
        "mixed_heavy": (pool, (200, 400), None),
        # 冷热跨度（pop_spread）
        "spread_wide": (hot + cold, (120, 250), None),
        "spread_narrow": (mid, (120, 250), None),
        # 质量轴
        "score_high": (hi, (80, 200), None),
        "score_low": (lo, (80, 200), None),
        # 年代轴
        "new_only": (new, (60, 150), None),
        "old_only": (old, (60, 150), None),
        "era_wide": (era_wide, (150, 300), None),
        # 题材簇（每簇一个，用来激活主表）
        "g_heal": (mono("治愈", "日常"), (80, 180), None),
        "g_myst": (mono("悬疑", "推理"), (80, 180), None),
        "g_mecha": (mono("机战", "萝卜"), (80, 180), None),
        "g_yuri": (mono("百合"), (80, 180), None),
        "g_isekai": (mono("异世界"), (80, 180), None),
        "g_love": (mono("恋爱", "校园"), (80, 180), None),
        "g_comedy": (mono("搞笑"), (80, 180), None),
        "g_music": (mono("音乐", "偶像"), (80, 180), None),
        "g_battle": (mono("热血", "战斗"), (80, 180), None),
        "g_scifi": (mono("科幻"), (80, 180), None),
        "omni": (pool, (150, 300), None),
        # 行为轴
        "hoarder": (pool, (100, 250), lambda r: "want" if r.random() < 0.7 else "seen"),
        "dropper": (
            pool,
            (100, 250),
            lambda r: "drop" if r.random() < 0.35 else "seen",
        ),
        "half_done": (
            pool,
            (100, 250),
            lambda r: "hold" if r.random() < 0.4 else "seen",
        ),
        # B站式：全「看过」但大量其实是「在看」（测 completion 修复）
        "bili_watching": (
            hot,
            (150, 350),
            lambda r: "seen",
        ),
    }


def main():
    rng = random.Random(SEED)
    pool = load_pool()
    print(f"候选池 {len(pool)} 部（带标签且有年份）")

    archs = archetypes(pool)
    out = []
    for name, (subset, (lo, hi), statuses) in archs.items():
        if not subset:
            print(f"  跳过 {name}：候选池为空")
            continue
        for k in range(PER_ARCHETYPE):
            size = rng.randint(lo, hi)
            source = "bilibili" if name == "bili_watching" else "site"
            # 单题材原型掺 40% 噪声：真实用户不会只看一个题材
            mix = 0.6 if name.startswith("g_") else 1.0
            works = build(
                rng,
                pool,
                size=size,
                subset=subset,
                statuses=statuses,
                source=source,
                mix=mix,
            )
            if name == "bili_watching":
                # 大量 follow_status=2（在看）：模拟 B站 追番表的真实形态
                for w in works:
                    w["follow_status"] = 2 if rng.random() < 0.7 else 3
            profile = persona.build_profile(works)
            title, code, _ = persona.assign_title(profile)
            out.append(
                {
                    "name": f"{name}#{k}",
                    "archetype": name,
                    "n": len(works),
                    "title": title,
                    "code": code,
                    "dims": profile["dimensions"],
                    "cluster": profile.get("cluster"),
                    "identity": profile.get("identity"),
                    "persona_tags": profile.get("persona_tags"),
                    "genre_counts": profile.get("genre_counts"),
                }
            )

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # 分布
    from collections import Counter

    c = Counter(x["title"] for x in out)
    total = len(out)
    print(f"\n生成 {total} 个画像，命中 {len(c)} 种称号\n")
    print(f"{'称号':<16}{'命中':>5}{'占比':>8}")
    for t, n in c.most_common():
        flag = "  ← 超 5%" if n / total > 0.05 else ""
        print(f"{t:<16}{n:>5}{n / total * 100:>7.1f}%{flag}")
    print(f"\nTop1 占比 {c.most_common(1)[0][1] / total * 100:.1f}%")
    print(f"零命中称号数：{60 - len(c)}（按 60 个称号目标）")

    # 稀有度先验：把每个称号的实测占比存下来，供 persona.TITLE_RARITY 用。
    # 关键是**零命中的称号也要写进去**（share=0），否则读这份表的人会误以为
    # 「没出现 = 没这个称号」，而实际上那是「校准样本里没人拿到」——稀有度最高的一档。
    all_titles = {}
    for row in (
        persona.TITLE_COMBO
        + persona.TITLE_EXTREME
        + persona.TITLE_FANDOM
        + persona.TITLE_AXIS
        + persona.TITLE_FALLBACK
    ):
        all_titles[row[2]] = row[1]
    for _cluster, row in persona.TITLE_MAIN.items():
        for _feature, cell in row.items():
            all_titles[cell[1]] = cell[0]
    for _identity, cell in persona.TITLE_IDENTITY.items():
        all_titles[cell[1]] = cell[0]

    freq = Counter(x["code"] for x in out)
    table = {
        code: {
            "name": name,
            "count": freq.get(code, 0),
            "share": round(freq.get(code, 0) / total * 100, 2),
        }
        for code, name in all_titles.items()
    }
    with open(FREQ_OUT, "w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=1, sort_keys=True)
    hit = sum(1 for v in table.values() if v["count"])
    print(
        f"\n稀有度先验已写入 {FREQ_OUT}：{len(table)} 个称号，"
        f"命中 {hit} / 零命中 {len(table) - hit}"
    )

    # 维度分布自检：模拟样本必须落在真实账号的量级里，否则阈值会调歪。
    # 真实 28 个账号参考（2026-10-30 实测 p25/p50/p75）：
    # tag_focus 34.8/39.7/44.7  mainstream 56.3/86.2/89.8  quality 55.6/63.8/72.7
    # exploration 40.0/41.7/46.7  pop_spread 29.2/50.0/64.6  fresh 17.9/29.6/39.5
    # NICHE 判据的 min_share 校准：现有门槛（百合 .08 / 机战 .06 / 恋爱校园 .22 …）
    # 是补站内标签**之前**定的，补完之后题材分布全变了，必须重算。
    # 这里输出每个题材组的占比分位 + 各候选阈值下的命中率，用来反推新门槛。
    groups = {
        "百合": ["百合"],
        "机战": ["机战", "萝卜", "机甲", "机器人"],
        "悬疑推理": ["悬疑", "推理"],
        "恋爱校园": ["恋爱", "校园"],
        "治愈日常": ["治愈", "日常"],
        "扭曲党争": ["扭曲", "党争", "修罗场"],
        "耽美乙女": ["耽美", "乙女"],
        "乐队": ["乐队"],
        "偶像": ["偶像", "Live"],
        "异世界": ["异世界"],
        "搞笑": ["搞笑"],
        "科幻": ["科幻"],
        "热血战斗": ["热血", "战斗"],
    }
    print("\nNICHE 判据 min_share 校准（占比分位 / 各阈值命中率）：")
    for label, names in groups.items():
        shares = []
        for x in out:
            gc = x.get("genre_counts") or {}
            if not gc:
                continue
            total = sum(gc.values())
            hit = sum(v for k, v in gc.items() if k in names)
            shares.append(hit / total if total else 0.0)
        if not shares:
            continue
        shares.sort()
        q = lambda p: shares[int(len(shares) * p)]  # noqa: E731
        rates = " ".join(
            f"≥{t:.2f}:{sum(1 for s in shares if s >= t) / len(shares) * 100:.0f}%"
            for t in (0.08, 0.15, 0.22, 0.30, 0.40, 0.50)
        )
        print(
            f"  {label:<10} p50={q(0.5):.3f} p75={q(0.75):.3f} "
            f"p90={q(0.90):.3f} p95={q(0.95):.3f} | {rates}"
        )

    # 兜底人群画像：掉进身份兜底的人到底长什么样？不知道就没法给他们设计称号，
    # 只能靠猜阈值来回试。
    fallback = {
        "当季番剧挂机者",
        "原作党",
        "不白金不封盘",
        "不等动画化",
        "四类都沾",
        "宅宅",
    }
    fb = [x for x in out if x["title"] in fallback]
    keys = list(out[0]["dims"])
    if fb:
        print(f"\n掉进身份兜底的有 {len(fb)} 个（{len(fb) / total * 100:.1f}%），其维度分布：")
        for k in keys:
            xs = sorted(x["dims"][k] for x in fb)
            q = lambda p: xs[min(len(xs) - 1, int(len(xs) * p))]  # noqa: E731
            allxs = sorted(x["dims"][k] for x in out)
            aq = lambda p: allxs[min(len(allxs) - 1, int(len(allxs) * p))]  # noqa: E731
            print(
                f"  {k:<12} 兜底 {q(0.25):>6.1f} {q(0.5):>6.1f} {q(0.75):>6.1f}"
                f"   | 全体 {aq(0.25):>6.1f} {aq(0.5):>6.1f} {aq(0.75):>6.1f}"
            )

    print("\n模拟样本的维度分布（p25/p50/p75）：")
    keys = list(out[0]["dims"])
    for k in keys:
        xs = sorted(x["dims"][k] for x in out)
        q = lambda p: xs[int(len(xs) * p)]  # noqa: E731
        print(f"  {k:<12} {q(0.25):>6.1f} {q(0.5):>6.1f} {q(0.75):>6.1f}")


if __name__ == "__main__":
    main()
