"""资讯收录判定验证集（2026-10-21 用户定稿口径）。

固定验证集覆盖：明确无关内容、纯商业促销、游戏内实质活动、ACGN 音乐人动态、
无法关联作品库的有效资讯、来源白名单漏收，以及分类是否各归其位。
判定规则见 scripts/news_filters（三层：确定性规则 → 内容意图 → 低置信度转人工）。
"""

from __future__ import annotations

import pytest

from app.config import settings
from scripts import news_classify, news_filters
from scripts.news_filters import (
    REPEAT_SUMMARY_MIN_LEN,
    comprehensive_allow_group,
    has_acgn_signal,
    review_reason,
    should_skip_article,
    summary_prefix_key,
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


# ---------- 站方自有节目 / 编辑部内容（2026-10-24 用户定稿） ----------
# 口径：源的 RSS 里混发的播客 / 电台 / 会员节目不是资讯，按「链接形态 + 内容署名」排除。
# 真实样本来自 2026-10-24 抓取的机核 RSS：/radios/ 路径 + 「本期时间轴制作」署名。


class TestSourceOwnProgram:
    def test_radio_entry_skipped_by_url_shape(self):
        reason = should_skip_article(
            "有什么因为影视剧意外种草的东西？核市奇谭Vol.128",
            "本期时间轴制作：猪猪米\n双十一快到了，想聊聊最近被种草的",
            url="https://www.gcores.com/radios/221015",
        )
        assert reason and "站方自有节目" in reason

    def test_radio_entry_skipped_by_program_wording(self):
        # 链接形态改版时的兜底：靠节目自称 / 制作署名也能拦住
        reason = should_skip_article(
            "（爆音注意）北京核聚变前夜实录，录音笔VOL.810",
            "【录音笔】是GPASS推出的一档会员专享节目，内容来自机核办公室日程生活",
        )
        assert reason and "站方自有节目" in reason

    def test_normal_article_not_affected(self):
        # 同站正常文章：/articles/ 路径 + 正经资讯文案，不能误伤
        assert should_skip_article(
            "《女神异闻录４ Revival》“伊格尔”“玛格丽特”介绍公开",
            "世嘉宣布，ATLUS新作RPG的登场角色公开",
            url="https://www.gcores.com/articles/221407",
        ) is None
        # 标题里带「Vol.」的普通文章也不受影响（只认 /radios/ 与节目署名）
        assert should_skip_article(
            "《某杂志》Vol.12 发售", "本期封面是新作特辑。", url="https://example.com/a/1"
        ) is None


# ---------- 源站自营内容（2026-10-25 用户定稿「网站的自营信息我们不要」）----------
# 与「站方自有节目」同层（判的都是「这条不是资讯」），区别是自营**按来源键分表**：
# 别的源报道同一个活动不算自营，仍按内容判定。


class TestSourceSelfOperation:
    def test_self_run_event_series_skipped(self):
        # 机核自办展会「核聚变」系列（用户 2026-10-25 点名不要的三种东西之一）
        for title in (
            "「更新直播页」核电波Live｜2026北京核聚变舞台活动",
            "冠军键盘正式亮相｜IQUNIX 参展 2026 北京核聚变",
            "核聚变2026北京站云逛展指南",
            "一路向西丨核聚变2026北京站入场指南",
        ):
            reason = should_skip_article(
                title, "", "游戏", "媒体编译", source_key="gcores"
            )
            assert reason and "自营" in reason, title

    def test_self_run_music_event_skipped(self):
        # GGG = 机核的音乐活动。ASCII 品牌名右边直接接中文（「GGG完整曲目」），
        # 词表用的双边界规则会把它漏掉——自营词只要求左侧边界。
        assert should_skip_article(
            "19个游戏IP的旋律，同一个音乐现场｜GGG完整曲目与阵容公开",
            "", "综合动态", "媒体编译", source_key="gcores",
        )
        assert should_skip_article(
            "来GGG游戏节奏联欢夜的朋友，看看入场指引！",
            "", "游戏", "媒体编译", source_key="gcores",
        )

    def test_other_source_reporting_same_event_kept(self):
        # 分源词表的意义：同一个活动由别的源报道时不是自营
        assert should_skip_article(
            "核聚变参展情报汇总", "", "游戏", "媒体编译", source_key="other"
        ) is None
        assert should_skip_article(
            "GGG游戏节奏联欢夜 现场报道", "", "游戏", "媒体编译", source_key="other"
        ) is None

    def test_ascii_brand_not_matched_inside_word(self):
        assert should_skip_article(
            "diggging into the game", "", "游戏", "媒体编译", source_key="gcores"
        ) is None


# ---------- 周报 / 行业综述栏目（2026-10-25）----------


class TestWeeklyDigest:
    def test_weekly_digest_skipped(self):
        for title in (
            "触乐本周行业大事：《王者荣耀》中国队名古屋亚运会卫冕，《三角洲行动》官宣日活破5800万",
            "触乐本周行业大事：小鸟播报！《塞尔达传说：时之笛》重制版公开新预告",
        ):
            reason = should_skip_article(
                title, "", "游戏", "媒体编译", source_key="chuapp"
            )
            assert reason and "周报" in reason, title

    def test_weekly_digest_skipped_even_if_looks_like_announcement(self):
        # 周报把一周新闻堆在标题里，里面的「重制版…公开」会被判成官宣（实测
        # major=2「重制+公开」）走豁免入库——周报规则必须能盖住它。
        title = (
            "触乐本周行业大事：小鸟播报！《塞尔达传说：时之笛》重制版公开新预告，"
            "育碧拟关闭“特技摩托”系列开发工作室"
        )
        assert news_classify.is_major_announcement(title, "")
        reason = should_skip_article(title, "", "游戏", "媒体编译", source_key="chuapp")
        assert reason and "周报" in reason

    def test_not_affected_outside_media_game(self):
        # 只认「媒体源的遊戲類」：官方 PR 与其它类别的既有判定不变
        assert should_skip_article(
            "本周行业大事汇总", "", "游戏", "官方PR", source_key="aniplex"
        ) is None
        assert should_skip_article(
            "本周行业大事汇总", "", "动画", "媒体编译", source_key="chuapp"
        ) is None


# ---------- 普通游戏的联名周边（2026-10-25 用户「普通游戏的联名不在乎」）----------


class TestCollabMerch:
    def test_non_acg_collab_merch_skipped(self):
        reason = should_skip_article(
            "致敬《古墓丽影》三十周年，八位堂两款联名纪念手柄全球上市",
            "", "游戏", "媒体编译", source_key="gcores",
        )
        assert reason and "普通游戏的联名周边" in reason

    def test_generic_role_word_is_not_acg_evidence(self):
        # 线上真实摘要：满篇都在说「角色」，但那是普通游戏里的角色，
        # 拿泛词当 ACG 证据会让这条规则直接失效（收录时这条就是这么漏进来的）。
        summary = (
            "1996年，《古墓丽影》正式诞生。对于很多玩家而言，她不是游戏里一个"
            "虚拟的角色，而是陪伴自己成长的老朋友。"
        )
        reason = should_skip_article(
            "致敬《古墓丽影》三十周年，八位堂两款联名纪念手柄全球上市",
            summary, "游戏", "媒体编译", source_key="gcores",
        )
        assert reason and "普通游戏的联名周边" in reason

    def test_acg_ip_collab_kept(self):
        # 站内 ACGN 游戏词表命中 = 被联名的是 ACG IP，不按普通游戏拒
        assert should_skip_article(
            "《原神》×罗森 联名周边10月28日发售", "", "游戏", "媒体编译",
            source_key="gcores", game_terms=["原神"],
        ) is None

    def test_collab_content_without_goods_kept(self):
        # 游戏内联动是内容不是周边：没有实物形态词，不触发
        assert should_skip_article(
            "《恋与深空》联动《银翼杀手2049》：为何用这个故事来表达“爱即真实”？",
            "", "游戏", "媒体编译", source_key="chuapp",
        ) is None

    def test_not_affected_outside_media_game(self):
        assert should_skip_article(
            "《古墓丽影》三十周年 联名纪念手柄 上市", "", "游戏", "官方PR",
            source_key="aniplex",
        ) is None


# ---------- 中文抽奖导流（2026-10-24 补）：机核「【会员抽奖】」这类自家会员福利稿 ----------


class TestChineseGiveaway:
    def test_membership_lottery_skipped(self):
        reason = should_skip_article(
            "【会员抽奖】特别好评奇幻巫师冒险游戏《巫塔战记》“沉睡之力”更新上线", ""
        )
        assert reason and "抽奖" in reason

    def test_game_update_without_lottery_not_affected(self):
        # 同一批的正常更新稿：标题没有抽奖词，不能被误伤
        assert should_skip_article(
            "《超英派遣中心》发售一周年庆典开启", ""
        ) is None
        # 游戏内的抽卡系统不是导流：只有「抽奖」没有参与动作词
        assert should_skip_article("新角色抽奖系统上线，概率公开", "") is None


# ---------- 复制粘贴的通稿引言（2026-10-24 用户定稿） ----------


class TestRepeatedSummary:
    def test_same_boilerplate_shares_fingerprint(self):
        # 通稿引言本身长于指纹长度（40 字），后面的厂商名 / 引导语才不同
        head = (
            "核聚变游戏嘉年华北京站即将开幕，热门新游试玩、精彩舞台活动、限定周边收集，"
            "现场还有舞台演出与限定周边贩售"
        )
        assert summary_prefix_key(head + "，详情见官网") == summary_prefix_key(head + "，更多情报公开")

    def test_different_leads_differ(self):
        assert summary_prefix_key("世嘉宣布，ATLUS新作RPG的登场角色公开") != summary_prefix_key(
            "由Paradow Environment开发，NiuGamer发行的国产新游"
        )

    def test_short_summary_not_compared(self):
        # 摘要太短不参与去重（「官方宣布」这类短前缀撞车不代表是通稿）
        assert len(summary_prefix_key("官方宣布")) < REPEAT_SUMMARY_MIN_LEN


# ---------- 固定栏目闲聊（标题前缀，2026-10-21 触乐接入时新增） ----------


class TestColumnChatPrefix:
    def test_chuapp_columns_skipped(self):
        assert should_skip_article("触乐怪话：在游戏里，为什么必须用钥匙开锁", "编辑随笔。")
        assert should_skip_article("触乐夜话：购买了游戏圈的未来", "编辑随笔。")

    def test_prefix_only_matches_title_head(self):
        # 栏目名出现在标题中部 / 正文不算（前缀匹配，零误伤）
        assert not should_skip_article("访谈：聊聊「触乐怪话」这档栏目", "触乐怪话是栏目。")
        assert not should_skip_article("触乐本周行业大事：塞尔达重制公开", "行业周报。")


# ---------- 第 N 组：综合资讯放行分组（2026-10-23 用户定稿）----------
# 口径：综合动态里的日系音乐 / 日偶 / VTuber 放行，且不再走商业促销层；
# 其它类别（动画/漫画/轻小说/游戏）与硬广完全不受影响。

class TestComprehensiveAllowGroups:
    @pytest.mark.parametrize("title,group", [
        ("YOASOBIのニューアルバム発売記念グッズを販売", "jmusic"),
        ("乃木坂46の新シングル発売を記念したグッズを販売", "idol"),
        ("にじさんじの新ユニットデビュー記念グッズを販売", "vtb"),
        ("「葬送のフリーレン」のEDテーマ、アニソン大賞を受賞", "anisong"),
    ])
    def test_group_is_detected(self, title, group):
        assert comprehensive_allow_group(title) == group

    def test_comprehensive_category_keeps_promo_heavy_news(self):
        """这些稿子按普通综合稿会被「商业促销」拒掉，综合放行后保留。"""
        title = "乃木坂46の新シングル発売を記念したグッズを販売"
        assert should_skip_article(title, "") is not None  # 不放行时确实被拒
        assert should_skip_article(title, "", "综合动态") is None

    def test_other_categories_unaffected(self):
        """动画类别同一条稿子仍按促销排除——放行只对综合动态生效。"""
        title = "乃木坂46の新シングル発売を記念したグッズを販売"
        reason = should_skip_article(title, "", "动画")
        assert reason is not None
        assert "促销" in reason

    def test_hard_ad_still_blocked(self):
        """硬广优先于放行分组：赞助稿即使聊 VTuber 也照排。"""
        reason = should_skip_article("ホロライブの新グッズを販売【PR】", "", "综合动态")
        assert reason is not None
        assert "硬广" in reason

    def test_group_blocker_disables_group(self, monkeypatch):
        """分组级排除词是以后的细化入口：配上就对该组失效，不影响其它组。"""
        monkeypatch.setitem(
            news_filters._COMPREHENSIVE_GROUP_BLOCKERS, "idol", ("グッズ",)
        )
        assert comprehensive_allow_group("乃木坂46の新シングル発売記念グッズを販売") != "idol"

    def test_config_can_disable_group(self, monkeypatch):
        """关掉某组（改环境变量）就回到旧行为，不用改代码。"""
        monkeypatch.setattr(settings, "news_comprehensive_allow_groups", "anisong")
        title = "にじさんじの新ユニットデビュー記念グッズを販売"
        assert should_skip_article(title, "", "综合动态") is not None

    def test_unknown_group_key_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "news_comprehensive_allow_groups", "nope,vtb")
        assert news_filters.enabled_comprehensive_groups() == ("vtb",)

    def test_blocker_table_covers_every_group(self):
        """扩展契约：新加分组必须同时在排除词表里占位（写成空元组即可）。"""
        assert set(news_filters._COMPREHENSIVE_GROUP_BLOCKERS) == set(
            news_filters.COMPREHENSIVE_ALLOW_GROUPS
        )
