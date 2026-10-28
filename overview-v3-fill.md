# 收录口径 v3 落地 + 作品库填充 · 交付总结（2026-10-23）

## 一、需求（用户定稿）

作品库填充，游戏口径从「只收 galge」扩展为五类 ACGN 向；轻小说仍只收轻小说；
已上线的文学作品清理；以上写入今后所有补库规则。

## 二、收录口径 v3（已写入 `.workbuddy/memory/ingest.md`，适用于今后所有补库）

游戏 `is_acgn_game_payload()` 五类优先级（选品端与服务端总闸共用，唯一真相来源）：

| 分类 | 判定 | 本次放行 |
|---|---|---|
| galge / 视觉小说 | tags 命中 GALGAME_TAG_RE | 430 |
| 改编 / 联动 | 选品联网确认动画/漫画/**轻小说**原作关联（打 `_site_related_ip` 标记）或标签命中改编系 | 9 + 329 |
| 二游 / 动漫风 | 手游/二次元/美少女/乙女/恋爱/ADV 等标签 | 529 |
| 日式 RPG / 同人独立 | RPG/冒险/奇幻/同人/独立标签 **+ 日系信号**（假名/日式/日本） | 382 |
| 宽泛兜底 | 宽泛 ACGN 标签且 rank ≤ 1000 | 0（头部无漏网，符合预期） |

小说：只收轻小说（不变）。轻小说改编游戏（Fate/stay night、Steins;Gate 类）
算「有明确原作关联」——`has_anime_manga_relation` 已纳入 type=1 书籍。

## 三、代码改动

- `scripts/import_bangumi.py`：新增 `is_acgn_game_payload`，`map_subject` 游戏总闸改调它（+92 行）
- `scripts/select_from_archive.py`：`apply_ingest_policy` 先标签免联网预过滤、未命中再查关联；`request_interval` 防 429（+93 行）
- `scripts/select_new_works.py`：接入 v3 过滤，保留 `_site_related_ip` 标记落盘
- 新增 `scripts/ingest_selection.py`：服务器侧批量入库（免 ingest Key/Cookie，--limit/--offset 试跑续跑）
- 新增 `scripts/cleanup_literature.py`：文学清理（默认只统计，--apply 强制先备份）
- 测试：v3 口径锁定用例 + 清理脚本 4 例；**全量 1817 passed 无回归**

## 四、生产库执行结果

| 项 | 前 | 后 |
|---|---|---|
| 总量 | 17895 | **18828** |
| game | 1464 | **3582**（+2118，v3 主成果） |
| novel | 1226 | 1901（**全 light_novel**，literature 0） |
| anime / comic | 7894 / 1428 | 10917 / 2428 |

- 入库：6354 候选 → imported **1118** / duplicate 5236 / failed 0
  （duplicate 高因 existing_ids 拉于 10-20 推送前，头部重叠；已记入 ingest.md 教训）
- 文学清理：208 条世界文学（0 用户收藏、1 条浏览）已删，行级 + DB 双备份
- 封面回填：1180 新作品 → 1121 成功（95%），库内无封面仅剩 59 条
- 服务页面全 200；paste.rs 11 个副本已删（本地复核 404）；防火墙未动

## 五、备份与回滚

- DB：`/opt/acgn-test-platform/backups/acgn-before-v3ingest.db`（入库前）
- 文学行级：`/opt/acgn-test-platform/backups/literature_cleanup_20261023.json`（可回灌）
- 旧脚本：服务器 `scripts/import_bangumi.py.bak-v2`
- 回滚 = 停服换 DB 备份重启（全量部署路径②）

## 六、经验（已入记忆）

- paste.rs 匿名上限实测 **384KB**，超限**静默截断**成坏包——分片传 + 每片本地回读校 md5（deploy.md ④附）
- 拉 existing_ids 必须在上一轮推送之后（ingest.md 七之二）
- 今后补库（选品/档期/手动 run_sync）统一走 v3 口径
