# ACGN 内容服务与 Web 接口自动化测试平台

本项目是一个面向 ACGN 内容服务场景的 Web 接口自动化测试与回归测试结果分析平台

项目将 ACGN 内容展示业务与接口测试流程结合，提供内容查询、测试用例管理、用例执行及执行记录查询等功能，并围绕回归测试结果开展分析。项目具体功能以当前代码和实际运行结果为准。

## 1. 项目功能

### 1.1 ACGN 内容服务

- 内容列表与详情展示页面。
- 内容列表、详情和关键词搜索等 JSON API。
- 健康检查接口。
- 使用数据库保存业务数据；开发环境可使用 SQLite。

### 1.2 接口自动化测试

- 测试用例的创建、查询、编辑和删除。
- 单条测试用例执行。
- 执行结果与历史记录持久化。
- 执行记录列表、详情查询及按用例筛选。
- 批量执行与回归测试结果分析功能（以当前实现为准）。

### 1.3 用例执行安全约束

测试用例仅接受本站本地路径。执行时在应用进程内向自身发起请求，不访问外部地址或内网，不跟随重定向，设置超时且不读取响应体。执行异常会记录为失败。删除测试用例后，历史执行记录仍保留用例名称、请求方法和路径快照。

## 2. 技术栈

- **Web 框架：** FastAPI
- **数据访问：** SQLAlchemy
- **页面模板：** Jinja2
- **页面样式：** Bootstrap CDN 与少量自定义 CSS
- **自动化测试：** pytest
- **代码检查：** Ruff
- **开发数据库：** SQLite
- **目标数据库：** MySQL（需配置连接信息后使用）

## 3. 项目结构

```text
acgn-test-platform/
├── app/
│   ├── main.py        # FastAPI 应用入口
│   ├── api.py         # 内容相关 JSON API
│   ├── cases.py       # 测试用例管理及执行相关 API
│   ├── runner.py      # 用例执行器与执行记录
│   ├── business_pages.py  # 业务页面路由（内容/资讯/新番档期/同步历史）
│   ├── testing_pages.py   # 测试平台页面路由（用例管理/执行历史/批次）
│   ├── page_common.py     # 页面路由共享设施（模板、会话注解、404 渲染）
│   ├── schemas.py     # 请求数据校验模型
│   ├── crud.py        # 数据库访问、查询与初始化逻辑
│   ├── models.py      # 数据模型
│   ├── database.py    # 数据库引擎、会话与依赖
│   ├── config.py      # 环境变量及数据库配置
│   ├── fake_data.py   # 开发测试种子数据
│   ├── templates/     # Jinja2 页面模板
│   └── static/        # 静态资源
├── scripts/           # 辅助脚本
├── tests/             # 自动化测试
├── reports/           # 测试报告等输出文件
├── conftest.py        # pytest 根配置
├── requirements.txt
├── .env.example       # 环境变量示例
└── README.md
```


## 5. 主要接口

### 5.1 内容服务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/contents` | 查询内容列表 |
| GET | `/api/contents/search?q=关键词` | 按标题或简介搜索内容 |
| GET | `/api/contents/{content_id}` | 查询内容详情 |
| GET | `/` | 内容列表页面 |
| GET | `/contents/{content_id}` | 内容详情页面 |

### 5.2 测试用例管理与执行

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/api/testcases` | 查询用例列表 / 创建用例 |
| GET / PUT / DELETE | `/api/testcases/{case_id}` | 查询 / 编辑 / 删除用例 |
| POST | `/api/testcases/{case_id}/execute` | 执行单条用例 |
| GET | `/api/runs` | 查询执行记录，可按 `case_id` 筛选 |
| GET | `/api/runs/{run_id}` | 查询执行记录详情 |
| GET / POST | `/testcases` | 用例管理页面 / 创建用例 |
| GET / POST | `/testcases/{case_id}/edit` | 用例编辑页面 |
| POST | `/testcases/{case_id}/delete` | 删除用例 |
| POST | `/testcases/{case_id}/execute` | 在页面执行用例 |
| GET | `/runs` | 执行历史页面，可按用例筛选 |
| GET | `/runs/{run_id}` | 执行记录详情页面 |

批量执行、回归分析相关入口请以当前应用的 API 文档和页面为准；本 README 不列出尚未核实的具体路径或请求参数。

## 6. 测试与代码检查

在项目根目录运行自动化测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

运行 Ruff 静态检查：

```powershell
.\.venv\Scripts\ruff.exe check app tests conftest.py
```

项目曾生成 `reports/api-test-report.html` 测试报告。该报告记录了 66 项测试全部通过、0 项失败；该数字仅代表该份报告覆盖的测试，不应直接视为项目当前全部测试的总数。运行测试前应确认测试配置使用隔离的测试数据库，避免测试数据影响开发数据。

## 7. 数据库配置

应用通过 `DATABASE_URL` 环境变量读取数据库连接配置，并支持 `.env` 文件。可参考项目中的 `.env.example`。

MySQL 连接串格式示例：

```text
mysql+pymysql://用户名:密码@127.0.0.1:3306/acgn_test_platform?charset=utf8mb4
```

开发环境默认使用 SQLite：

```text
sqlite:///./local_dev.db
```

请勿将包含真实密码的 `.env` 文件提交到版本控制。MySQL 使用前需要确认本机服务已启动、数据库已创建且连接参数正确。数据库迁移方案及生产环境部署配置应以项目后续实际完成情况为准。
