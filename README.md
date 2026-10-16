# ACGN 内容服务与接口自动化测试平台

一个 FastAPI 应用，里面装了两块：能正常浏览的 ACGN 内容站，和围绕这些内容接口搭的自动化测试后台。
同进程、同数据库，但页面布局和权限各走各的——内容站对所有人开放，测试后台只有管理员能进。

## 目录

- [1. 快速开始](#1-快速开始)
- [2. 页面区域划分](#2-页面区域划分)
- [3. ACGN 内容平台](#3-acgn-内容平台)
- [4. 接口自动化测试平台](#4-接口自动化测试平台)
- [5. 数据管理](#5-数据管理)
- [6. 用户与访问控制](#6-用户与访问控制)
- [7. 项目结构](#7-项目结构)
- [8. 接口一览](#8-接口一览)
- [9. 数据模型](#9-数据模型)
- [10. 测试与代码检查](#10-测试与代码检查)
- [11. 数据库配置](#11-数据库配置)
- [12. 已知问题](#12-已知问题)

## 1. 快速开始

Python 3.13。默认 SQLite，不装额外服务就能跑起来。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

首次启动建表并写入开发种子数据，重复启动不会覆盖已有数据。之后：

- 内容站 <http://127.0.0.1:8000/>
- 测试后台 <http://127.0.0.1:8000/testcases>

### 1.1 默认账号

`users` 表为空时，按 `.env` 创建两个账号：

| 角色 | 用户名 | 口令 | 环境变量前缀 |
|---|---|---|---|
| 管理员 | `admin` | `admin123456` | `DEFAULT_ADMIN_*` |
| 普通用户 | `demo` | `demo123456` | `DEFAULT_USER_*` |

已有账号时不重复创建，也不会改动任何现存账号。

> 部署前用 `.env` 覆盖默认口令和 `AUTH_SECRET_KEY`，并确认 `.env` 没被提交（已在 `.gitignore` 里）。

自助注册（`/register`）、邮箱验证和找回密码见[第 6 节](#6-用户与访问控制)。

### 1.2 命令行维护账号

```powershell
python scripts/create_user.py --list                              # 列出全部账号
python scripts/create_user.py --username alice --role admin       # 创建，口令交互输入
python scripts/create_user.py --username alice --reset-password   # 重置口令
```

## 2. 页面区域划分

两套布局，共用 `base.html` 骨架：

| 区域 | 布局 | 受众 | 主导航 |
|---|---|---|---|
| ACGN 内容平台 | `base_content.html` | 所有人 | 首页 / ACGN 资讯 / 新番档期，右侧有「测试管理」入口；登录用户另有「个人空间」 |
| 测试管理平台 | `base_admin.html` | 仅管理员 | 测试用例 / 执行历史 / 回归批次 / 数据同步 / 数据管理，右侧有返回入口 |

这个划分同时也是权限边界，见[第 6 节](#6-用户与访问控制)。

## 3. ACGN 内容平台

三类数据，全部公开：

- **作品**（`contents`）：门户页 `/` 只做发现（分类入口 / 高分推荐 / 最新资讯）；全量浏览和搜索统一在 `/contents`，类型筛选 + 关键词搜索 + 分页；详情在 `/contents/{id}`。
- **资讯**（`articles`）：列表 `/articles`、详情 `/articles/{id}`，只展示已发布的。
- **新番档期**（`anime_schedules`）：档期页 `/schedule` 按 `season` 切换，同步历史在 `/schedule/runs`。
- **个人空间**（`/me`，需登录）：账号信息、个人简介、作品收藏与浏览记录。详情页可收藏 / 取消收藏作品；登录用户浏览作品自动留记录，重复浏览只更新时间和条数，每人最多保留最近 100 条。

数据主要来自三个抓取脚本（见 [5.2](#52-数据同步)），管理员也可以在后台手动触发。

## 4. 接口自动化测试平台

管理员用的测试后台，管四件事：用例、执行、执行记录，以及批次之间的回归对比。

- **用例管理**：增删改查，字段有名称、请求方法、路径、期望状态码、启用开关。
- **单条执行**：`POST /api/testcases/{id}/execute`，或页面上的执行按钮，结果写入 `test_runs`。
- **批量执行**：`POST /api/batches` 把所有启用的用例跑成一批，统计落 `test_batches`。
- **回归分析**：`GET /api/batches/{id}/regression` 拿当前批次和上一个有效批次对比，看哪些从通过变失败、哪些修好了。
- **执行历史**：列表可按 `case_id` 筛，详情页看期望 / 实际状态码、耗时和错误信息。

### 4.1 执行安全约束

用例只接受本站本地路径，执行时在应用进程内向自己发请求：

- 不访问外部地址，不访问内网，不跟随重定向
- 有超时，不读响应体
- 单条执行异常记为失败，不影响同批其它用例
- 用例删掉后，历史记录仍保留用例名、方法和路径快照（弱关联，不设外键级联）

## 5. 数据管理

测试后台的「数据管理」维护内容站的三类数据，二级导航：总览 / 作品数据 / 资讯数据 / 新番档期 / 数据同步。

| 数据域 | 页面 | 查看 | 删除 | 其它 |
|---|---|---|---|---|
| 作品 | `/admin/data/contents` | 跳内容详情页 | 单条 + 批量 | 关键词 / 类型筛选 + 分页 |
| 资讯 | `/admin/data/articles` | 跳资讯详情页 | 单条 | 关键词筛选 + 分页，含未发布 |
| 新番档期 | `/admin/data/schedules` | 跳档期页 | 单条 | 按档期重新同步 + 筛选 |
| 数据同步 | `/admin/data/sync` | 同步结果与历史 | — | 选范围 → 预览 → 确认写入 |

### 5.1 删除与重新同步

删除和同步都限管理员：未登录跳 `/login?next=...`，普通用户给 403 页面，后端对提交请求同样校验。

删除按主键单表删，不做跨表级联，一个都没勾选时提交不会删任何东西。删完或同步完 303 跳回列表页，结果用查询参数回显（`deleted=2`、`resync=ok&added=..`），刷新不会重复提交。

重新同步直接调 `scripts/sync_yuc_schedule.run_sync`（`apply=True`），没有另写一份逻辑，成功后回显本次的新增 / 更新 / 不变 / 失败计数。外部网络不通或档期不存在时只回显原因，不抛异常。同步不删本地记录，删除只能由管理页显式触发。

### 5.2 数据同步

`/admin/data/sync` 是统一入口，只做编排，三个数据源的实际逻辑都复用已有脚本，由 `app/sync_tasks.py` 调度：

| 数据源 | 同步范围 | 复用的流程 |
|---|---|---|
| Bangumi 条目 | 一个或多个条目 ID（空格 / 逗号分隔，单次上限 30） | `scripts/import_bangumi.run_import` |
| 新番档期 | 一个档期（下拉取本地已同步档期，或手填 `YYYYMM`） | `scripts/sync_yuc_schedule.run_sync` |
| 官方资讯 | 站点栏目 + 最多条数（上限 30，留空取最新 12 条） | `scripts/import_articles.run_import_articles` |

官方资讯抓的是 Aniplex 官方新闻站 [ANIPLEX NEWS BOX](https://news.aniplex.co.jp/)：栏目列表顺着页内「view more」的 REST 端点翻页，详情页 4 线程并发抓，完整标题、发布日期、摘要和正文写入 `articles`，正文里的内嵌视频转成媒体链接行。

栏目只挑站内能拿到「标题 + 日期 + 原文链接」的：

| 入库 `category` | 站点栏目 | 正文来源 |
|---|---|---|
| 动漫资讯 | `?category=news` | 站内详情页 `detail.html?id=...` |
| 榜单资讯 | `?category=ranking` | 站内详情页 `detail.html?id=...` |
| 活动资讯 | `?category=event` | 条目自带官网链接，以「标题 + 原文链接」入库 |

YouTube（`?category=movie`）和播客（`?category=radio`）栏目只有播放器弹窗、没有文章页，不收，免得写进一堆只有标题的空记录。

流程分两步：预览只读源数据并汇总，不写业务数据；确认才真正写入。确认入口只在刚预览完时出现，主入口和「预览」按钮并排在范围表单下方，按钮上带待写入条数；逐条明细表下面另留一个次要入口，因为明细最多 30 条，只放在明细之后会被推到页面底部看不见。历史回看 `?run=` 不给入口。

重复执行不产生脏数据：Bangumi 按 `bangumi_id` 唯一索引跳过，档期按 `(season_code, source_key)` 更新或保持，资讯按 `original_url` / 标题命中跳过。源站内容也不会盖掉本地已有的摘要和正文。

单条失败不写半条——详情页抓不到就计失败并在结果里说明，站点不可用或结构变化由页面回显原因。同一数据源同一时刻只允许一个任务跑（进程内互斥锁 + 运行记录 `running` 状态检查），超过 10 分钟的 `running` 记录当僵死处理，不再拦截。

每次预览和正式同步都写一条 `sync_task_runs`，记录数据源、范围、模式、触发来源、发起人、各类计数、失败原因和逐条明细快照；页面下方显示最近 20 条，可 `?run=<id>` 回看。

同步这块只用标准库 `threading` 和已有的 SQLAlchemy 数据层，没引调度框架。以后要挂定时任务，调同一个入口 `app.sync_tasks.run_task(..., apply=True, trigger="scheduled")` 就行。

## 6. 用户与访问控制

服务端会话 Cookie + 角色判断，没引第三方登录，也没做 RBAC 那套。

### 6.1 角色与权限

只有 `user` 和 `admin` 两种角色。Cookie 只存用户 id，角色每次请求查库。

| 资源 | 游客 | 普通用户 | 管理员 |
|---|---|---|---|
| 内容平台页面（`/`、`/articles`、`/contents/{id}`、`/schedule`、`/schedule/runs`） | 可访问 | 可访问 | 可访问 |
| 内容 API（`/api/health`、`/api/contents*`、`/api/schedule*`） | 可访问 | 可访问 | 可访问 |
| 个人空间（`/me`、收藏与浏览记录） | 跳登录 | 可访问 | 可访问 |
| 测试平台页面（`/testcases*`、`/runs*`、`/batches*`、`/admin/data*`） | 跳登录 | 403 页 | 可访问 |
| 测试平台 API（`/api/testcases*`、`/api/runs*`、`/api/batches*`） | 401 | 403 | 放行 |

### 6.2 拒绝方式

- 页面：未登录 303 跳 `/login?next=...`，已登录但越权给 403 HTML 页
- API：未登录 401，越权 403，都是 JSON

判断都在后端做（自定义异常 `LoginRequired` / `PermissionDenied` + FastAPI 异常处理器），隐藏导航只是顺带效果，直接输管理 URL 或调管理 API 一样被拦。`/login` 的 `next` 过 `safe_next_url` 校验，只放行站内 `/` 开头的路径。

### 6.3 口令与会话

口令用 PBKDF2-HMAC-SHA256 + 每账号随机盐存，明文不落库（`app/security.py`，纯标准库）。会话密钥 `AUTH_SECRET_KEY` 必须经 `.env` 覆盖，默认值只够本地开发；`SESSION_MAX_AGE_SECONDS` 默认 8 小时。

### 6.4 注册

`/register` 自助注册，注册出来的账号固定是 `user`，只能看内容站。角色由服务端常量 `REGISTRATION_ROLE` 决定，不读表单或查询串里的任何 `role` 值。管理员只能由现有管理员用命令行建。

用户名 3-50 字符（字母、数字、下划线、连字符），口令 6-128 位且需二次确认。用户名或邮箱重复、参数不合法都回显原因（400），不抛 500。

### 6.5 邮箱验证

可插拔，启用后注册要填邮箱，注册完不直接登录，先点验证链接；链接 24 小时内有效，重发后旧链接失效，重发有 60 秒冷却。

没配 SMTP 时不发真邮件：链接写进日志，也能在待验证页（「生成验证链接」）或管理员页面 `/admin/accounts` 取到，本地没邮件服务照样能走完流程。配好 `SMTP_HOST` / `SMTP_FROM_ADDRESS` 和授权码后自动切真实发信，业务代码不用动。`EMAIL_VERIFICATION_ENABLED=false` 整体关掉，退回「注册即登录」。

命令行建的账号和启用该功能之前的历史账号没有邮箱，一律算已验证，不会因为开了邮箱验证就登不进去。

令牌只以 SHA-256 哈希入库，一次有效。管理员可以在 `/admin/accounts` 手动放行。

### 6.6 找回密码

登录页点「忘记密码」进 `/forgot-password`，填注册邮箱后发出一次性重置链接（默认 1 小时，比邮箱验证短），在 `/reset-password` 设新密码。

- 未注册的邮箱返回和成功完全一致的提示，不回显邮箱、不签发令牌，避免被用来枚举账号
- 链接一次性：改密成功立即失效，重新申请会让旧链接作废，重发有 60 秒冷却
- 改密后此前签发的会话全部失效——会话 Cookie 没法在服务端逐条吊销，改成比较 Cookie 签发时间（iat）和 `users.password_changed_at`，早于改密时刻的一律判未登录
- 邮箱还没验证的账号，通过重置链接改密会顺带标记为已验证
- 没有邮箱的历史账号（比如脚本建的 admin）没有投递目标，不签发令牌，只能由管理员用 `scripts/create_user.py --reset-password` 重置

## 7. 项目结构

```text
acgn-test-platform/
├── app/
│   ├── main.py              # 应用入口：路由、静态资源、守卫与异常处理器
│   ├── config.py            # 环境变量 / .env 读取（pydantic-settings）
│   ├── database.py          # 引擎、会话与依赖
│   ├── models.py            # SQLAlchemy 数据模型
│   ├── crud.py              # 数据访问、查询与初始化（含角色常量与默认账号）
│   ├── schemas.py           # 请求校验模型
│   ├── api.py               # 内容 JSON API（公开）
│   ├── cases.py             # 测试用例 / 执行 / 批次 API（仅管理员）
│   ├── runner.py            # 用例执行器、批次执行与回归对比
│   ├── business_pages.py    # 内容站页面路由（门户 / 作品 / 详情 / 资讯 / 档期 / 同步历史）
│   ├── space_pages.py       # 个人空间页面路由（信息 / 简介 / 收藏 / 浏览记录）
│   ├── testing_pages.py     # 测试平台页面路由（用例 / 执行历史 / 批次）
│   ├── admin_pages.py       # 数据管理与账号管理页面路由
│   ├── sync_tasks.py        # 同步任务注册与执行入口（范围校验、防重、运行记录）
│   ├── auth.py              # 注册 / 登录 / 登出、当前用户解析、页面与 API 守卫
│   ├── security.py          # 口令哈希与会话令牌（仅标准库）
│   ├── mailer.py            # 邮件发送（无 SMTP 时降级为日志）
│   ├── page_common.py       # 页面路由共享设施（模板、会话注解、404、布局选择）
│   ├── fake_data.py         # 开发测试种子数据
│   ├── templates/           # Jinja2 模板（base / base_content / base_admin + 各页面）
│   └── static/              # 静态资源
├── scripts/
│   ├── create_user.py       # 账号创建 / 重置 / 列表
│   ├── import_bangumi.py    # Bangumi 条目导入
│   ├── import_articles.py   # Aniplex 官方资讯导入
│   └── sync_yuc_schedule.py # Yuc.wiki 新番档期同步
├── tests/                   # pytest 自动化测试
├── reports/                 # 测试报告等输出
├── conftest.py              # pytest 根配置（把项目根加入 sys.path）
├── requirements.txt
├── .env.example
└── README.md
```

## 8. 接口一览

### 8.1 内容服务（公开）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/contents` | 内容列表，支持 `type` 筛选 |
| GET | `/api/contents/search?q=关键词` | 按标题或简介搜索 |
| GET | `/api/contents/{content_id}` | 内容详情 |
| GET | `/api/schedule` | 新番档期列表，支持 `season` |
| GET | `/api/schedule/seasons` | 已同步档期列表 |
| GET | `/api/schedule/runs` | 档期同步历史 |
| GET | `/api/schedule/{schedule_id}` | 档期条目详情 |
| GET | `/` | 门户页（分类 / 推荐 / 资讯，不含全量列表） |
| GET | `/contents` | 全部作品页，支持 `q`、`type`、`page` |
| GET | `/contents/{content_id}` | 内容详情页 |
| GET | `/articles` | 资讯列表页 |
| GET | `/articles/{article_id}` | 资讯详情页 |
| GET | `/schedule` | 新番档期页 |
| GET | `/schedule/runs` | 档期同步历史页 |

### 8.2 认证与账号

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/login` | 登录表单 / 提交，支持 `next` 回跳 |
| GET / POST | `/register` | 注册，只创建普通用户，支持 `next` 回跳 |
| GET / POST | `/logout` | 登出 |
| GET / POST | `/forgot-password` | 申请重置链接（未注册邮箱提示一致） |
| GET / POST | `/reset-password` | 凭 `?token=` 设置新密码 |
| GET | `/reset/pending` | 重置待办页（无 SMTP 时取站内链接） |
| POST | `/reset/resend` | 重发重置邮件（带冷却） |
| POST | `/reset/link` | 无 SMTP 时生成站内重置链接 |
| GET | `/verify/pending` | 邮箱验证待办页 |
| GET / POST | `/verify/resend` | 重发验证邮件（带冷却） |
| POST | `/verify/link` | 无 SMTP 时生成站内验证链接 |
| GET | `/verify/email?token=…` | 完成验证（令牌一次有效） |
| GET | `/admin/accounts` | 管理员：待验证账号列表 |
| POST | `/admin/accounts/{id}/verify` | 管理员：手动标记已验证 |
| POST | `/admin/accounts/{id}/link` | 管理员：生成验证链接 |

### 8.3 个人空间（需登录）

未登录跳 `/login?next=...`，已登录即可用（不限制角色）。收藏与浏览记录只归属当前登录账号，查询一律带 `user_id`；写操作统一 PRG——303 跳回 `/me`（或来源详情页）并用查询参数回显结果。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/me` | 个人空间页，支持 `tab=favorites\|history`、`page` |
| POST | `/me/profile` | 保存简介（`bio`，最多 500 字，留空即清除） |
| POST | `/contents/{content_id}/favorite` | 收藏 / 取消收藏（切换），`next` 回跳来源页 |
| POST | `/me/favorites/{content_id}/remove` | 取消单条收藏 |
| POST | `/me/favorites/clear` | 清空收藏 |
| POST | `/me/history/{content_id}/remove` | 删除单条浏览记录 |
| POST | `/me/history/clear` | 清空浏览记录 |

### 8.4 测试管理 API（仅管理员）

未登录 401，普通用户 403。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/testcases` | 用例列表 / 创建 |
| GET / PUT / DELETE | `/api/testcases/{case_id}` | 查询 / 编辑 / 删除 |
| POST | `/api/testcases/{case_id}/execute` | 执行单条用例 |
| GET | `/api/runs` | 执行记录列表，可按 `case_id` 筛选 |
| GET | `/api/runs/{run_id}` | 执行记录详情 |
| POST | `/api/batches` | 批量执行全部启用用例（201） |
| GET | `/api/batches` | 批次列表（最新在前） |
| GET | `/api/batches/{batch_id}` | 批次统计详情 |
| GET | `/api/batches/{batch_id}/regression` | 与上一有效批次对比 |

### 8.5 测试管理平台页面（仅管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/testcases` | 用例管理页 / 创建用例 |
| GET / POST | `/testcases/{case_id}/edit` | 用例编辑页 |
| POST | `/testcases/{case_id}/delete` | 删除用例 |
| POST | `/testcases/{case_id}/execute` | 页面内执行单条用例 |
| POST | `/testcases/execute-all` | 批量执行全部启用用例 |
| GET | `/runs` | 执行历史页，可按用例筛选 |
| GET | `/runs/{run_id}` | 执行记录详情页 |
| GET | `/batches` | 回归批次列表页 |
| GET | `/batches/{batch_id}` | 批次详情与回归分析页 |

### 8.6 数据管理页面（仅管理员）

权限同测试平台：未登录跳 `/login?next=...`，普通用户 403 页。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/data` | 数据管理总览（三类数据规模与入口） |
| GET | `/admin/data/contents` | 作品列表，支持 `q`、`type`、`page` |
| POST | `/admin/data/contents/delete` | 删除作品，提交重复的 `ids` 字段（单条或批量） |
| GET | `/admin/data/articles` | 资讯列表，支持 `q`、`page`（含未发布） |
| POST | `/admin/data/articles/{article_id}/delete` | 删除单篇资讯 |
| GET | `/admin/data/schedules` | 档期列表，支持 `season`、`q`、`page` |
| POST | `/admin/data/schedules/{schedule_id}/delete` | 删除单条档期作品 |
| POST | `/admin/data/schedules/resync` | 按 `season_code` 重新同步该档期 |
| GET | `/admin/data/sync` | 数据同步页，支持 `task`、`run`、`error` |
| POST | `/admin/data/sync/preview` | 预览：`task`、`scope`（资讯另需 `limit`），只汇总不写业务数据 |
| POST | `/admin/data/sync/apply` | 确认写入，同步完 303 跳到该次结果页 |

> 批量删除用**重复的 `ids` 表单字段**（`await request.form()` + `getlist("ids")`）。
> 当前 FastAPI 版本的 `list[int] = Form(default=[])` 收不到多值，别改成那种写法。

## 9. 数据模型

| 表 | 模型 | 用途 | 唯一约束 |
|---|---|---|---|
| `contents` | `Content` | ACGN 作品条目 | `bangumi_id` 唯一（导入去重） |
| `articles` | `Article` | 官方资讯文章 | 业务层按 `original_url` / 标题去重 |
| `anime_schedules` | `AnimeSchedule` | 新番档期条目 | `(season_code, source_key)` 联合唯一 |
| `test_cases` | `TestCase` | 接口测试用例定义 | — |
| `test_runs` | `ExecutionRecord` | 执行记录（含用例名 / 方法 / 路径快照） | — |
| `test_batches` | `ExecutionBatch` | 批次执行统计 | — |
| `sync_task_runs` | `SyncTaskRun` | 统一数据同步运行记录 | — |
| `schedule_sync_runs` | `ScheduleSyncRun` | 档期专用同步记录 | — |
| `content_favorites` | `ContentFavorite` | 个人空间作品收藏 | `(user_id, content_id)` 联合唯一 |
| `content_view_history` | `ContentViewHistory` | 个人空间浏览记录 | `(user_id, content_id)` 联合唯一 |
| `users` | `User` | 平台账号（`user` / `admin`，含简介 `bio`） | `username` 唯一 |

几个设计上的选择：

- `test_runs.case_id` 和 `sync_task_runs.actor_id` 都是弱关联，不设外键级联。用例或账号删掉后，历史记录靠名称 / 路径快照照样能读懂。
- `test_runs.batch_id` 为 `NULL` 表示单条执行，永远不计入批次统计。
- `anime_schedules.source_key` 取规范化后的主标题（去空白、转小写），因为源站档期页没有稳定的作品 ID。
- 收藏与浏览记录的 `content_id` 同为弱关联：作品被后台删除后记录行仍保留，列表页内连接查不到即不展示。收藏重复点击幂等；浏览记录同一作品只更新 `viewed_at` 不堆叠行，每人最多保留最近 100 条，写入时裁掉最旧的。

## 10. 测试与代码检查

```powershell
python -m pytest -q                                                    # 全部用例
python -m pytest -q --tb=no                                            # 只看失败摘要
python -m pytest --html=reports/api-test-report.html --self-contained-html
ruff check app tests conftest.py
```

当前 468 项用例，除 `test_api.py` 里 5 项外全部通过（见[第 12 节](#12-已知问题)）。

测试的组织方式：

- `tests/auth_helpers.py` 提供 `create_accounts(factory)` 和 `login_as_admin(client)`，管理相关的测试要先建账号并登录。`tests/` 由 pytest prepend 模式加进 `sys.path`，可以直接 import。
- 覆盖内容 API、用例 CRUD、执行与记录、批次与回归、认证与越权、注册、邮箱验证（令牌 / 登录拦截 / 降级 / 管理员放行）、找回密码、个人空间（简介 / 收藏 / 浏览记录 / 跨账号隔离）、数据管理、同步任务、三类抓取脚本。
- 邮件相关测试不触网，SMTP 用桩替换（`tests/test_email_verification.py` 的 `FakeSMTP`）。
- 跑之前确认用的是隔离的测试库，别把测试数据写进开发库。

## 11. 数据库配置

`DATABASE_URL` 环境变量，支持 `.env`（参考 `.env.example`）。

```text
# 开发环境（默认）：SQLite
sqlite:///./local_dev.db

# 生产目标：MySQL
mysql+pymysql://用户名:密码@127.0.0.1:3306/acgn_test_platform?charset=utf8mb4
```

切 MySQL 前确认服务已起、库已建、连接参数对。建表在应用导入期做（`crud.init_db`，幂等）；`alembic` 已装但迁移脚本还没写，生产部署配置也待补。

> 别把带真实密码的 `.env` 提交上去。

## 12. 已知问题

1. **`test_api.py` 里 5 项用例依赖本地种子数据的固定 id**：`test_get_content_detail`、`test_detail_page`、`test_search_hit`、`test_search_case_insensitive`、`test_search_matches_summary_only_keyword`。
   它们直连 `local_dev.db` 并假设 id 是 1、5、6，本地数据被后台删掉或改过就会失败。种子重新播进干净库后这 5 项同样通过，是用例的数据依赖，不是代码缺陷。
2. **数据库迁移还没落地**：建表靠应用导入期执行，没接 Alembic；切到 MySQL 后的生产部署配置也待完善。
