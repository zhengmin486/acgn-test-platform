# 特殊档期三分类填充交付总结（2026-10-24）

## 需求

`/schedule` 页三个特殊分类 tab 全部显示 0：**未上线新番 / 剧场版ova与特别篇 / 大陆引进剧场版**，要求填充。

## 根因（两层叠加）

1. **从来没人触发过**：cron（`.ops/run-cron.sh auto` → `run_sync.py --task yuc_schedule`）只同步 Atom 里的最新**季度**档期；三个特殊分类不在季度发现路径里，管理端虽支持按分类码手动重同步，但一直没被触发过。
2. **源站改了页面结构，旧解析器切不出条目**：季度页（/YYYYMM/）仍是 `<!--#A01-->` 注释标记结构（69 条照常）；但三个特殊分类页 2026-10 改版——
   - `/new/`（未上线新番）→ `<div class="future_div">` 块（future_type_/future_date_/future_title_ + 块内海报）；
   - `/sp/`（剧场版ova与特别篇）→ `<div class="div_sp">` 块（sp_title_/type-m/s/o/sp_release_ + 月度分组 details）；
   - `/movie/（大陆引进剧场版）→ 同 div_sp 块（movie_title/movie_release/type-o）。
   - 同时 cn_movie 的 Atom 匹配键失效：链接 `/cnmovie/` → `/movie/`，标题「内地上映动画电影」→「日本动画电影-内地院线档期」。

## 改动

| 文件 | 改动 |
|---|---|
| `scripts/run_sync.py` | cron 主循环：yuc 季度档期后**接力同步三个特殊分类**（`special_category_codes()` + 共用 `_run_one` helper）；与季度互相独立、单个失败不阻断其余、幂等 |
| `scripts/sync_yuc_schedule.py` | 新增 `split_special_entries` / `parse_special_entry` / `_SpecialEntryParser`（块起点正则 + 类名前缀分组；`<br>` 折行转空格；平台链接走 `platform_from_link`）；`run_sync` 按 `category` 分流（特殊页走新解析器，季度页走原解析器）；`SPECIAL_CATEGORIES` 的 cn_movie 匹配键改为 `/movie/` + 标题「内地院线」 |
| `tests/test_run_sync.py` | 默认调用序列改为「季度 + 3 分类 + 资讯」；新增特殊分类单失败隔离、`special_category_codes` 用例 |
| `tests/test_yuc_schedule.py` | Atom 夹具改真实结构（/movie/ + 新标题）；新增 `TestSpecialPageParser`（三个页面的 fixture 镜像真实 HTML）；标题兜底匹配用例 |

## 验证

- 全量测试 **1835 passed**（新增 18 个用例，无回归）。
- 部署：paste.rs 单文件通道，本地回读 md5 校验一致后替换，服务器 `py_compile` 通过；
  备份 `scripts/run_sync.py.bak-20261024` / `scripts/sync_yuc_schedule.py.bak-20261024`。
- 填充（`sync_yuc_schedule.py --special --apply`）：

| 分类 | 解析 | 入库 | 说明 |
|---|---|---|---|
| 未上线新番 | 129 | **129** | 0 跳过 |
| 剧场版ova与特别篇 | 368 | **364** | 4 条页内重复（按设计合并跳过） |
| 大陆引进剧场版 | 77 | **76** | 1 条缺标题（按设计跳过） |

合计 **569 条、0 failed**。

- 线上复核：DB 分类计数 129/364/76；`list_schedule_seasons` 仍只收 28 个 YYYYMM 季度（分类码未污染季度导航）；`/schedule` 及三个 `?category=` 页面全部 200，tab 显示「已同步 N 部」；API `/api/schedule?category=*` total 分别 129/364/76。
- 样本数据：鬼灭之刃无限城篇/蜡笔小新/小林家的龙女仆（cn_movie）、BanG Dream! Ave Mujica 续篇/LoveLive! 莲之空第2章（unreleased），类型/档期/海报齐全。

## 常态化（今后不再需要手动点）

cron 每轮（当前 10 月为每 4 小时）跑季度档期后自动接力三个特殊分类，日常只有「无变化」；源站更新后下一轮自动跟进。规则已写入 `ingest.md` 第十节与今日日志。

## 备注 / 风险

- 部署时间窗教训：00:17 的 cron 进程比文件替换早 11 秒启动，加载了旧码——**改 cron 入口文件后当轮不生效**，本次已手动 systemd 补跑验证，后续部署注意此窗口。
- 两个 paste 副本均已删除并复核；本地临时探查脚本已清理。
- 旧解析器代码保留（季度页仍在用），未做删除。
