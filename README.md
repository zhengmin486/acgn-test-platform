# Otaku —— ACGN 内容平台与接口自动化测试后台

**Otaku** 是一个 ACGN 内容平台（动画 / 漫画 / 游戏 / 轻小说的作品库、资讯与档期）。

一个 FastAPI 应用，里面装了一半能正常浏览的 ACGN 内容站，和一半给这些接口做回归的自动化测试后台。同进程、同数据库，但页面布局和权限各走各的：内容站对所有人开放，测试后台只有管理员能进。

最早的起点其实只有后半块。2026 年 3 月它是一套给自家接口做回归用的小工具，内容站压根是为了给测试造点真实数据才顺手弄的，结果越长越大，现在反而是站更像主角。

## 版本更新

- **v0.1（2026-03）** 用例管理、进程内执行、批次间回归对比。当时没有页面，全靠 curl 和 pytest 脚本。
- **v0.2（2026-04）** 给测试后台补了 Web 页面。同进程、同库，页面与 API 各走各的路由，这个骨架一直用到现在。
- **v0.3（2026-05）** 内容站就是从这儿长出来的：作品 / 资讯 / 新番档期三张表，配上 Bangumi、Aniplex、Yuc.wiki 三个抓取脚本。
- **v0.4（2026-06）** 补齐门户那一套：分类入口、关键词搜索、作品详情、标签筛选。到这时候它已经不像个配角了。
- **v0.5（2026-07）** 账号与权限。三级角色、服务端会话、注册与邮箱验证、找回密码、邀请码；公开注册默认关掉，走邀请制。
- **v0.6（2026-08）** 个人空间和阿宅画像。收藏（看过 / 未看 / 想看的标注跟收藏是同一行数据）、浏览记录、头像，以及读 Bangumi 公开收藏算出的六维雷达和 LLM 锐评。
- **v0.7（2026-09）** 三个散装同步表单收拢成统一编排：预览 → 确认写入，顺带把词条申请审核和标签归一也做了。
- **v0.8（2026-10 上旬）** 封面本地化（按需派生缩略图）、作品关系识别，以及用例从接口清单自动生成。
- **v1.0（2026-10 中旬）** 部署上线。页面 / API / 同步 / 备份都跑通了，测试用例也攒到 990 多项。

## 界面截图

先看图有个概念就行，都是 1440×900 截的。作品的封面是本地还没抓图时的占位样式，跑过 `fetch_covers.py` 之后会换成真封面：

| 页面 | 截图 | 页面 | 截图 |
|---|---|---|---|
| 首页 `/` | ![首页](docs/screenshots/home.webp) | 作品库 `/contents` | ![作品库](docs/screenshots/contents.webp) |
| 作品详情 `/contents/{id}` | ![作品详情](docs/screenshots/detail.webp) | 资讯 `/articles` | ![资讯](docs/screenshots/articles.webp) |
| 新番档期 `/schedule` | ![新番档期](docs/screenshots/schedule.webp) | 阿宅画像（免登录试算） | ![阿宅画像](docs/screenshots/persona.webp) |
| 测试后台（管理员） | ![测试后台](docs/screenshots/testcases.webp) | 数据管理（管理员） | ![数据管理](docs/screenshots/manage.webp) |
| 个人空间 `/me` | ![个人空间](docs/screenshots/me.webp) | | |

## 功能

**内容站（公开）**

`/contents` 能按类型筛、按关键词搜、按标签归类，翻页也还算顺手；作品详情页里除了标签，还有制作信息、角色声优和同系列作品。资讯走的是 Aniplex 官方新闻站，导入时会自动识别题材标签、作品信息（作者 / 公司 / 系列 / 译名）并跟站内作品关联上——标题没出现的作品，也能靠作者、公司与系列名反查补上。`schedule` 是按季度切换的新番档期。

登录后多了 `/me`：收藏、浏览记录、头像、词条申请进度都在那儿。资料与凭据另开一页 `/settings`（账号设置，入口在右上角头像菜单）：个人资料（性别 / 生日 / 所在地 / 简介）在 `/settings/profile`，邮箱与手机的绑定、更换、解绑在 `/settings/security`。另外 `/persona` 填个 Bangumi 用户名就能免登录试算画像，登录后可以绑定，生成六维雷达和 LLM 锐评文案。

**测试后台（仅管理员）**，还是当年那套路子：

- 用例增删改查，单条执行，或者把启用的用例整批跑成一个批次
- 批次之间能拉出来对比，看哪些从通过变成了失败
- 执行器跑在应用进程里（`TestClient` 发请求），只认本站本地路径，够不着外部地址
- 「从接口清单生成」扫一遍全部只读且不带路径参数的路由，自动生成冒烟用例

## 快速开始

Python 3.13，默认 SQLite，不装额外服务就能跑。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

内容站在 <http://127.0.0.1:8000/>，测试后台在 <http://127.0.0.1:8000/testcases>。

首次启动会建表，按需要写一份开发用的种子数据（`SEED_DEMO_DATA=false` 就换成干净库），再按 `.env` 里的内容建初始账号——库里已经有账号时它不会动。日常维护账号可以这样：

```powershell
python scripts/create_user.py --list
python scripts/create_user.py --username alice --role admin
```

> 部署场景下记得先用 `.env` 覆盖默认口令和 `AUTH_SECRET_KEY`，顺便确认 `.env` 没被提交上去。

数据除了在后台「数据同步」里点，也可以直接跑脚本：

```powershell
python scripts/import_bangumi.py --help       # 作品条目
python scripts/sync_yuc_schedule.py --help    # 新番档期
python scripts/import_articles.py --help      # 官方资讯
```

作品条目取的是 [bangumi/Archive](https://github.com/bangumi/Archive) 的每周全量导出和 Bangumi API；档期来自 [Yuc.wiki](https://yuc.wiki/)；资讯正文抓自 [ANIPLEX NEWS BOX](https://news.aniplex.co.jp/)。

## 项目结构

```
app/
  main.py            # FastAPI 应用和路由装配
  models.py          # SQLAlchemy 模型
  crud.py            # 数据访问，外加建表时的幂等补列
  auth.py            # 会话、口令、角色
  api.py             # JSON API（/api/**）
  *_pages.py         # 服务端渲染的页面
  templates/         # Jinja2 模板
scripts/             # 抓取、同步、封面、账号维护
tests/               # pytest，每个用例跑在独立临时库上
conftest.py
```

## 开发

```powershell
pytest -q          # 现在 990 多项
ruff check .
```

先说最容易踩的两个坑。一是这项目**没有 Alembic**，加列得写成 `crud.init_db()` 里的幂等 `ALTER`（照着 `ensure_user_email_columns` 写），只改模型是没用的。二是 `conftest.py` 默认把测试里的联网关掉了，依赖外部站点的用例要自己 monkeypatch。环境变量都收在 `app/config.py`，常用的几个是 `DATABASE_URL`、`AUTH_SECRET_KEY`、`SEED_DEMO_DATA`、`REGISTRATION_MODE`、`COVER_STORAGE_DIR`。

## 已知限制

内容站和测试后台共用一个进程一个库，测试后台的写入会落在同一份数据上。用例执行器只认本站本地路径，拿它测外部服务是不行的。数据库也没有迁移工具，升级靠 `init_db` 里的幂等 DDL——上面提过一嘴，这里再啰嗦一遍。

## License

MIT，见 [LICENSE](./LICENSE)。
