"""ACGN 游戏资讯准入闸门（2026-10-23 用户定稿）。

两条线各测一遍：
1. ``judge_game_admission`` 的判定顺序——评奖 → 硬件 → 3A → 白名单（含魔法少女）→
   独立 / 同人 → 兜底拒；
2. ``crud.list_acgn_game_terms`` 的准入集合——标签/人工表进、3A 不进、短词丢弃。

判定顺序本身就是需求：硬件必须排在白名单前面，否则「显卡驱动稿里提了一句
宝可梦」会被 IP 命中直接放行（用户明确禁止）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import crud
from app.database import Base
from app.models import Content, ContentCast, ContentTag, Tag
from scripts import news_filters
from scripts.news_filters import judge_game_admission


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'admission.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield session_factory
    engine.dispose()


class TestReject:
    """必须拒：硬件 / 外设 / 欧美 3A / 普通游戏 / 赛事。"""

    def test_gpu_driver_rejected(self):
        verdict = judge_game_admission(
            "「RTX Spark」版が加わった「GeForce 621.01 Driver」が登場",
            "最新ドライバの更新内容を解説",
        )
        assert verdict.allowed is False
        assert "硬件" in verdict.reason

    def test_wireless_mouse_rejected(self):
        verdict = judge_game_admission("UWB接続で電波干渉知らずのワイヤレスマウス「Sensei Pro」")
        assert verdict.allowed is False

    def test_call_of_duty_rejected(self):
        verdict = judge_game_admission("「Call of Duty: Modern Warfare 4」がいよいよ明日リリース")
        assert verdict.allowed is False
        assert "3A" in verdict.reason

    def test_western_aaa_update_rejected(self):
        verdict = judge_game_admission("「Forza Horizon 6」，大型アップデートを配信開始")
        assert verdict.allowed is False

    def test_racing_event_rejected_by_fallback(self):
        """F1 赛事稿不靠专门名单——没有任何 ACGN 信号，走兜底拒。"""
        verdict = judge_game_admission("F1 2026 シーズン開幕、新レギュレーションを解説")
        assert verdict.allowed is False
        assert "普通游戏" in verdict.reason

    def test_western_indie_rejected(self):
        """欧美独立游戏不放行（2026-10-23 用户更正）：形态词不再是放行依据。"""
        verdict = judge_game_admission("『Hollow Knight: Silksong』がインディーとして配信開始")
        assert verdict.allowed is False
        assert "普通游戏" in verdict.reason

    def test_indie_showcase_rejected(self):
        """独立游戏展示会这类行业稿同样不进——没有 ACGN 关联就是普通游戏。"""
        verdict = judge_game_admission("インディーゲーム展示会「DREAMSCAPE#5」を10月24日に開催")
        assert verdict.allowed is False

    def test_indie_fps_rejected(self):
        verdict = judge_game_admission("インディーFPS『未知の戦場』がアーリーアクセス開始")
        assert verdict.allowed is False

    def test_indie_racing_rejected(self):
        verdict = judge_game_admission("個人開発のレーシングゲームがSteamで配信開始")
        assert verdict.allowed is False

    def test_doujin_fps_rejected(self):
        """同人放行，但同人 FPS 仍属泛竞技体裁。"""
        verdict = judge_game_admission("同人サークルがFPSの新作を頒布")
        assert verdict.allowed is False
        assert "泛竞技" in verdict.reason

    def test_unknown_japanese_game_rejected(self):
        """日系但不在白名单里、也不是同人形态的游戏同样拒。"""
        verdict = judge_game_admission("『モンスターハンター』新作の体験版が配信", terms=())
        assert verdict.allowed is False

    def test_hardware_collab_without_ip_rejected(self):
        """有联名词但没命中任何 IP → 仍是硬件稿。"""
        verdict = judge_game_admission(
            "コラボモデルのゲーミングマウスを発表", terms=("ポケモン",)
        )
        assert verdict.allowed is False


class TestAllow:
    """必须放：Galgame / 二游 / 改编 / IP / 角色 / 创作者 / 评奖 / 联名周边。"""

    def test_galge_subcategory_allowed(self):
        verdict = judge_game_admission("新作ビジュアルノベルを発表", subcategory="Galgame")
        assert verdict.allowed is True
        assert "Galgame" in verdict.reason

    def test_gacha_subcategory_allowed(self):
        verdict = judge_game_admission("Ver.2.0 アップデート配信、新キャラ実装", subcategory="二游")
        assert verdict.allowed is True

    def test_anime_adaptation_game_allowed(self):
        verdict = judge_game_admission("TVアニメ『ホシ』のゲーム化が決定")
        assert verdict.allowed is True

    def test_voice_actor_interview_allowed(self):
        verdict = judge_game_admission("『ホシ』声優インタビュー、収録の裏側を聞く")
        assert verdict.allowed is True

    def test_ip_subtitle_allowed(self):
        verdict = judge_game_admission(
            "「Fate/hollow ataraxia」リメイク版が発表", terms=("Fate", "セイバー")
        )
        assert verdict.allowed is True
        assert "词表" in verdict.reason

    def test_character_only_allowed(self):
        """标题里没有作品名、只有角色名也要放（用户要求：不能因为没写作品名就漏）。"""
        verdict = judge_game_admission("セイバー新宿ver.の1/7スケールフィギュアが登場", terms=("セイバー",))
        assert verdict.allowed is True

    def test_award_allowed(self):
        verdict = judge_game_admission("The Game Awards 2026、GOTY 発表")
        assert verdict.allowed is True
        assert "评奖" in verdict.reason

    def test_aaa_with_anime_tie_in_allowed(self):
        """欧美 3A 但明确是动画联动 → 放行（Cyberpunk × エッジランナーズ 这类）。"""
        verdict = judge_game_admission(
            "「サイバーパンク2077」，アニメ「エッジランナーズ 2」にあわせた大型無料アップデート"
        )
        assert verdict.allowed is True

    def test_collab_hardware_allowed_with_penalty(self):
        """硬件 × IP 联名周边：收，但降分（与商品宣传同档）。"""
        verdict = judge_game_admission("ポケモン×Logicool コラボマウスが発売", terms=("ポケモン",))
        assert verdict.allowed is True
        assert verdict.penalty == news_filters._GAME_COLLAB_PENALTY

    def test_magical_girl_genre_allowed(self):
        """「魔法少女」是 ACG 体裁词，作品没进库也放行（2026-10-23 用户点名）。"""
        verdict = judge_game_admission("新作魔法少女ゲーム『星のゆりかご』発表", terms=())
        assert verdict.allowed is True
        assert "魔法少女" in verdict.reason

    def test_magical_girl_ip_allowed_by_terms(self):
        """具体魔法少女 IP 靠词表命中（作品名 / 角色名都在词表里）。"""
        verdict = judge_game_admission("『プリキュア』新作ゲームが冬に発売", terms=("プリキュア",))
        assert verdict.allowed is True

    def test_doujin_game_allowed(self):
        """同人游戏放行：同人即 ACGN 原生形态（成人向由 is_adult 在展示层处理）。"""
        verdict = judge_game_admission("同人サークルが東方Projectの二次創作ゲームを頒布")
        assert verdict.allowed is True
        assert "同人游戏" in verdict.reason

    def test_doujin_event_allowed(self):
        verdict = judge_game_admission("コミティアで同人ゲーム新作を頒布、体験版も配布")
        assert verdict.allowed is True
        assert "同人游戏" in verdict.reason

    def test_doujin_does_not_rescue_aaa(self):
        """顺序约束：同人 / 独立词救不了 3A 稿（3A 判定在更前面）。"""
        verdict = judge_game_admission("「Call of Duty」がインディー風味の新モードを追加")
        assert verdict.allowed is False
        assert "3A" in verdict.reason

    def test_doujin_does_not_rescue_hardware(self):
        """顺序约束：硬件稿也不会因为出现「同人ゲーム」被放行。"""
        verdict = judge_game_admission("同人ゲーム向けのゲーミングマウスを発表")
        assert verdict.allowed is False
        assert "硬件" in verdict.reason


class TestTermMatching:
    def test_ascii_term_requires_word_boundary(self):
        """DOA 不能匹配 loadout 这种单词内部（ASCII 词条必须走词边界）。"""
        assert news_filters._term_in_text("DOA", "the loadout system is new") is False
        assert news_filters._term_in_text("DOA", "『DOA6』の新キャラ") is True

    def test_award_word_requires_word_boundary(self):
        """``tga`` 用纯子串会命中任何含这三个字母的单词——回放时把一条怪物题材 ADV
        误判成年度评奖，所以评奖/硬件/3A 三张表都改用词边界匹配。"""
        assert news_filters._hits_bounded(("tga",), "ストーリーのtga続きを描く") == []
        assert news_filters._hits_bounded(("tga",), "tga 2026 の受賞作が発表") == ["tga"]

    def test_cjk_term_is_substring(self):
        assert news_filters._term_in_text("セイバー", "セイバー新宿ver.") is True


class TestAcgnGameTerms:
    """白名单数据来源：标签进、3A 不进、人工别名永远保留。"""

    def test_manual_alias_kept_without_any_work(self, factory):
        db = factory()
        try:
            terms = crud.list_acgn_game_terms(db, manual_aliases=["アークナイツ"])
        finally:
            db.close()
        assert "アークナイツ" in terms

    def test_tagged_work_expands_to_title_and_character(self, factory):
        db = factory()
        try:
            galge = Content(title="Fate/stay night", type="game", summary="", rating=0.0)
            aaa = Content(title="Call of Duty 4", type="game", summary="", rating=0.0)
            db.add_all([galge, aaa])
            db.flush()
            tag = Tag(name="Galgame")
            db.add(tag)
            db.flush()
            db.add(ContentTag(content_id=galge.id, tag_id=tag.id))
            db.add(
                ContentCast(
                    content_id=galge.id, character="セイバー", actor="川澄綾子", sort_order=0
                )
            )
            db.commit()
            terms = crud.list_acgn_game_terms(db)
        finally:
            db.close()
        assert "Fate/stay night" in terms
        assert "セイバー" in terms
        # 3A 没有 ACG 标签、也不在人工表 → 标题与角色一条都不进
        assert all("call of duty" not in term.lower() for term in terms)

    def test_short_terms_dropped(self, factory):
        """短词条一律丢弃：单字角色名、两字作品名都会在正文里误匹配。

        这类短名要走人工 IP 表（不受长度限制，见下一个用例）。
        """
        db = factory()
        try:
            work = Content(title="月姫", type="game", summary="", rating=0.0)
            db.add(work)
            db.flush()
            tag = Tag(name="Galgame")
            db.add(tag)
            db.flush()
            db.add(ContentTag(content_id=work.id, tag_id=tag.id))
            db.add(
                ContentCast(content_id=work.id, character="凛", actor="", sort_order=0)
            )
            db.commit()
            terms = crud.list_acgn_game_terms(db)
        finally:
            db.close()
        assert "凛" not in terms
        assert "月姫" not in terms

    def test_manual_alias_bypasses_length_limit(self, factory):
        """人工表确认过的短 IP（月姫 / NieR）不受最短长度限制。"""
        db = factory()
        try:
            terms = crud.list_acgn_game_terms(db, manual_aliases=["月姫", "NieR"])
        finally:
            db.close()
        assert "月姫" in terms
        assert "NieR" in terms
