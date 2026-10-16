# ACGN 内容服务与接口自动化测试平台

一个把 **ACGN 内容展示** 与 **Web 接口自动化测试** 合并在同一进程里的 FastAPI 应用：
前半部分是能真正浏览的作品 / 资讯 / 新番档期内容站，后半部分是围绕这些内容接口搭建的
用例管理、执行、回归分析与数据同步后台。

两者共用同一套数据层与基础样式，但在信息架构、布局与访问权限上相互独立——
内容平台对所有人开放，测试管理平台仅管理员可进入。

---

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

---

## 1. 快速开始

### 1.1 环境要求

- Python 3.13
- 无需外部数据库即可跑起来（默认 SQLite）

### 1.2 安装与启动

```powershell
# 1) 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) 安装依赖
pip install -r requirements.txt

# 3) 复制环境变量模板并填写（至少改掉默认口令与会话密钥）
copy .env.example .env

# 4) 启动
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

启动即建表并播种开发与测试种子数据（幂等，已有数据不覆盖）。打开
<http://127.0.0.1:8000/> 进入内容平台，<http://127.0.0.1:8000/testcases> 进入测试管理平台。

### 1.3 账号

首次初始化（`users` 表为空）时按 `.env` 创建两个账号：

| 角色 | 默认用户名 | 默认口令 | 环境变量前缀 |
|---|---|---|---|
| 管理员 | `admin` | `admin123456` | `DEFAULT_ADMIN_*` |
| 普通用户 | `demo` | `demo123456` | `DEFAULT_USER_*` |

> **部署前必须通过 `.env` 覆盖默认口令与 `AUTH_SECRET_KEY`**，并确认 `.env` 未被提交（已被 `.gitignore` 忽略）。
> 已有账号时不会重复创建，也不会覆盖或清空任何账号。

账号维护走命令行：

```powershell
python scripts/create_user.py --list                                   # 列出全部账号
python scripts/create_user.py --username alice --role admin            # 创建（口令交互输入）
python scripts/create_user.py --username alice --reset-password        # 重置口令
```

---

## 2. 页面区域划分

页面分两个区域，各有一套布局，共用 `base.html` 骨架：

| 区域 | 布局 | 受众 | 主导航 |
|---|---|---|---|
| **ACGN 内容平台** | `base_content.html` | 所有人（含游客） | 首页 / ACGN 资讯 / 新番档期 + 右侧「测试管理」入口 |
| **测试管理平台** | `base_admin.html` | 仅管理员 | 测试用例 / 执行历史 / 回归批次 / 数据同步 / 数据管理 + 「返回内容平台」入口 |

区域边界同时就是访问控制的划分依据（见 [第 6 节](#6-用户与访问控制)）。

---

## 3. ACGN 内容平台

内容平台提供三类数据与对应页面 / API，均对所有人开放：

- **作品数据**（`contents`）：列表页 `/`、详情页 `/contents/{id}`，支持类型筛选与关键词搜索。
- **资讯数据**（`articles`）：列表页 `/articles`、详情页 `/articles/{id}`，只展示已发布文章。
- **新番档期**（`anime_schedules`）：档期页 `/schedule`，按 `season` 切换档期；`/schedule/runs` 查看档期同步历史。

数据主要来自三个抓取脚本（见 [第 5.2 节](#52-数据同步)），也可由管理员在后台手动同步。

---

## 4. 接口自动化测试平台

面向管理员的测试管理后台，覆盖「用例 → 执行 → 记录 → 回归」的完整闭环。

- **用例管理**：创建、查询、编辑、删除，字段含名称、请求方法、路径、期望状态码、启用开关。
- **单条执行**：`/api/testcases/{id}/execute` 或页面上的执行按钮，结果落 `test_runs`。
- **批量执行**：`/api/batches`（POST）把**全部启用用例**作为一批执行，落 `test_batches` 统计。
- **回归分析**：`/api/batches/{id}/regression` 把当前批次与上一个有效批次对比，定位新增失败与恢复用例。
- **执行历史**：列表可按 `case_id` 筛选，详情页展示期望 / 实际状态码、耗时、错误信息。

### 4.1 执行安全约束

用例**仅接受本站本地路径**，执行时在应用进程内向自身发起请求：

- 不访问外部地址，不访问内网，不跟随重定向；
- 设置超时，不读取响应体；
- 执行异常统一记为失败，不影响其它用例；
- 删除用例后历史记录仍保留用例名称、请求方法与路径快照（弱关联，不设外键级联）。

---

## 5. 数据管理

测试管理平台 →「数据管理」维护内容平台的三类数据，二级导航为：总览 / 作品数据 / 资讯数据 / 新番档期 / 数据同步。

| 数据域 | 页面 | 查看 | 删除 | 其他 |
|---|---|---|---|---|
| 作品数据 | `/admin/data/contents` | 链接到内容详情页 | 单条 + **批量删除** | 关键词 / 类型筛选 + 分页 |
| 资讯数据 | `/admin/data/articles` | 链接到资讯详情页 | 单条 | 关键词筛选 + 分页（含未发布） |
| 新番档期 | `/admin/data/schedules` | 链接到内容平台档期页 | 单条 | **按档期重新同步** + 档期 / 关键词筛选 |
| 数据同步 | `/admin/data/sync` | 同步结果与历史 | — | **选数据源与范围 → 预览 → 确认写入** |

### 5.1 删除与重新同步

- **权限**：仅管理员，未登录跳 `/login?next=...`，普通用户返回 403 页面；后端对提交请求同样校验。
- **删除语义**：单表按主键删除，**不做跨表级联**，也不影响其它模块数据；未勾选任何条目时提交不删除任何数据。
- **结果回显**：删除 / 同步成功均 303 重定向回列表页并用查询参数回显（如 `deleted=2`、`resync=ok&added=..`），避免刷新重复提交。
- **重新同步**：复用既有同步流程 `scripts/sync_yuc_schedule.run_sync`（`apply=True`），不复制也不改动同步逻辑；
  成功后回显本次「新增 / 更新 / 不变 / 失败」计数。外部网络不可用或档期不存在时只回显原因，不抛异常。
  同步本身不删除本地记录，删除只能由管理页显式触发。

### 5.2 数据同步

`/admin/data/sync` 是统一同步入口，**只做编排**，三类数据源的实际逻辑全部复用既有脚本（`app/sync_tasks.py` 调度）：

| 数据源 | 同步范围 | 复用的既有流程 |
|---|---|---|
| Bangumi 条目 | 一个或多个条目 ID（空格 / 逗号分隔，单次上限 30） | `scripts/import_bangumi.run_import` |
| 新番档期 | 一个档期（下拉来自本地已同步档期，或手填 `YYYYMM`） | `scripts/sync_yuc_schedule.run_sync` |
| 官方资讯 | 站点栏目 + 最多条数（上限 30，留空取最新 12 条） | `scripts/import_articles.run_import_articles` |

**官方资讯数据源**为 Aniplex 官方新闻站 [ANIPLEX NEWS BOX](https://news.aniplex.co.jp/)：抓取栏目列表（必要时顺着页内「view more」的 REST 端点翻页），再并发抓取详情页（4 线程），把完整标题、发布日期、摘要与正文写入 `articles`；正文内的内嵌视频转为媒体链接行。

可选栏目只包含站内能拿到「标题 + 日期 + 原文链接」的栏目：

| 入库 `category` | 站点栏目 | 正文来源 |
|---|---|---|
| 动漫资讯 | `?category=news` | 站内详情页 `detail.html?id=...` |
| 榜单资讯 | `?category=ranking` | 站内详情页 `detail.html?id=...` |
| 活动资讯 | `?category=event` | 条目自带官网原文链接（以「标题 + 原文链接」入库） |

站点的 YouTube（`?category=movie`）与播客（`?category=radio`）栏目只有播放器弹窗、没有文章页，不作为可选分类，避免写入只有标题没有内容的空壳记录。

关键行为：

- **两阶段**：先「预览」（只读取源数据并汇总，不写业务数据），再「确认写入」。
  「确认写入」入口只在刚预览完时出现，渲染在结果面板**顶部**（逐条明细表之前）与明细表下方各一处——
  明细最多 30 条，入口若只放在明细之后会被推到页面底部而难以发现。历史回看（`?run=`）不提供该入口。
- **幂等 / 不覆盖**：Bangumi 按 `bangumi_id` 唯一索引跳过；档期按 `(season_code, source_key)` 唯一约束更新或保持；
  资讯按 `original_url` / 标题命中跳过。重复点击确认不会产生重复数据，也不会用源站内容覆盖本地已有的摘要与正文。
- **单条失败不写半条**：详情页抓不到时该条计为失败并在结果里说明；站点不可用 / 结构变化由页面回显原因，不影响其它功能。
- **防重**：同一数据源同一时刻只允许一个任务执行（进程内互斥锁 + 运行记录 `running` 状态检查），超过 10 分钟的 `running` 记录视为僵死不再拦截。
- **运行记录**：每次预览 / 正式同步都写一条 `sync_task_runs`（数据源、范围、模式、触发来源、发起人、各类计数、失败原因、逐条明细快照）；页面下方展示最近 20 条，可 `?run=<id>` 回看。
- **无新增依赖**：只用标准库 `threading` 与既有 SQLAlchemy 数据层，未引入调度框架。后续接入定时任务时只需调用同一入口
  `app.sync_tasks.run_task(..., apply=True, trigger="scheduled")`，本模块无需改动。

---

## 6. 用户与访问控制

采用「服务端会话 Cookie + 角色判断」，未引入第三方登录或复杂 RBAC。

### 6.1 角色与权限边界

角色只有两种：`user`（普通用户）、`admin`（管理员）。Cookie 只存用户 id，**角色每次请求查库**。

| 资源 | 游客 | 普通用户 | 管理员 |
|---|---|---|---|
| 内容平台页面（`/`、`/articles`、`/contents/{id}`、`/schedule`、`/schedule/runs`） | ✅ | ✅ | ✅ |
| 内容 API（`/api/health`、`/api/contents*`、`/api/schedule*`） | ✅ | ✅ | ✅ |
| 测试管理平台页面（`/testcases*`、`/runs*`、`/batches*`、`/admin/data*`） | 跳登录 | 403 | ✅ |
| 测试管理 API（`/api/testcases*`、`/api/runs*`、`/api/batches*`） | 401 | 403 | ✅ |

### 6.2 拒绝方式

区分对待，而不是简单隐藏入口：

- **页面**：未登录 → 303 跳 `/login?next=...`；已登录但越权 → 403 HTML 页面。
- **API**：未登录 → 401；越权 → 403（均为 JSON）。

权限判断全部在**后端**执行（自定义异常 `LoginRequired` / `PermissionDenied` + FastAPI 异常处理器），
隐藏导航只是附带效果，直接输入管理 URL 或调用管理 API 同样被拒绝。
`/login` 的 `next` 参数经 `safe_next_url` 校验，只允许站内 `/` 开头，防开放重定向。

### 6.3 口令与会话

- **口令存储**：PBKDF2-HMAC-SHA256 + 每账号随机盐，明文口令不落库（`app/security.py`，仅标准库实现）。
- **登录 / 登出**：`/login`（GET 表单 / POST 提交）、`/logout`。
- **会话密钥**：`AUTH_SECRET_KEY` 必须通过 `.env` 覆盖（默认值仅供本地开发）；`SESSION_MAX_AGE_SECONDS` 默认 8 小时。

---

## 7. 项目结构

```text
acgn-test-platform/
├── app/
│   ├── main.py              # FastAPI 应用入口：装配路由、静态资源、守卫与异常处理器
│   ├── config.py            # 环境变量 / .env 读取（pydantic-settings）
│   ├── database.py          # 引擎、会话与依赖
│   ├── models.py            # SQLAlchemy 数据模型
│   ├── crud.py              # 数据库访问、查询与初始化逻辑（含角色常量与默认账号）
│   ├── schemas.py           # 请求数据校验模型
│   ├── api.py               # 内容相关 JSON API（公开）
│   ├── cases.py             # 测试用例 / 执行 / 批次 API（仅管理员）
│   ├── runner.py            # 用例执行器、批次执行与回归对比
│   ├── business_pages.py    # 内容平台页面路由（首页 / 详情 / 资讯 / 档期 / 同步历史）
│   ├── testing_pages.py     # 测试管理平台页面路由（用例 / 执行历史 / 批次）
│   ├── admin_pages.py       # 数据管理页面路由（查看、删除、重新同步、数据同步）
│   ├── sync_tasks.py        # 数据同步任务注册与执行入口（范围校验、防重、运行记录）
│   ├── auth.py              # 登录 / 登出、当前用户解析、页面与 API 守卫
│   ├── security.py          # 口令哈希与会话令牌（仅标准库）
│   ├── page_common.py       # 页面路由共享设施（模板、会话注解、404 渲染、布局选择）
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

---

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
| GET | `/` | 内容列表页 |
| GET | `/contents/{content_id}` | 内容详情页 |
| GET | `/articles` | 资讯列表页 |
| GET | `/articles/{article_id}` | 资讯详情页 |
| GET | `/schedule` | 新番档期页 |
| GET | `/schedule/runs` | 档期同步历史页 |

### 8.2 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/login` | 登录表单 / 提交，支持 `next` 回跳 |
| GET / POST | `/logout` | 登出 |

### 8.3 测试管理 API（仅管理员）

未登录返回 `401`，普通用户返回 `403`。

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
| GET | `/api/batches/{batch_id}/regression` | 与上一有效批次的回归对比 |

### 8.4 测试管理平台页面（仅管理员）

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

### 8.5 数据管理页面（仅管理员）

与测试管理平台同样的权限规则：未登录跳 `/login?next=...`，普通用户返回 403 页面。

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
| POST | `/admin/data/sync/apply` | 确认写入，正式同步后 303 跳转到该次结果页 |

> 批量删除使用**重复的 `ids` 表单字段**（`await request.form()` + `getlist("ids")`）。
> 当前 FastAPI 版本的 `list[int] = Form(default=[])` 收不到多值，不要改用该写法。

---

## 9. 数据模型

| 表 | 模型 | 用途 | 唯一约束 |
|---|---|---|---|
| `contents` | `Content` | ACGN 作品条目 | `bangumi_id` 唯一（Bangumi 导入去重） |
| `articles` | `Article` | 官方资讯文章 | 业务层按 `original_url` / 标题去重 |
| `anime_schedules` | `AnimeSchedule` | 新番档期条目 | `(season_code, source_key)` 联合唯一 |
| `test_cases` | `TestCase` | 接口测试用例定义 | — |
| `test_runs` | `ExecutionRecord` | 执行记录（含用例名 / 方法 / 路径快照） | — |
| `test_batches` | `ExecutionBatch` | 批次执行统计 | — |
| `sync_task_runs` | `SyncTaskRun` | 统一数据同步运行记录 | — |
| `schedule_sync_runs` | `ScheduleSyncRun` | 档期专用同步记录 | — |
| `users` | `User` | 平台账号（`user` / `admin`） | `username` 唯一 |

设计要点：

- `test_runs.case_id` 与 `sync_task_runs.actor_id` 均为**弱关联**（不设外键级联）：
  用例或账号删除后，历史记录仍可通过名称 / 路径快照完整解读。
- `test_runs.batch_id` 为 `NULL` 表示单条执行，**永不计入批次统计**。
- `anime_schedules.source_key` 取「规范化主标题」（去空白、小写），因为源站档期页没有稳定作品 ID。

---

## 10. 测试与代码检查

```powershell
# 运行全部测试
python -m pytest -q

# 只看失败摘要
python -m pytest -q --tb=no

# 生成 HTML 报告
python -m pytest --html=reports/api-test-report.html --self-contained-html

# 静态检查
ruff check app tests conftest.py
```

当前共 **327 项用例**，`test_api.py` 之外的全部通过（详见 [第 12 节](#12-已知问题)）。

测试结构：

- `tests/auth_helpers.py` 提供 `create_accounts(factory)` + `login_as_admin(client)`，
  管理相关测试需先建账号并登录；`tests/` 目录由 pytest prepend 模式加入 `sys.path`，可直接导入。
- 覆盖分层：内容 API、用例 CRUD、执行与记录、批次与回归、认证与越权、数据管理、同步任务、三类抓取脚本。
- 运行前请确认测试配置使用隔离的测试数据库，避免测试数据影响开发数据。

---

## 11. 数据库配置

通过 `DATABASE_URL` 环境变量读取，支持 `.env` 文件（参考 `.env.example`）。

```text
# 开发环境（默认）：SQLite
sqlite:///./local_dev.db

# 目标生产环境：MySQL
mysql+pymysql://用户名:密码@127.0.0.1:3306/acgn_test_platform?charset=utf8mb4
```

切换 MySQL 前需确认服务已启动、库已创建、连接参数正确。

- 建表在应用导入期执行（`crud.init_db`，幂等），数据库迁移方案与生产部署配置待后续完善（已装 `alembic`）。
- **请勿将包含真实密码的 `.env` 提交到版本控制。**

---

## 12. 已知问题

1. **`test_api.py` 中 5 项用例依赖本地种子数据的固定 id**
   （`test_get_content_detail`、`test_detail_page`、`test_search_hit`、`test_search_case_insensitive`、`test_search_matches_summary_only_keyword`）。
   它们直连 `local_dev.db` 并假设 id 为 1、5、6，一旦本地数据被后台删除或改动即失败。
   属于用例的数据依赖，不是代码缺陷：把种子重新播进干净库后这 5 项同样通过。
   后续计划改为不依赖固定 id。

2. **数据库迁移尚未落地**：建表由应用在导入期执行（`crud.init_db`，幂等），
   未接入 Alembic 迁移脚本；切换到 MySQL 后的生产部署配置也待完善。
