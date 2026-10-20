"""查询理解层（app/search_query.py）的行为测试。

钉住三类口径：
- 确定性意图识别：类型/题材/年份/季数 各自的提取与守卫（宁漏不误）；
- 季数等价类：第二季 = 第2季 = 第二期 = 第2期 = Ⅱ/II = 2nd；
- 失败兜底：识别不了的词原样进关键词，最差等价旧版 LIKE。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.search_query import (  # noqa: E402
    parse_query,
    parse_season_suffix,
    season_variants,
)


class TestTypeWords:
    def test_exact_token_type(self):
        p = parse_query("游戏 治愈")
        assert p.content_type == "game"
        assert p.genres == ["治愈"]
        assert p.keyword == ""

    def test_title_like_token_not_type(self):
        """「游戏王」是标题不是类型词——防误伤的核心守卫。"""
        p = parse_query("游戏王")
        assert p.content_type is None
        assert p.keyword == "游戏王"

    def test_de_bound_suffix_type(self):
        p = parse_query("2019年的动画")
        assert p.year == 2019
        assert p.content_type == "anime"
        assert p.keyword == ""

    def test_light_novel_beats_novel(self):
        p = parse_query("轻小说")
        assert p.content_type == "novel"


class TestGenres:
    def test_single_genre_token(self):
        p = parse_query("治愈")
        assert p.genres == ["治愈"]
        assert p.keyword == ""

    def test_alias_genre_token(self):
        p = parse_query("治愈系")
        assert p.genres == ["治愈"]

    def test_compound_genre_token(self):
        p = parse_query("治愈日常")
        assert p.genres == ["治愈", "日常"]

    def test_partial_consumption_stays_keyword(self):
        """「轻喜剧」含未消费的「轻」——整段切不干净，保持关键词不误筛。"""
        p = parse_query("轻喜剧")
        assert p.genres == []
        assert p.keyword == "轻喜剧"

    def test_genre_plus_type_compound(self):
        """「治愈动画」= 题材头 + 类型尾：题材与类型同时识别。"""
        p = parse_query("治愈动画")
        assert p.genres == ["治愈"]
        assert p.content_type == "anime"
        assert p.keyword == ""

    def test_genre_plus_keyword(self):
        p = parse_query("治愈 凉宫春日")
        assert p.genres == ["治愈"]
        assert p.keyword == "凉宫春日"


class TestYear:
    def test_year_with_nian(self):
        p = parse_query("2019年的治愈动画")
        assert p.year == 2019
        assert p.content_type == "anime"
        assert p.genres == ["治愈"]

    def test_year_standalone(self):
        p = parse_query("2006 凉宫春日")
        assert p.year == 2006
        assert p.keyword == "凉宫春日"

    def test_title_digits_not_year(self):
        """数字嵌在标题里（前后挨着别的字符）不当年份。"""
        p = parse_query("命运石之门0")
        assert p.year is None
        assert p.keyword == "命运石之门0"


class TestSeason:
    @pytest.mark.parametrize(
        "query,base,number",
        [
            ("败犬女主太多了 第二季", "败犬女主太多了", 2),
            ("败犬女主太多了 第二期", "败犬女主太多了", 2),
            ("败犬女主太多了 第2期", "败犬女主太多了", 2),
            ("败犬女主太多了 Ⅱ", "败犬女主太多了", 2),
            ("fate/stay night II", "fate/stay night", 2),
            ("某科幻番 3rd", "某科幻番", 3),
            ("第七季 纪元", "纪元", 7),
            ("第十二期 边缘", "边缘", 12),
        ],
    )
    def test_season_equivalence(self, query, base, number):
        p = parse_query(query)
        assert p.season_number == number
        assert p.season_base == base
        assert p.keyword == base

    def test_season_in_title_without_space(self):
        p = parse_query("轻音少女第二季")
        assert p.season_number == 2
        assert p.keyword == "轻音少女"

    def test_bare_season_query_not_split(self):
        """剥完主体为空 → 放弃拆分，整串走关键词（防空关键词全表扫）。"""
        p = parse_query("第二季")
        assert p.season_number is None
        assert p.keyword == "第二季"

    def test_roman_one_not_season(self):
        """「I」「1st」歧义太大，不认成季数。"""
        p = parse_query("作品名 I")
        assert p.season_number is None

    def test_variants_cover_common_writings(self):
        v = season_variants(2)
        assert "第2季" in v and "第二期" in v and "Ⅱ" in v and "II" in v and "2nd" in v
        assert len(v) == len(set(v))

    def test_variants_no_roman_for_one(self):
        v = season_variants(1)
        assert "第1季" in v and "第一期" in v
        assert "I" not in v

    def test_twelve(self):
        assert "第十二季" in season_variants(12)
        assert "Ⅻ" in season_variants(12)


class TestFallback:
    def test_empty_query(self):
        p = parse_query("   ")
        assert p.keyword == "" and not p.has_intent

    def test_unknown_word_passthrough(self):
        p = parse_query("凉宫春日的忧郁")
        assert p.keyword == "凉宫春日的忧郁"
        assert not p.has_intent

    def test_no_space_compound_keeps_keyword(self):
        """无空格长串识别不了就整体走关键词——最差等于旧版 LIKE。"""
        p = parse_query("某某作品名大全")
        assert p.keyword == "某某作品名大全"

    def test_full_width_normalized(self):
        p = parse_query("２００６年 凉宫春日")
        assert p.year == 2006
        assert p.keyword == "凉宫春日"


def test_parse_season_suffix_direct():
    base, number = parse_season_suffix("某作品 第10季")
    assert base == "某作品" and number == 10
    base, number = parse_season_suffix("没有季数的查询")
    assert number is None and base == "没有季数的查询"
