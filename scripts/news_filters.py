"""资讯内容过滤：只把「最露骨广告」挡在入库之前。

与用户定稿的口径一致：
- **只挡最露骨广告**：标题或摘要命中硬广关键词（中日英三语）即跳过；
- **营销活动保留**：联动 / 周边 / 活动 / 抽选 / 预售票等（キャンペーン / コラボ /
  グッズ / 発売記念 / 舞台挨拶 / 抽選 / プレゼント / 応募 / 先行予約 / ムビチケ /
  イベント / 投票）一律不在此处过滤；
- **galge / eroge 不挡**：Galgame 是游戏内最高优先级，成人内容只在展示层按全局开关
  隐藏图片与露骨摘要（见 ``news_classify.flag_adult``），**不在入库阶段删除**；
- **二游不挡**：日本二游与国产二游同权。

本模块不依赖其它脚本（避免 news_sources ↔ import_articles 循环导入），两条管线都从
这里 import ``should_skip_article``。
"""

from __future__ import annotations

# 最露骨广告关键词：命中标题或摘要即跳过。均为明确的广告 / 赞助 / 软广标记，
# 不会在正常编辑性报道里出现（营销活动词不在此列）。
HARD_AD_KEYWORDS: tuple[str, ...] = (
    # 中文
    "推广", "赞助", "广告", "商务合作", "软文",
    # 日文
    "広告", "タイアップ", "スポンサー", "ステマ", "アフィリエイト",
    # 英文
    "sponsored", "advertorial",
)


def should_skip_article(title: str, summary: str, category: str = "") -> str | None:
    """返回跳过原因（命中则跳过该条），未命中返回 None。

    ``category`` 预留给后续按栏目细化的规则，当前不参与判定（成人向不在入库阶段过滤）。
    """
    text = f"{title}\n{summary}".lower()
    for keyword in HARD_AD_KEYWORDS:
        if keyword.lower() in text:
            return f"硬广关键词命中：{keyword}"
    return None
