"""标签归一与清洗脚本：题材 / 公司 / 年份三分，噪音丢弃。

覆盖：
- classify_tag / split_tags：年份、公司（别名与后缀）、题材、噪音（声优 / 媒介）；
- year_from_date / year_from_summary 兜底；
- scripts/clean_content_tags.py：预览不写库、--apply 写库、年份优先级、幂等、孤儿标签清理。
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base
from app.models import BangumiCollection, Content, Tag
from app.tagging import (
    classify_tag,
    expand_genre_names,
    split_tags,
    year_from_date,
    year_from_summary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "clean_content_tags_for_tests", PROJECT_ROOT / "scripts" / "clean_content_tags.py"
)
clean_script = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = clean_script
_spec.loader.exec_module(clean_script)


class TestClassify:
    def test_year(self):
        assert classify_tag("2011") == ("year", "2011")
        assert classify_tag(" 2024 ") == ("year", "2024")
        assert classify_tag("11") != ("year", "11")

    def test_studio_alias_and_canonical(self):
        assert classify_tag("京都动画") == ("studio", "京都动画")
        assert classify_tag("京阿尼") == ("studio", "京都动画")
        assert classify_tag("A1 Pictures") == ("studio", "A-1 Pictures")
        assert classify_tag("BONES") == ("studio", "BONES")
        # 同义词归一到规范名（WIT STUDIO 的常见大小写写法）
        assert classify_tag("Wit Studio") == ("studio", "WIT STUDIO")

    def test_studio_by_suffix(self):
        assert classify_tag("Geno Studio") == ("studio", "Geno Studio")
        assert classify_tag("CloverWorks") == ("studio", "CloverWorks")

    def test_genre(self):
        assert classify_tag("日常") == ("genre", "日常")
        assert classify_tag("机战") == ("genre", "机战")

    def test_genre_aliases_normalized(self):
        """同义词归一到规范名：SF->科幻、癒し->治愈、日常系->日常。"""
        assert classify_tag("SF") == ("genre", "科幻")
        assert classify_tag("ＳＦ") == ("genre", "科幻")  # 全角
        assert classify_tag("sf") == ("genre", "科幻")
        assert classify_tag("癒し") == ("genre", "治愈")
        assert classify_tag("日常系") == ("genre", "日常")
        assert classify_tag("BL") == ("genre", "耽美")
        assert classify_tag("幼馴染") == ("genre", "青梅竹马")

    def test_game_genres_are_genre_platforms_are_noise(self):
        """游戏类型词算题材；平台词（本站无平台维度）算噪音。"""
        assert classify_tag("RPG") == ("genre", "RPG")
        assert classify_tag("Roguelike") == ("genre", "Roguelike")
        assert classify_tag("roguelike") == ("genre", "Roguelike")  # 小写归一到规范名
        assert classify_tag("Galgame") == ("genre", "Galgame")
        assert classify_tag("开放世界") == ("genre", "开放世界")
        for platform in ("PC", "PS5", "Switch", "手机", "Android", "Steam", "VR"):
            assert classify_tag(platform)[0] == "noise", platform

    def test_season_tag_is_noise(self):
        """季度标签（2011年10月）不是年份也不是题材：发行年由 date 承载。"""
        assert classify_tag("2011年10月")[0] == "noise"
        assert classify_tag("2021年1月")[0] == "noise"
        assert classify_tag("2011") == ("year", "2011")

    def test_noise_before_studio_suffix(self):
        """「国产动画」是噪音不是公司：精确噪音匹配先于公司后缀规则。"""
        assert classify_tag("国产动画")[0] == "noise"
        assert classify_tag("剧场版")[0] == "noise"

    def test_new_genre_words(self):
        for name in ("智斗", "权谋", "末日", "丧尸", "转生", "复仇", "武侠", "神怪", "剧情"):
            assert classify_tag(name)[0] == "genre", name

    def test_unknown_defaults_to_noise(self):
        assert classify_tag("花泽香菜")[0] == "noise"
        assert classify_tag("TV")[0] == "noise"
        assert classify_tag("漫画原作")[0] == "noise"
        assert classify_tag("")[0] == "noise"

    def test_split_tags_keeps_order_and_earliest_year(self):
        genres, studios, year = split_tags(
            ["日常", "2011", "京都动画", "花泽香菜", "TV", "2019", "机战", "京阿尼"]
        )
        assert genres == ["日常", "机战"]
        assert studios == ["京都动画"]
        assert year == 2011  # 多个年份取最早

    def test_expand_genre_names_variants(self):
        """口味词展开：原名 / 规范名 / 别名字串 / 关键词子串四级，只留站内存在的。"""
        site = {"日常", "搞笑", "百合", "致郁", "运动", "竞技", "党争"}
        assert expand_genre_names(["日常"], site) == ["日常"]
        # 画像原始写法（日常系→日常）与别名（搞笑番→搞笑）
        assert expand_genre_names(["日常系", "搞笑番"], site) == ["日常", "搞笑"]
        # 用户自然语言：别名字串（轻喜剧含喜剧→搞笑、体育竞技含体育→运动）
        assert expand_genre_names(["轻喜剧", "体育竞技"], site) == ["搞笑", "运动", "竞技"]
        # 关键词子串：致郁系→致郁、硬核科幻→科幻（站内没有科幻则丢弃）
        assert expand_genre_names(["致郁系", "硬核科幻"], site) == ["致郁"]
        # 站内不存的标签不进展开面；空词忽略
        assert expand_genre_names(["后宫", ""], site) == []
        # 带括号的口味词（体育竞技（除了赛马娘））照样展开
        assert expand_genre_names(["体育竞技（除了赛马娘）"], site) == ["运动", "竞技"]

    def test_expand_genre_names_empty_inputs(self):
        assert expand_genre_names([], {"日常"}) == []
        assert expand_genre_names(["日常"], set()) == []
        assert expand_genre_names([None, "  "], {"日常"}) == []

    def test_year_helpers(self):
        assert year_from_date("2006-04-02") == 2006
        assert year_from_date("") is None
        assert year_from_summary("2006 年的动画，Bangumi 评分 8.4。") == 2006
        assert year_from_summary("没有年份") is None


@pytest.fixture
def clean_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'clean.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        noisy = Content(
            title="噪音样本", type="anime", summary="某作品", rating=8.0, bangumi_id=901
        )
        db.add(noisy)
        db.commit()
        crud.replace_content_tags(
            db, noisy.id, ["日常", "2011", "京都动画", "花泽香菜", "TV"]
        )

        clean = Content(
            title="干净样本",
            type="anime",
            summary="某作品",
            rating=7.5,
            bangumi_id=902,
        )
        db.add(clean)
        db.commit()
        crud.replace_content_tags(db, clean.id, ["2015", "热血", "A-1 Pictures"])
    finally:
        db.close()
    return factory


class TestCleanScript:
    def test_preview_does_not_write(self, clean_env):
        db = clean_env()
        try:
            summary = clean_script.clean_contents(db, apply=False)
            assert summary.scanned == 2
            assert summary.changed == 2
            assert summary.applied == 0

            noisy = db.scalar(select(Content).where(Content.bangumi_id == 901))
            assert set(crud.list_content_tags(db, noisy.id)) == {
                "日常", "2011", "京都动画", "花泽香菜", "TV",
            }
            assert crud.list_content_studios(db, noisy.id) == []
        finally:
            db.close()

    def test_apply_splits_tags_studios_and_year(self, clean_env):
        db = clean_env()
        try:
            summary = clean_script.clean_contents(db, apply=True)
            assert summary.applied == 2

            noisy = db.scalar(select(Content).where(Content.bangumi_id == 901))
            assert crud.list_content_tags(db, noisy.id) == ["日常"]
            assert crud.list_content_studios(db, noisy.id) == ["京都动画"]
            assert noisy.year == 2011

            # 没有快照时，标签里的四位年份作为兜底
            clean = db.scalar(select(Content).where(Content.bangumi_id == 902))
            assert crud.list_content_tags(db, clean.id) == ["热血"]
            assert crud.list_content_studios(db, clean.id) == ["A-1 Pictures"]
            assert clean.year == 2015
        finally:
            db.close()

    def test_orphan_tags_pruned(self, clean_env):
        db = clean_env()
        try:
            clean_script.clean_contents(db, apply=True)
            names = set(db.scalars(select(Tag.name)))
            assert "花泽香菜" not in names
            assert "TV" not in names
            assert "2011" not in names
            assert {"日常", "热血"} <= names
        finally:
            db.close()

    def test_second_run_is_idempotent(self, clean_env):
        db = clean_env()
        try:
            clean_script.clean_contents(db, apply=True)
            again = clean_script.clean_contents(db, apply=True)
            assert again.changed == 0
            assert again.applied == 0
        finally:
            db.close()

    def test_snapshot_date_fills_year(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'snap.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        db = factory()
        try:
            content = Content(
                title="快照年份", type="anime", summary="无年份简介", rating=8.0, bangumi_id=903
            )
            db.add(content)
            db.add(
                BangumiCollection(
                    user_id=1,
                    subject_id=903,
                    subject_type=2,
                    name="快照年份",
                    date="2008-10-01",
                    rate=0,
                    collection_type=3,
                    private=False,
                    subject_tags=[],
                    user_tags=[],
                )
            )
            db.commit()
            crud.replace_content_tags(db, content.id, ["日常"])

            clean_script.clean_contents(db, apply=True)
            db.expire_all()
            assert db.scalar(select(Content.year).where(Content.id == content.id)) == 2008
        finally:
            db.close()
