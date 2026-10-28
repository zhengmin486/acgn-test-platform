# 两页剧场版改按年/月分组（2026-10-24）

## 需求

「两页剧场版页面要按年/月排，不是按日」——即档期页的两个剧场类分类：
**剧场版/OVA/特别篇（ova_sp）** 与 **大陆引进剧场版（cn_movie）**。

## 为什么

上一轮（同日早先）把三个特殊分类都做成了「按播出/上映日期分组的时间线」。
对电影这是错的粒度：一部电影一个上映日，按日分会碎成大量单条组。线上实测：

| 页面 | 改前（按日） | 改后（按月） |
| --- | --- | --- |
| cn_movie | 76 部 → **71 组** | 76 部 → **43 组**（全部「YYYY年M月」） |
| ova_sp | 363 部 → **238 组** | 363 部 → **60 组**（54 月组 + 6 个源站原文组） |
| unreleased（未上线新番） | 129 部 → 5 组 | **不变**（仍按日，见下） |

## 改动

| 文件 | 改动 |
| --- | --- |
| `app/business_pages.py` | `_group_by_date(works, by_month=False)` 与 `_date_group_name(key, by_month=False)` 加月粒度参数：ISO air_date 取前 7 位当月键 → 组名「2026年11月」；月组 `weekday_name`/`countdown` 置 None。新增 `MONTH_GROUP_CATEGORIES = frozenset({"ova_sp", "cn_movie"})` 作为唯一切换位，`schedule_page` 按 `category in MONTH_GROUP_CATEGORIES` 选粒度 |
| `app/templates/schedule.html` | **逻辑零改动**（组头对 countdown/weekday 本就判空，月组自动只显示组名 + 条数）；仅更新注释说明两种粒度 |
| `tests/test_schedule_date_groups.py` | 新增 `TestGroupByMonth` 4 个单测（同月并组/组内顺序、月组无周几与倒计时、note 原文兜底 + 待定最后、默认仍按日）；新增 ova_sp 月组页面用例与 unreleased 按日护栏用例；原 cn_movie 页面用例断言按新口径改写（「2025年11月」在、「2025年11月14日」与倒计时 pill 不在） |

**关键决策——未上线新番（unreleased）不改成按月**：倒计时到天（「还有 48 天」）对追番有意义，电影没有。切换集中在 `MONTH_GROUP_CATEGORIES` 一个常量，路由一行传参。

**保持不变的行为**：无 air_date 时按 broadcast_note 原文归组（「2027冬」「TV放送：9/2-9/9」）、「时间待定」排最后、组内沿用查询排序（air_date 倒序）、候选不足不凑数。

## 验证

- `tests/test_schedule_date_groups.py`：**16 passed**（原 10 + 新 6）；
  所有触碰 `/schedule` 的 7 个测试文件合计 **317 passed，0 failed**。
- 部署：paste.rs 双文件（business_pages.py 70,606 B + schedule.html 9,538 B），
  上传/下载 **md5 双向一致** → 覆盖 → grep 确认 → `import app.main` 预检
  IMPORT_OK → `systemctl restart acgn`（active）。
- 线上复核（见上表）：两页剧场版月分组生效，未上线新番页面与改前逐组一致。

## 回滚

服务器旧版备份：`/tmp/business_pages.py.bak-20261024`（md5 `fb11a7ae…`）、
`/tmp/schedule.html.bak-20261024`（md5 `1d871bb5…`）。覆盖回这两个文件 →
导入预检 → 重启即可。

## 备注

- 未提交 git（工作树挂着大量未提交改动，按既往惯例不主动 commit/push）。
- ova_sp 有 6 个组是源站原文 note（如「TV放送：9/2-9/9」「20262/20放送」，
  后者疑为源站把 2026/2/20 写坏），按「源站怎么说就怎么归组」的既定原则原样
  展示；字符串倒序下 'T' 开头的 note 组会排在 ISO 月组前面——此为改前就存在
  的既有行为，本次未动。
