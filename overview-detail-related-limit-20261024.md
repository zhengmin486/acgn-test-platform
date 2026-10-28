# 详情页「相关内容推荐」上限 6 → 12（2026-10-24）

## 问题

用户反馈「推荐作品好像有 6 个的上限，这在 PC 上不好」。

**定位过程**：先拉线上首页核验——首页「推荐作品」实际是 **100 条满铺**
（90 张带 `home-top-extra` 由 PC 端 CSS 强制铺开，模板 + 后端均正常），排除。
全库扫描后确认真正的 6 上限在**作品详情页「相关内容推荐」**：

- `crud.list_related_contents_with_reasons(db, content, limit: int = 6, ...)`
  默认值 6，详情页调用点（`app/business_pages.py:925`）没有显式传 limit；
- 该区块是海报网格 `.detail-poster-grid`（`repeat(auto-fill, minmax(150px, 1fr))`，
  外层为标准 Bootstrap container），PC 宽屏下 6 张只铺一行、末行空缺，版面发虚。

## 改动

| 文件 | 改动 |
| --- | --- |
| `app/crud.py` | `list_related_contents_with_reasons` 默认 `limit` 6 → **12**，docstring 补列数推算与取舍理由；薄封装 `list_related_contents` 默认值同步对齐（生产无调用，仅测试在用，避免双默认不一致埋坑） |
| `tests/test_content_relations.py` | 新增 `test_default_limit_is_twelve_for_pc_grid`：15 个同原作候选、不传 limit，钉住默认返回 12 条且理由全部可追溯；文件头覆盖点同步更新 |

**为什么是 12**：海报网格 PC 各断点实测 **5 列（lg 936px）/ 6 列（xl 1116px）/ 7 列（xxl 1296px）**，手机 2 列、平板（md）4 列。12 条在最常见的 6 列宽正好排满 2 行；7 列末行 5/7、5 列末行 2/5 是已知取舍（与首页 `HOME_RECOMMEND_MAX=100` 同一哲学：不凑整除、不硬填）。

**行为不变**：三层召回口径（实体关系 → IDF 题材 → 同类型榜单兜底）、exclude_ids 排除系列区、「候选不足就少给几条、不强行填满」全部保留。

## 验证

- `tests/test_content_relations.py`：**17 passed**（原 16 + 新增 1）；
  另跑 `test_home_recommend_user.py` + `test_home_posters.py`：26 passed（首页路径无回归）。
- 线上部署（paste.rs 单片 `app/crud.py`，280,514 B，上传/下载 **md5 双向一致**
  `0c953a90…`）→ 覆盖 → `grep` 确认两处 `limit: int = 12` 落盘 →
  `import app.main` 预检 IMPORT_OK → `systemctl restart acgn`（active）。
- 线上抽样 8 个详情页（魔卡少女樱 / 凉宫春日 / 轻音少女 / 小圆 / 孤独摇滚 /
  点兔 / Keroro / 七叶树王国），「相关内容推荐」**全部渲染 12 张海报**。
- paste.rs 公开副本已删（GET 复核 404）；本地临时文件已清。

## 回滚

服务器旧版备份：`/tmp/crud.py.bak-20261024`（md5 `6a8c6fcfbc71b15062855dac5a6a6df2`）。
如需回滚：覆盖回该文件 → `import app.main` 预检 → `systemctl restart acgn`。

## 备注

- 未提交 git（工作树本就挂着大量 v3 未提交改动，按既往惯例不主动 commit/push）。
- 若后续想要 7 列宽（≥1400px）也排满，可把上限抬到 14；要 5/6/7 列全满则需 210，
   不现实，维持当前取舍。
