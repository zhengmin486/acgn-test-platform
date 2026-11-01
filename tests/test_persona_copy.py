"""文案质量护栏：称号毒舌必须满足「能被截图转发」的最低标准。

对应规范见 .workbuddy/memory/persona_copy_guide.md，命令行体检用
scripts/lint_persona_copy.py。这里把它固化成断言，避免以后改文案时静默退步
（比如又出现「全员同一句」的单句称号，或塞回「现实生活」这类全网通用梗）。

只守住可自动判定的部分；「有没有梗」仍要靠人写。
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from app import persona

# 全网通用梗：圈外人也能接，等于没有圈内共鸣
BANNED = ("现实生活", "母单", "现充爆炸", "被生活")
# 量化锚：数字 / 次数 / 时长 / 倍数 / 全量词
QUANT = re.compile(r"[0-9]|一年|两年|三年|一集|一部|一半|一场|一个字|全|零|百|千|倍")
MAX_LEN = 45


def _variants(note) -> list[str]:
    return list(note) if isinstance(note, (list, tuple)) else [note]


def _titles() -> list[tuple[str, str, list[str]]]:
    """全部称号及其变体：[(来源, 称号, [文案...])]。"""
    rows: list[tuple[str, str, list[str]]] = []
    for name, pool in (
        ("COMBO", persona.TITLE_COMBO),
        ("EXTREME", persona.TITLE_EXTREME),
        ("FANDOM", persona.TITLE_FANDOM),
        # AXIS / FALLBACK 是 2026-10-30 新增的两层，文案同样要进体检池，
        # 否则评分厨这类从 FANDOM 挪下去的称号会变成「漏检」。
        ("AXIS", persona.TITLE_AXIS),
        ("FALLBACK", persona.TITLE_FALLBACK),
    ):
        for cond, title, _code, note in pool:
            rows.append((name, title, _variants(note)))
    for cluster, row in persona.TITLE_MAIN.items():
        for _feat, (title, _code, note) in row.items():
            rows.append((f"MAIN/{cluster}", title, _variants(note)))
    for _ident, (title, _code, note) in persona.TITLE_IDENTITY.items():
        rows.append(("IDENTITY", title, _variants(note)))
    return rows


def _sentences() -> list[str]:
    return [s for _src, _title, vs in _titles() for s in vs]


def test_every_title_has_three_variants():
    """单句称号 = 所有同类型用户看到同一句话，是最先要消灭的形态。"""
    thin = [
        f"{src}/{title}（{len(vs)} 条）"
        for src, title, vs in _titles()
        if len(vs) < 3
    ]
    assert not thin, "这些称号变体不足 3 条：" + "、".join(thin)


def test_no_banned_offline_memes():
    bad = [
        s for s in _sentences() if any(word in s for word in BANNED)
    ]
    assert not bad, "出现全网通用梗（圈内无共鸣）：" + " / ".join(bad)


def test_sentences_are_short_enough():
    long = [s for s in _sentences() if len(s) > MAX_LEN]
    assert not long, "过长（没人会截图）的句子：" + " / ".join(long)


def test_no_duplicated_sentences():
    dup = [s for s, n in Counter(_sentences()).items() if n > 1]
    assert not dup, "重复句：" + " / ".join(dup)


def test_variants_not_same_opening():
    """同一称号的 3 条变体不能都从相同的几个字起头（读起来像同一句改的）。"""
    same = []
    for src, title, vs in _titles():
        heads = Counter(s[:3] for s in vs)
        for head, n in heads.items():
            if n > 1:
                same.append(f"{src}/{title}「{head}…」x{n}")
    assert not same, "句式同构：" + "、".join(same)


@pytest.mark.parametrize("minimum", [0.25])
def test_quantified_share(minimum):
    """带数字 / 量词的句子占比：全靠形容词的文案读者不会觉得「被说中」。"""
    sentences = _sentences()
    hit = sum(1 for s in sentences if QUANT.search(s))
    assert hit / len(sentences) >= minimum, (
        f"量化锚只有 {hit}/{len(sentences)}（{hit / len(sentences):.0%}），低于 {minimum:.0%}"
    )


# 「文案版本迁移」用的画像骨架（事实由调用方决定）
def _profile(**overrides) -> dict:
    profile = {
        "persona_tags": ["治愈系", "番剧党"],
        "cluster": "治愈",
        "identity": "番剧党",
        "total": 448,
        "seen": 446,
        "want": 0,
        "drop": 0,
        "dimensions": {
            "type_focus": 100,
            "tag_focus": 63,
            "mainstream": 100,
            "quality": 95,
            "exploration": 54,
            "fresh": 28,
            "completion": 100,
        },
        "top_genres": [{"name": "日常"}, {"name": "奇幻"}, {"name": "战斗"}],
        "top_tags": [],
        "avoided_tags": [],
        "top_studios": [],
        "entry_year": 2016,
        "latest_year": 2026,
        "entry_work": "某部入坑作",
        "latest_work": "某部新番",
        "bangumi_total": 0,
        "bilibili_total": 500,
        "site_counts": {"favorites": 6, "statuses": 0},
        "avg_score": 7.4,
        "avg_popularity": 30000,
        "representative": [],
        "notes": [],
    }
    profile.update(overrides)
    return profile


def test_stale_copy_gets_migrated():
    """老报告存的是旧文案，不迁移的话全量重写对老用户等于没发生。

    报告里的文案是生成那一刻写死的快照；这次把 66 个称号扩到 198 条，
    老报告必须自动换新，而不是逼用户点「重新生成」重算整个画像。
    """
    from app.persona_pages import _backfill_template_report

    stale_sentinel = "【旧文案占位】这句话不该再出现"
    payload = {
        "profile": _profile(),
        "rarity": None,
        "report": {
            # 三个键都齐、但版本落后 —— 旧逻辑在这里会直接 return，一句都不换
            "trait_blurbs": {"治愈系": stale_sentinel},
            "long_review": stale_sentinel,
            "share_line": stale_sentinel,
            "hero_title": "旧称号",
            "hero_note": stale_sentinel,
            "final_quip": stale_sentinel,
            "subtitle": "基于 448 部作品的御宅成分",
        },
    }

    migrated = _backfill_template_report(payload)
    assert migrated["copy_version"] == persona.COPY_VERSION
    assert stale_sentinel not in migrated["long_review"]
    assert stale_sentinel not in migrated["hero_note"]
    # 新毒舌必须取自当前文案池（而不是又编了一句）
    assert migrated["hero_note"] in _sentences()
    # 事实字段不动：迁移只换文案
    assert migrated["subtitle"] == "基于 448 部作品的御宅成分"


def test_up_to_date_report_is_returned_as_is():
    """版本一致时直接返回原对象，不做无谓重建（渲染路径每页都会调一次）。"""
    from app.persona_pages import _backfill_template_report

    report = persona.build_template_report(_profile(), None, "seed")
    payload = {"profile": _profile(), "rarity": None, "report": report}
    assert _backfill_template_report(payload) is report


def test_broken_profile_still_marks_version():
    """老 profile 缺字段导致重建失败时，不能每页重试抛异常，也不能 500。"""
    from app.persona_pages import _backfill_template_report

    payload = {
        "profile": {},  # 空画像：重建必失败
        "rarity": None,
        "report": {"long_review": "", "share_line": "", "trait_blurbs": {}},
    }
    out = _backfill_template_report(payload)
    assert out is not None
    assert out["copy_version"] == persona.COPY_VERSION


def test_assign_title_tolerates_profile_without_quality():
    """旧报告的 dimensions 只有 6 个键（线上真实存在，缺 quality / fresh）。

    之前 `p["dimensions"]["quality"]` 直接下标会 KeyError，把整段文案重建吞掉，
    老用户就一直停在旧文案上。
    """
    profile = _profile()
    profile["dimensions"] = {
        "type_focus": 100.0,
        "tag_focus": 0.0,
        "mainstream": 50.0,
        "exploration": 0.0,
        "hype": 0.0,
        "completion": 0.0,
    }
    title, code, note = persona.assign_title(profile, None, "seed")
    assert title and code and note
    # 缺 quality 不能凭填补值 50 拿下「流行厨」（要 <50）或「评价党」（要 ≥50）
    assert title not in ("流行厨", "评价党")


def test_long_review_small_sample_is_honest():
    """只标了 2 部就别硬编成分结论。"""
    text = persona._compose_long_review(
        _profile(total=2, seen=2, want=0, drop=0), None, "seed"
    )
    assert "2 部" in text
    assert "成分还看不出来" in text
    assert "翻来覆去" not in text


def test_long_review_without_genres_has_no_stray_space():
    """题材为空时不能拼出「那几个老口味 都沾一点」这种多余空格。"""
    dims = dict(_profile()["dimensions"], tag_focus=0, exploration=30)
    text = persona._compose_long_review(
        _profile(top_genres=[], top_tags=[], dimensions=dims), None, "seed"
    )
    assert " 都沾" not in text
    assert " 什么" not in text
    assert "什么类型都沾一点" in text


def test_radar_recomputed_with_current_dimensions():
    """雷达不能直接用报告里存的旧坐标：换过维度后标签会停在「追更强度」。"""
    from app.persona_pages import _radar_for

    stale = {
        "labels": [{"x": 0, "y": 0, "text": "追更强度", "anchor": "middle"}],
        "polygon": "",
        "grid": [],
        "axes": [],
    }
    radar = _radar_for({"profile": _profile(), "radar": stale})
    texts = [label["text"] for label in radar["labels"]]
    assert "追新度" in texts
    assert "追更强度" not in texts


def test_radar_falls_back_to_stored_when_profile_empty():
    from app.persona_pages import _radar_for

    stale = {"labels": [], "polygon": "", "grid": [], "axes": []}
    assert _radar_for({"profile": {}, "radar": stale}) is stale
