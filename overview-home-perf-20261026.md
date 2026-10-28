# 首页 / 列表页卡顿诊断与优化（2026-10-26）

> 状态：**两轮优化均已实施并部署上线**（2026-10-26）。
> 首页 TTFB 1.71s → 0.52s（`recommend_contents` + `canonical_genre` 缓存）；
> `/contents` TTFB 1.18s → 0.18~0.26s（`list_tags_with_counts` 去冗余 join + count 去 order_by）。
> 改动均在 `app/crud.py`（另 `app/tagging.py` 加 `lru_cache`）。

---

## 1. 现象与实测

线上（154.8.194.198:8000，2C2G，12012 部作品）实测 TTFB：

| 页面 | TTFB | 总耗时 | HTML 体积 |
|---|---|---|---|
| `/`（首页） | **1.71s** | 1.88s | 246 KB |
| `/contents` | **1.18s** | 1.98s | 124 KB |
| `/schedule` | 0.19s | 0.30s | 122 KB |
| `/articles` | 0.10s | 0.22s | 65 KB |

对照：本地空库（11 部作品）首页仅 53ms。→ 慢与**数据量**强相关，不是网络也不是框架。

`curl` 走系统代理时每个请求固定 +2s，那是本机代理的测量噪声（绕过代理后 3ms），**不是网站问题**，别据此判断。

## 2. 根因（定量）

本地造了与线上同规模的数据（12012 部作品 × 10 标签 = 120120 行关联），
用 `DATABASE_URL=sqlite:///./.workbuddy/tmp/perf.db` 跑首页并 cProfile：

```
单次首页 = 1.85s
  └─ recommend_contents         cumtime 1.779s   ← 占 96%
```

`recommend_contents`（`app/crud.py:758`）内部耗时构成：

| 项 | 次数 | 耗时 |
|---|---|---|
| `sqlite3.Cursor.fetchall` | 24 | 0.851s |
| SQLAlchemy `_instance`（ORM 实体构造） | **361560** | 1.106s |
| `json.decoder.decode`（`aliases` JSON 列） | **361560** | 0.900s |
| `tagging.canonical_genre`（标签归一） | 360408 | 0.474s |

`361560 / 3 次请求 = 120520` —— 正是「12012 部 × 10 标签」的 JOIN 行数。

**慢的原因**：

```python
rows = db.execute(
    select(Content, Tag.name)      # ← 取整个 ORM 实体
    .join(ContentTag, ...).join(Tag, ...)
    .order_by(Content.id, Tag.id)
).all()
```

1. 把 **12 万行**「作品 × 标签」结果全部实例化为完整 `Content` ORM 对象——
   实际只需要 12012 个作品，却造了 12 万个对象（10 倍冗余）；
2. `Content.aliases` 是 JSON 列，**每行都触发一次 `json.loads`** → 12 万次 JSON 解码；
3. 排序与打分只需要 6 个标量字段，却把整行（含 `summary` 长文本）都搬进了 Python。

一句话：**为了给 12012 部作品打分，先把全表标签关联展开成 12 万个 ORM 对象搬进内存。**

## 3. 方案

### 方案 A（推荐，最小改动、治本）

把 `select(Content, Tag.name)` 改成只取打分真正需要的标量列，不再构造 ORM 实体：

```python
rows = db.execute(
    select(
        Content.id, Content.bangumi_id, Content.title,
        Content.type, Content.year, Content.rating, Tag.name,
    )
    .join(ContentTag, ContentTag.content_id == Content.id)
    .join(Tag, Tag.id == ContentTag.tag_id)
    .order_by(Content.id, Tag.id)
).all()

plain: dict[int, dict] = {}
tags_by_content: dict[int, set[str]] = {}
site_tags: set[str] = set()
for cid, bangumi_id, title, ctype, year, rating, tag_name in rows:
    if cid not in plain:      # 同一作品多行，只需首次登记
        plain[cid] = {"id": cid, "bangumi_id": bangumi_id, "title": title,
                      "type": ctype, "year": year, "rating": rating}
    name = str(tag_name)
    canonical = canonical_genre(name) or name
    site_tags.update((name, canonical))
    tags_by_content.setdefault(cid, set()).add(canonical)
```

打分循环内把 `content = content_by_id[content_id]` 换成 `item = plain[content_id]`，
取 `item["bangumi_id"] / ["title"] / ["type"] / ["year"] / ["rating"]`。

**兼容性**：函数返回值仍是同样的 dict 列表（`content_id/title/type/year/rating/matched/score/reason`）。
下游 `dedupe_series_picks` 只读 `item["content_id"]`，随后自己用 `get_contents_by_ids` 查实体；
`persona.py:680 / :804` 同样只消费 dict。→ **返回契约零变化**。

**预期**：12 万次 ORM 实例化与 JSON 解码全部消除 → 首页 `recommend_contents` 由 1.78s 降到约 **0.3~0.5s**。

### 方案 A+（顺手，收益/风险比最高）

`canonical_genre` 被调用 12 万次，但标签名种类只有几百个。给它加 `functools.lru_cache`
（纯函数：str → str），再省掉约 0.16s。

### 方案 B（进阶，可选）

把「命中过滤」下推到 SQL：先取全站 distinct 标签名（轻量）算出展开后的口味集，
再只查命中这些标签的作品，行数从 12 万降到几千级；避雷用 `NOT EXISTS` 子查询在 SQL 侧排除。

- 收益：可再降到 0.05s 级。
- 风险：口味词归一（`expand_genre_names` / `canonical_genre`）目前是 Python 侧逻辑，
  下推时若只传原始名，可能漏掉需要归一的写法 → **行为不完全等价**，需要逐条对拍验证。
- 建议：**A 上线并验证后**，若仍需更快再上 B。

### 方案 C（缓存，只能当补充）

对游客口径（`list_home_recommendations`）加进程内 TTL 缓存，可让游客首页接近 0 耗时。

**但不能单独依赖它**：
- 登录用户走 `persona.build_user_home_picks`，结果个性化，无法按同一份缓存复用；
- 缓存会让新入库作品延迟 N 分钟才上首页（可接受则需显式定 TTL）。

所以顺序是：**先 A（治本），缓存只做锦上添花**。

## 4. 影响面与验证

**改动文件**：仅 `app/crud.py`（`recommend_contents`），`app/tagging.py`（A+ 的 lru_cache）。

**模板 / API / 数据库**：均不涉及，无需迁移。

**验证闭环（已有现成工具）**：
- 造数库已就绪：`.workbuddy/tmp/perf.db`（12012 部 / 120120 条标签关联），
  基准脚本 `.workbuddy/tmp/perf_bench.py`（不入库，可随时删）。
- 复测命令：
  `PYTHONPATH=".;.venv/Lib/site-packages" E:/python/python.exe .workbuddy/tmp/perf_bench.py`
- 判据：首页单次耗时从 ~1.1~1.8s 降到 <0.5s，且 `recommend_contents` 的 cumtime 占比大幅下降。

**回归测试**：改 `crud.py` 会自动带出较大测试集（`scripts/devtest.py` 按 git 改动选相关文件 +
app 模块反向传播一层）。重点看首页推荐、成分检测推荐相关的用例。
既有文案与测试断言依赖推荐顺序——A 方案**不改排序口径**（命中数 → 评分 → id），顺序应完全一致。

**上线**：`crud.py` 约 281KB，走 deploy.md 的单文件直推路径（上限 3MB）可行；
推完按惯例 `cd /opt/acgn-test-platform && .venv/bin/python -c "import app.main"` 预检再 restart。

## 5. 实施结果（A + A+）

已改动两个文件：

- `app/crud.py` · `recommend_contents`：`select(Content, Tag.name)` →
  只取 `id / bangumi_id / title / type / year / rating` + `Tag.name`，
  作品标量存进 `plain_by_id`（同作品多行只登记一次），打分循环改读标量。
- `app/tagging.py` · `canonical_genre`：加 `@lru_cache(maxsize=8192)`。
  它是纯函数（查模块级常量词表 + 字符串归一），词表运行期不变，缓存不会读到过期结果；
  已确认没有测试 monkeypatch 该词表。

**同规模复测（12012 部 / 120120 条标签关联，`.workbuddy/tmp/perf.db`）**：

| 指标 | 改前 | 改后 |
|---|---|---|
| 首页单次耗时 | 1.096s | **0.434s** |
| `recommend_contents` cumtime | 1.779s | **0.500s** |
| ORM 实体构造 | 361560 次 | 1200 次 |
| `json.loads` | 361560 次 | 1200 次 |

剩余的 1200 次是 `dedupe_series_picks` 用 `get_contents_by_ids` 取最终入选作品的正常开销。

**正确性验证**：

- 测试 `test_persona` + `test_tagging` = **93 passed**；
  `test_home_recommend_user` + `test_home_posters` + `test_ui_landing` + `test_content_search` = **55 passed**。
- 新旧算法对拍（`.workbuddy/tmp/equiv_check.py`，用旧实现原样复制跑同一份数据）：
  首页游客口径 32 条、带排除集 10 条、带权重 6 条、空口味 0 条 —— **逐条完全一致**，排序未变。
- 本地真实库冒烟：`/` 99ms、`/contents` 43ms、`/articles` 21ms，均 200。

线上预期：首页 TTFB 由 1.71s 降到约 0.6~0.7s（线上数据标签种类更多，实际以部署后实测为准）。

## 6. 部署记录（2026-10-26）

**部署方式：精确补丁，不是整文件覆盖。**

核对发现服务器 `crud.py`（md5sum `12f1d80a…`）既不等于本地 HEAD（`1f1f85ca…`）也不等于本地当前版本，
一度判断存在约 7KB 差异。实际原因是**换行符**：服务器文件是 CRLF，`md5sum` 得 `12f1d80a`，
用 Python 文本模式读取（CRLF→LF）则是 `d45830fb…`，与本地改前版本**逐字一致**。
即便如此，整文件覆盖仍会把本地 CRLF 换行写上去、让整个文件的 diff 全变，
所以改成只替换目标代码块（服务器上跑 Python 脚本做精确字符串替换，带 `count == 1` 断言，
不匹配就报错退出、不改动任何文件）。

流程：备份 `/tmp/crud.py.bak-20261026`、`/tmp/tagging.py.bak-20261026` → 语法检查 →
打补丁 → `grep` 确认新代码在位、旧写法残留 0 处 → `.venv/bin/python -c "import app.main"` 预检 →
`systemctl restart acgn` → HTTP 校验。

**线上实测（服务端回环，不含公网开销）**：

| 页面 | 部署后 | 部署前 |
|---|---|---|
| `/` | **0.38~0.49s** | 1.71s（公网 TTFB） |
| `/contents` | 0.19s | 1.18s |
| `/articles` | 0.03s | 0.10s |

**公网复测（同口径对比）**：首页 TTFB **1.71s → 0.52s**（-70%），
`/contents` 1.24s（本次未动，符合预期），`/articles` 0.13s。

首页 HTML 体积部署前后都是 **246183 字节**——推荐结果逐条未变，只是算得快了。

**回滚**（如需）：
```
cp /tmp/crud.py.bak-20261026 /opt/acgn-test-platform/app/crud.py
cp /tmp/tagging.py.bak-20261026 /opt/acgn-test-platform/app/tagging.py
systemctl restart acgn
```

## 7. `/contents` 卡顿（2026-10-26 续查，已实施并部署上线）

首页优化上线后，`/contents` 仍是线上第二慢页（TTFB 1.18s）。本机代理噪声已排除，
转而在本地造**贴近线上的分布**复现：12012 部 × 真实量级文本（summary ~420B/行、
aliases 为 JSON 别名数组）× 4000 个题材标签 × 25 标签/部 = 30 万行 `content_tags`
（造数库 `.workbuddy/tmp/perf4.db`）。

**默认 `/contents`（无筛选）每请求跑 7 个聚合 + 取 20 条**。逐条测出瓶颈：

| 查询 | 改前 | 改后 | 说明 |
|---|---|---|---|
| `list_tags_with_counts`（题材 chips） | **183 ms** | **23 ms** | 冗余 `JOIN contents` 让 SQLite 物化 30 万行关联 |
| `search_contents_page` 的 count 子查询 | 9.3 ms | 0.1 ms | 子查询带 ORDER BY，SQLite 不能扁平化，全字段物化整张 contents |
| `list_media / years / count_by_type` | ~10 ms | ~10 ms | 走 `contents` 列，已很快 |
| 取 20 条实体 | 0.3 ms | 0.3 ms | — |

**根因 1（主因）：`list_tags_with_counts` 的冗余 `JOIN contents`。**
计数只需要 `content_tags ↔ tags`，`Content` 表纯属多余；但那一步 inner join 让 SQLite
把几十万行标签关联物化成临时表再聚合。实测裸 SQL：含 Content join **183ms** → 去掉 **14.7ms**。
`Content` 只在「按 `content_type` 过滤」时才是必需的（用 `Content.type` 加 where），
所以改成：**`content_type=None` 时不再 join `Content`；有值时才 join**。

**根因 2（次因）：count 子查询 `select(func.count()).select_from(stmt.subquery())`。**
`stmt` 是 `select(Content).order_by(Content.id)`，包成子查询后 `ORDER BY` 阻止 SQLite
扁平化，必须物化整张 contents（含 summary/aliases 大文本）再数行。去掉 `order_by`
（`stmt.order_by(None)`）后扁平化，0.1ms。

**改动文件**：仅 `app/crud.py` 两处——
`list_tags_with_counts`（条件化 join）+ `search_contents_page`（count 去 order_by）。

**正确性验证**：
- 默认路径新结果与旧实现（含 Content join）前 31 条**逐条完全一致**；
  `content_type='anime'` 过滤路径新结果与旧实现**完全一致**；count 总数新旧一致（12012）。
- 测试 `test_content_search` + `test_contents_browse` + `test_search_recall` + `test_media` + `test_api`
  = **152 passed**；本地 `/contents` 路由冒烟 200，题材 chips / 类型 Tab 均在位。
- 复用 `.workbuddy/tmp/perf4.db` + `.workbuddy/tmp/build_perf4.py` 可随时复测。

**线上预期**：`list_tags_with_counts` 从 183ms 降到 15~25ms（本地），`/contents` TTFB 应
从 1.18s 降到约 0.3~0.5s。部署后需公网复测确认（与首页同口径）。

### 7.1 部署记录（2026-10-26，已上线）

**部署方式：精确补丁 + 先本地强校验。** 服务器 `crud.py` 是 LF、本地是 CRLF（内容一致，
字节差 ≈ 行数 7293），且线上与本地存在 188 字节既存分歧 → 仍不整文件覆盖。

**部署前强校验（关键）**：本地反向重建「服务器应有版本」，断言其 md5 == 服务器 md5
`37bf14097e5c65a076b3bf766c33ffa9`，证明 OLD 文本与服务器**逐字节一致**后，才生成补丁。

**踩坑与修正**：第一版补丁用 `src.index("    if content_type is not None:")` 取块，
命中了文件里更早的另一个函数，导致 NEW1 切片错误——补丁把别的函数代码注入
`list_tags_with_counts`，该函数被改坏（`stmt` 未定义）。**已从备份 `/tmp/crud.py.bak-contents-20261026`
完整恢复**，改用唯一短语锚点（「把前 N 个名额吃光」）重做，一次命中。

**部署结果**：

| 指标 | 部署前 | 部署后 |
|---|---|---|
| `/contents` 公网 TTFB | 1.18s | **0.18~0.26s**（-80%） |
| `/contents` 服务端回环 | ~1.1s | **0.084s** |
| `/` 首页 TTFB | 0.52s | 0.55s（未动，维持） |
| `/articles` | 0.10s | 0.11s（未动） |

部署后线上 `crud.py` md5 = `ca5cff7b49c32ed8df8da5910a2c0bf8`，与本地规范化版（LF）逐字节一致。
预检：`select_from(ContentTag)`=3、`stmt.order_by(None)`=1、旧 count 写法残留=0、`import app.main` OK、
`systemctl is-active acgn` = active。

**回滚**：

```bash
cp /tmp/crud.py.bak-contents-20261026 /opt/acgn-test-platform/app/crud.py
systemctl restart acgn
```

（`/tmp/crud.py.bak-contents-20261026` 是本次改动前、含首页修复的 LF 版，md5 `37bf140…`；
`/tmp/crud.py.bak-20261026` 是更早的 CRLF 原始版。）

## 8. 仍未处理（待定）

- **gzip 未开启**（`gzip_enabled=False`）：首页 HTML 246KB、`bootstrap.min.css` 232KB、
  `style.css` 220KB 全部明文传输。开启后首页 HTML 约 35KB，弱网/移动端首屏会明显变快，
  代价是 2C2G 上多一点 CPU。
- 静态缓存已是 `max-age=86400`、媒体 7 天，配置合理，无需动。
