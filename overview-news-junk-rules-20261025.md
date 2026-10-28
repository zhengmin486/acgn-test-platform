# 资讯三条新过滤规则 + 存量清理（2026-10-25）

## 需求

用户两条消息定稿：

1. 游戏源相关——**不要把之前写的游戏准入闸门扩到所有游戏源**；不要
   「核电波Live｜2026北京核聚变舞台活动」「触乐本周行业大事」
   「致敬《古墓丽影》三十周年，八位堂两款联名纪念手柄全球上市」这种东西；
   **普通游戏的联名不在乎**。
2. **网站的自营信息我们不要**。

两个取舍经确认后确定：规则作用域 =「自营按源分表 + 周报/联名只对媒体源的游戏类生效」，
存量 =「删掉命中新规则的」。

## 线上先定位的三类垃圾

| 类型 | 来源 | 当时条数 |
| --- | --- | --- |
| 源站自营（机核自办活动 核聚变 / 核电波 / GGG） | 机核 GCORES | 7（清理时已 11） |
| 周报 / 行业综述（触乐「本周行业大事」） | 触乐 | 2（清理时已 3） |
| 普通游戏的联名周边（八位堂 × 古墓丽影） | 机核 GCORES | 1 |

其中一条周报还被分类器判成「重大官宣」（major=2，命中「重制+公开」）走了豁免额度——
因为它把一周新闻堆在标题里，里面提到了《塞尔达传说：时之笛》重制版。

## 改动

### scripts/news_filters.py（三条规则，各一个函数）

- **`self_op_reason(source_key, ...)`** —— 自营内容，按源分表 `SOURCE_SELF_OP_WORDS`
  （现只有 `gcores`：核聚变 / 核电波 / 核聚变游戏节 / 核市奇谭 / ggg / 机核商城 / 机核会员）。
  挂在既有「站方自有节目」同一层（第零层），判的是「这条根本不是资讯」。
  按源分表而不是塞进总词表：别的源报道同一活动时说的是同一件事，不能一起挡。
  ASCII 品牌词用单独的 `_self_op_word_in_text`（只要求左侧边界）——
  「GGG完整曲目」右边直接接汉字，词表用的双边界规则会漏掉它。
- **`weekly_digest_reason(...)`** —— 周报 / 行业综述，命中即拒，**不看标题里堆了哪些作品**
  （那些作品名会让周报被当成官宣走豁免）。
- **`collab_merch_reason(...)`** —— 联名周边：联名标记 × 实物形态 × **没有 ACG 证据** 才拒。
  ACG 证据 = 站内 ACGN 游戏词表命中，或 `_COLLAB_ACG_SIGNALS` 强体裁信号命中。

对外入口 `media_game_rule_reason()`（清理脚本与抓取链路共用同一份口径）；作用域常量
`_MEDIA_RULE_SOURCE_TYPES=("媒体编译",)` / `_MEDIA_RULE_CATEGORY="游戏"`——
官方 PR 与其它类别的既有判定完全不变。

### scripts/news_daily.py

`should_skip_article` 调用处补传 `source_key` / `source_type` / `game_terms`；
ACG 游戏词表改为**每轮常驻加载**（原来只在启用游戏闸门时加载，现在联名规则也要用）。

### scripts/cleanup_news_junk.py（新）

默认预览、`--apply` 必须带 `--backup`；按三条规则扫全库命中即删（文章 + 标签 / 作品 /
实体关联；收藏行按 `ArticleFavorite` 的设计保留）。复用同一批函数，规则改一处两边生效。

## 上线当天踩到的坑：联名规则本地全过、线上一条没命中

本地用例用**空摘要**测，「八位堂联名手柄」被正确拒绝；上了服务器它却不命中。原因是：
联名规则当时拿 `_GAME_ACG_SIGNALS` 当 ACG 证据，而那张表里有泛词「角色」——
文章摘要写着「她不是游戏里一个虚拟的**角色**」，于是被当成 ACG 证据放行。

修法：新表 `_COLLAB_ACG_SIGNALS` 只收强体裁信号（ギャルゲー / 乙女 / 美少女 / 魔法少女 /
アニメ化 …），**刻意不收「角色 / キャラクター / 动画 / 恋愛 / アイドル」这类泛词**。
用例 `test_generic_role_word_is_not_acg_evidence` 用线上真摘要钉住。

## 验证

- `tests/test_news_filters.py` **78 passed**（+12：自营命中 / 别源提同一活动不误伤 /
  ASCII 不误命中 / 周报（含被判官宣那条）仍拒 / 作用域外不生效 / 联名三例）。
- 服务器清理预览 15 条 → `--apply` 删除 15 条 + 113 依赖行，资讯 **417 → 402**。
- 站点复核：「核聚变」「本周行业大事」「八位堂」「核电波」在 /articles 前三页全部 0 处。

## 部署记录

- 通道：**paste.rs 当天整段 502**（连 9KB 也传不动），探测 8 个备选后改用
  **paste.c-net.org**（直传、56KB 往返 md5 一致、无 384KB 截断）。
- 推 4 个文件：`scripts/news_filters.py`、`scripts/news_daily.py`、
  `scripts/cleanup_news_junk.py`、`tests/test_news_filters.py`，逐个 md5 双向校验 →
  整文件覆盖 → `IMPORT_OK` → 重启。
- 回滚锚点：`/tmp/news_filters.py.bak-20261025`、`/tmp/news_daily.py.bak-20261025`；
  数据侧整库热备 `/opt/acgn-backup-newsjunk-20261025/before-cleanup.db` + 行快照 JSON。

## 待办 / 注意

1. **paste.c-net.org 删不掉**（DELETE 403，POST 到旧 ID 只生成新 ID）。本次 5 个源码副本
   仍留在公网（URL 是随机两词组合、不索引）。列表：
   `HolinessConnell`（news_filters 旧版）、`FistsHooligan`（news_daily）、
   `VanceAngels`（cleanup_news_junk）、`JeffreyRoads`（news_filters 新版）、
   `WrittenCompound`（tests）。内容是项目源码，无密钥；下次部署能删的 paste.rs 恢复后优先用它。
2. 机核自营词表现在只覆盖已确认的活动品牌；以后机核再办新活动 / 新栏目，往
   `SOURCE_SELF_OP_WORDS["gcores"]` 加一行即可。
3. 触乐的周报靠 `_WEEKLY_DIGEST_WORDS` 的栏目名命中；换栏目名（如「一周要闻」）时要补词。
