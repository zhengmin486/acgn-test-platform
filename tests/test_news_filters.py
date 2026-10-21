"""资讯收录判定验证集（2026-10-21 用户定稿口径）。

固定验证集覆盖：明确无关内容、纯商业促销、游戏内实质活动、ACGN 音乐人动态、
无法关联作品库的有效资讯、来源白名单漏收，以及分类是否各归其位。
判定规则见 scripts/news_filters（三层：确定性规则 → 内容意图 → 低置信度转人工）。
"""

from __future__ import annotations

import pytest

from scripts import news_classify
from scripts.news_filters import (
    has_acgn_signal,
    review_reason,
    should_skip_article,
)


# ---------- 第 1 组：正常作品动态应保留 ----------

class TestKeepNormalWorksNews:
    @pytest.mark.parametrize("title,summary", [
        ("新作アニメ「ケロロ軍曹☆」制作決定、2027年放送", "制作会社とキャストを発表。"),
        ("アニメ「ホテル・インヒューマンズ」第2期の放送が決定", "2027年1月より放送。"),
        ("漫画「ワンピース」次号、連載再開", "週刊少年ジャンプで掲載。"),
        ("ライトノベル「薬屋のひとりごと」最新巻の発売日が決定", "単行本と同時発売。"),
        ("「アズールプロミリア」大型アップデート配信、新マップと新キャラを追加", "Ver.2.0として配布。"),
        ("「モンスターハンターパズル」サービス終了へ、買い切り版を販売", "2027年1月14日をもって終了。"),
        ("アニメ「日本三國」新キャスト発表、監督とスタッフも公開", "制作会社が発表。"),
        ("俳優・声優の土田大さんが54歳で逝去", "「カーズ」マックィーンなど。"),
    ])
    def test_normal_news_kept(self, title, summary):
        assert should_skip_article(title, summary) is None


# ---------- 第 2 组：ACGN 音乐人 / 文化动态（关联不到具体作品）----------

class TestKeepMusicAndCultureNews:
    @pytest.mark.parametrize("title,summary", [
        ("Ado、新曲「吟」を配信リリース。ツアー開催も決定", "新アルバムの発売も控える。"),
        ("ヨルシカ、初の海外ライブを開催。セットリストを公開", "音楽フェスにも出演。"),
        ("19个游戏IP的旋律，同一个音乐现场｜GGG完整曲目与阵容公开", "オーケストラコンサート。"),
        ("麻枝准确诊咽癌，确认不进行积极治疗", "作曲家・脚本家として活動。"),
    ])
    def test_music_and_creator_news_kept(self, title, summary):
        """音乐人 / 创作者动态即使关联不到具体作品也应收录。"""
        assert should_skip_article(title, summary) is None

    def test_music_news_without_work_link_goes_to_general(self):
        """关联不到作品的音乐人动态归「综合」，不是丢弃。"""
        subject = news_classify.classify_subject_from_text(
            "Ado、新曲「吟」を配信リリース", "ツアー開催も決定", fallback="动画"
        )
        assert subject == news_classify.SUBJECT_GENERAL


# ---------- 第 3 组：商业联名促销应排除（即便涉及 ACGN IP）----------

class TestExcludeCommercialCollab:
    @pytest.mark.parametrize("title,summary", [
        # ポケモン×ミスド：主要内容是餐饮联名 + 先行预约
        (
            "ポケモンとミスドのコラボキャンペーン「みんなで集合！冬のポケモンキャンペーン」，先行予約を10月27日より順次スタート",
            "全国のミスタードーナツで開催。ミスドポケモンドーナツやグッズが当たる。",
        ),
        # ハローキティ×吉野家：联名餐品开售
        (
            "「サンリオ」ハローキティ&妹ミミィが双子コーデ♪ 「吉野家」コラボどんぶりが販売開始",
            "吉野家各店で販売。",
        ),
        # アベイル：服饰店周边发售
        (
            "おっきな刺繍のハンギョドンが可愛すぎ！アベイルで「ハンギョドン」グッズが10月24日発売",
            "アベイル各店舗にて。",
        ),
        # Maison de FLEUR：包袋配饰登场
        (
            "ハローキティ＆タイニーチャムが「Maison de FLEUR」とコラボ！ ギンガムチェックのリボンがかわいいバッグ＆チャーム登場",
            "バッグとチャームをラインアップ。",
        ),
        # サンキューマート：定价销售
        (
            "「はぴだんぶい」がピースッ☆ 靴下やクリカ、Tシャツキーホルダーなど全部390円！ サンキューマート新商品",
            "全国のサンキューマートで発売。",
        ),
    ])
    def test_commercial_collab_excluded(self, title, summary):
        reason = should_skip_article(title, summary)
        assert reason is not None
        assert "商业" in reason


# ---------- 第 4 组：游戏内实质内容应保留（边界案例）----------

class TestKeepInGameContent:
    def test_madoka_anniversary_event_kept(self):
        """madoka 1.5 周年：主要内容是游戏内限定角色，保留。"""
        title = (
            "『魔法少女まどか☆マギカ Magia Exedra』、まどドラ1.5周年第2弾として，"
            "新限定★5キオク「美樹さやか」が登場"
        )
        summary = "1.5周年記念キャンペーンを開催。★5キオクがガチャに実装。"
        assert should_skip_article(title, summary) is None

    def test_collab_event_with_in_game_rewards_kept(self):
        """放置少女×生徒会：主要内容是游戏内联名活动与限定道具，保留。"""
        title = (
            "ゲーム「放置少女」×アニメ「生徒会にも穴はある！」コラボ決定！"
            "10月27日開催＆事前予約イベで限定ガチャ券"
        )
        summary = "コラボイベントを開催。ログインボーナスも配布。"
        assert should_skip_article(title, summary) is None

    def test_in_game_content_goes_to_game_category(self):
        title = "『魔法少女まどか☆マギカ Magia Exedra』、まどドラ1.5周年第2弾として，新限定★5キオク「美樹さやか」が登場"
        subject = news_classify.classify_subject_from_text(
            title, "1.5周年記念キャンペーン。", fallback="动画"
        )
        assert subject == news_classify.SUBJECT_GAME


# ---------- 第 5/6 组：无法关联作品库 / 未进白名单 ----------

class TestNoWhitelistGate:
    def test_unknown_studio_game_kept(self):
        """作品库没有、也没进任何白名单：只要本身是游戏动态就收录。"""
        title = "巨大な宇宙クジラの背中に街を築くSFシティビルダー「Beyond These Stars」配信開始"
        assert should_skip_article(title, "生態系と産業の発展を描く。") is None

    def test_unrelated_to_library_kept(self):
        title = "「ファイナルファンタジー レゾナンス」はHD-2D演出がすごいだけじゃない。バトルの駆け引きやキャラとパーティの"
        assert should_skip_article(title, "スクウェア・エニックスの新作。") is None

    def test_source_label_is_ignored(self):
        """来源标签不再参与判定：同一个标题在任何源下结论一致。"""
        title = "ポケモンとミスドのコラボキャンペーン「みんなで集合！冬のポケモンキャンペーン」，先行予約を10月27日より順次スタート"
        summary = "全国のミスタードーナツで開催。"
        assert should_skip_article(title, summary, source_label="4Gamer") is not None
        assert should_skip_article(title, summary, source_label="アニメ！アニメ！") is not None
        assert should_skip_article(title, summary, source_type="官方PR") is not None


# ---------- 第 7 组：应排除的各类内容 ----------

class TestExcludeClearlyIrrelevant:
    def test_live_action_drama_excluded(self):
        """纯实写剧（无 ACGN 信号）排除。"""
        title = "実写ドラマ「初恋、ざらり」の配信はどこで見れる？見放題・レンタルを比較"
        assert should_skip_article(title, "実写ドラマ化作品。") is not None

    def test_live_action_adaptation_of_anime_kept(self):
        """ACGN 作品被実写化本身是 ACGN 新闻，不能因为出现「実写」就排除。"""
        title = "人気アニメ「xxx」の実写化が決定、2027年に映画公開"
        assert should_skip_article(title, "アニメの実写映画化。") is None

    def test_consumer_tech_news_excluded(self):
        title = "库克又去了米哈游：一部折叠屏 iPhone，还有一些值得琢磨的见闻"
        assert should_skip_article(title, "Apple 的新机传闻。") is not None

    def test_hard_ad_excluded(self):
        assert should_skip_article("【PR】新製品のご案内", "") is not None
        assert should_skip_article("通常記事", "本記事はスポンサー提供です") is not None

    def test_giveaway_excluded(self):
        title = "XREALとMMORPG「AION2」がコラボキャンペーンを実施。Xの投稿リポストでビデオグラス「ROG XREAL R1」が抽選で1名に当たる"
        assert should_skip_article(title, "抽選で1名に当たる。") is not None

    @pytest.mark.parametrize("title", [
        "アニメ「日本三國」の配信はどこ？見放題で見れるサブスクを比較",
        "「kiss×sis OAD」の配信はどこで見れる？見放題のサブスクを比較",
        "映画「ルパン三世 不死身の血族」の配信はどこで見れる？見放題サービスや声優キャストも紹介",
    ])
    def test_vod_funnel_excluded(self, title):
        assert should_skip_article(title, "") is not None

    def test_official_streaming_launch_kept(self):
        """官方「見放題配信開始」是上线公告，不含导流词，不误伤。"""
        title = "「北極百貨店のコンシェルジュさん」各配信サービスにて見放題配信開始！"
        assert should_skip_article(title, "") is None

    def test_merch_release_excluded_even_for_acgn_ip(self):
        """ACGN IP 的周边发售同样排除（用户行动目的是购买）。"""
        title = "「ワンピース」新作一番くじ全ラインナップ公開！ラストワン賞は「カルー」ビッグぬいぐるみ"
        assert should_skip_article(title, "全国のコンビニで予約受付。") is not None

    def test_theatrical_attendee_bonus_kept(self):
        """入場者特典（看电影送的）是作品上映动态，不是卖周边 → 保留（修 DROP#211 误伤）。"""
        title = "「超かぐや姫！」入場者特典第6弾は「ray」場面写真ポストカード！ 復活上映は11月5日まで"
        assert should_skip_article(title, "") is None

    def test_purchase_bonus_still_excluded(self):
        """購入特典（买 BD 送的）仍属购买附赠 → 排除，不被观影特典豁免误伤。"""
        title = "Blu-ray 購入特典は「カルー」ビッグぬいぐるみ！ 全国のショップで予約受付"
        assert should_skip_article(title, "") is not None


# ---------- 第三层：低置信度不丢弃 ----------

class TestReviewNotDrop:
    def test_channel_signal_without_goods_goes_to_review(self):
        """出现商业渠道但看不出在卖实物 → 待复核，不丢弃。"""
        title = "ビックカメラとのコラボが決定！10/14スタート！"
        reason = review_reason(title, "全国のビックカメラで開催。")
        assert reason is not None
        assert should_skip_article(title, "全国のビックカメラで開催。") is None

    def test_plain_news_no_review(self):
        assert review_reason("新作アニメ制作決定", "2027年放送。") is None


# ---------- 第 8 组：分类各归其位 ----------

class TestSubjectClassification:
    @pytest.mark.parametrize("title,summary,expected", [
        ("アニメ「ホテル・インヒューマンズ」第2期の放送が決定", "2027年1月より放送。", news_classify.SUBJECT_ANIME),
        ("漫画「ワンピース」次号、連載再開", "週刊少年ジャンプで掲載。", news_classify.SUBJECT_MANGA),
        ("ライトノベル「薬屋のひとりごと」最新巻の発売日が決定", "単行本と同時。", news_classify.SUBJECT_LN),
        ("「アズールプロミリア」大型アップデート配信、新マップを追加", "Ver.2.0。", news_classify.SUBJECT_GAME),
        ("Ado、新曲「吟」を配信リリース", "ツアー開催も決定。", news_classify.SUBJECT_GENERAL),
    ])
    def test_subject_by_content_not_source(self, title, summary, expected):
        """分类依据资讯实际内容，源栏目只作平分时的参考。"""
        subject = news_classify.classify_subject_from_text(
            title, summary, fallback=news_classify.SUBJECT_GENERAL
        )
        assert subject == expected

    def test_source_category_no_longer_forces_subject(self):
        """源栏目写「动画」但内容明显是漫画 → 归漫画（修分类空转）。"""
        subject = news_classify.classify_subject_from_text(
            "漫画「ワンピース」次号、連載再開", "週刊少年ジャンプで掲載。", fallback="动画"
        )
        assert subject == news_classify.SUBJECT_MANGA

    def test_acgn_signal_helper(self):
        assert has_acgn_signal("新作アニメ制作決定", "")
        assert not has_acgn_signal("折叠屏 iPhone 的发售日", "")


# ---------- 固定栏目闲聊（标题前缀，2026-10-21 触乐接入时新增） ----------


class TestColumnChatPrefix:
    def test_chuapp_columns_skipped(self):
        assert should_skip_article("触乐怪话：在游戏里，为什么必须用钥匙开锁", "编辑随笔。")
        assert should_skip_article("触乐夜话：购买了游戏圈的未来", "编辑随笔。")

    def test_prefix_only_matches_title_head(self):
        # 栏目名出现在标题中部 / 正文不算（前缀匹配，零误伤）
        assert not should_skip_article("访谈：聊聊「触乐怪话」这档栏目", "触乐怪话是栏目。")
        assert not should_skip_article("触乐本周行业大事：塞尔达重制公开", "行业周报。")
