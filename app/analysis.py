"""结果分析（方案第九节第 3 步）：失败归因 + 路由分组统计。

两条铁律，改这个文件前先读 `docs/测试平台毕设方案.md` 的第 6.5、7.1、7.2、9.2 节：

1. **只数失败**：通过 / 跳转 / 未执行都不进归因分布。跳转在 `test_runs.passed`
   里是占位 True，未执行根本没有执行记录，所以取数必须按批次口径分别取：
   新批次读 `case_set.cases[].outcome == 'fail'`，legacy 批次与单条执行按
   `passed=False` 回退（方案 5.6）。
2. **错误文本只读、不改写**：`test_runs.error` 是原始证据。旧记录的全角冒号与
   无前缀文本在读取时经 `normalize_error` 归一，绝不 UPDATE 历史数据（6.5）。

本模块不写库、不新建表；归因纯自动归一，判不了的落 To Investigate，
No Defect 本期不产出（决策 4 / 7.2）。

v0.6 分口径（方案 7.1 / 9.5）：批次归因与单条执行归因分开统计、分开展示，
单条执行不混进批次失败分母；最慢用例只取有效 `duration_ms`，是耗时观察
而非性能测试，且全程只读，不改写任何执行结果或批次统计。
"""

import json
import re

from app import crud
from app.models import CASE_SET_SCHEMA, ExecutionBatch, ExecutionRecord
from app.runner import (
    ERR_APP,
    ERR_PATH,
    ERR_REQUEST,
    ERR_SERVER,
    ERR_STATUS,
    ERR_TOOL,
)

# 五类缺陷归因（ReportPortal 分类体系，本项目只自动产出前四类）。
DEFECT_TYPES = (
    "To Investigate",
    "Product Bug",
    "Automation Bug",
    "System Issue",
    "No Defect",
)

# 6.5：旧记录的全角冒号前缀 → 半角，读取时归一，不改写库里的原文。
_LEGACY_FULLWIDTH = (
    ("路径不合法：", ERR_PATH),
    ("请求异常：", ERR_REQUEST),
    ("服务端错误：", ERR_SERVER),
    ("状态码不符：", ERR_STATUS),
    ("应用异常：", ERR_APP),
    ("工具异常：", ERR_TOOL),
    ("跳转：", "跳转:"),
)
# 旧记录第三种写法：完全没有前缀的状态码不符文本。
_LEGACY_STATUS_MISMATCH = re.compile(
    r"^实际状态码\s*\d+\s*与预期\s*\d+\s*不一致"
)

# 7.2：只有这三类映射是有把握的自动归一；5xx / 应用异常 / 无前缀一律待调查。
_PREFIX_TO_DEFECT = {
    ERR_STATUS: "Product Bug",
    ERR_PATH: "Automation Bug",
    ERR_TOOL: "Automation Bug",
    ERR_REQUEST: "System Issue",
    ERR_SERVER: "To Investigate",
    ERR_APP: "To Investigate",
    "跳转:": "To Investigate",  # 跳转不该进失败集；真出现了也不猜根因
}

# 9.2：分组算法的例外人工映射（形式同 EXPECTED_STATUS_OVERRIDES）。
# 只有当算法结果与预期不符时才往这里加一条，不靠路径前缀猜业务模块。
ROUTE_GROUP_OVERRIDES: dict[str, str] = {}

_API_PREFIXES = frozenset({"api", "manage", "admin"})


def normalize_error(error: str | None) -> str:
    """把历史 error 文本归一到半角前缀写法（6.5 的纯函数实现）。

    三类旧文本：全角冒号前缀、无前缀的「实际状态码 X 与预期 Y 不一致」、
    其他无前缀（原样返回，归因时兜底 To Investigate）。
    """
    text = error or ""
    for old, new in _LEGACY_FULLWIDTH:
        if text.startswith(old):
            return new + text[len(old):]
    if _LEGACY_STATUS_MISMATCH.match(text):
        return ERR_STATUS + " " + text
    return text


def classify_failure(error: str | None) -> str:
    """单条失败记录的归因；无前缀或判断不了统一 To Investigate。"""
    text = normalize_error(error)
    for prefix, defect in _PREFIX_TO_DEFECT.items():
        if text.startswith(prefix):
            return defect
    return "To Investigate"


def route_group(path: str | None) -> str:
    """按 9.2 的可复现算法给 path 分组，丢弃空段与以 `{` 开头的路径参数段。

    - `/api/contents/7` → `api.contents`；`/manage/contents` → `manage.contents`
    - `/contents/7` → `page.contents`；`/` → `page.root`
    - 先查 ROUTE_GROUP_OVERRIDES，命中即用人工值
    """
    raw = path or ""
    if raw in ROUTE_GROUP_OVERRIDES:
        return ROUTE_GROUP_OVERRIDES[raw]
    segments = [s for s in raw.split("/") if s and not s.startswith("{")]
    if not segments:
        return "page.root"
    first = segments[0]
    if first in _API_PREFIXES:
        return f"{first}.{segments[1]}" if len(segments) > 1 else first
    return f"page.{first}"


def _snapshot(batch: ExecutionBatch) -> dict | None:
    """解析判定快照；legacy 或结构版本不符返回 None（不伪造口径）。"""
    if not batch.case_set:
        return None
    try:
        data = json.loads(batch.case_set)
    except (ValueError, TypeError):
        return None
    return data if data.get("schema") == CASE_SET_SCHEMA else None


def is_legacy(batch: ExecutionBatch) -> bool:
    """legacy 判定与 runner / 通过率同口径：没有快照或结构版本不符。"""
    return _snapshot(batch) is None


def _failed_runs_for_batch(db, batch: ExecutionBatch) -> list[ExecutionRecord]:
    """单批失败记录：新批次按快照 outcome，legacy 按 passed=False（5.6）。"""
    runs = crud.list_runs_for_batch(db, batch.id)
    snapshot = _snapshot(batch)
    if snapshot is None:
        return [r for r in runs if not r.passed]
    fail_run_ids = {
        entry.get("run_id")
        for entry in snapshot.get("cases", [])
        if entry.get("state") == "executed" and entry.get("outcome") == "fail"
    }
    return [r for r in runs if r.id in fail_run_ids]


def collect_failed_runs(db, batch_id: int | None = None) -> list[ExecutionRecord]:
    """**批次**失败记录，归因的批次口径输入；不含单条执行（方案 7.1 v0.6）。

    batch_id 给定时只取该批；不指定时覆盖全部批次。新批次按快照
    `outcome='fail'` 取数，legacy 按 `passed=False` 回退（5.6）。单条执行
    走 `collect_single_failed_runs`，与批次分开展示、分别统计。
    """
    if batch_id is not None:
        batch = crud.get_batch(db, batch_id)
        return _failed_runs_for_batch(db, batch) if batch is not None else []
    by_id: dict[int, ExecutionRecord] = {}
    for batch in crud.list_batches(db):
        for run in _failed_runs_for_batch(db, batch):
            by_id[run.id] = run
    return [by_id[key] for key in sorted(by_id)]


def collect_single_failed_runs(db) -> list[ExecutionRecord]:
    """单条执行（`batch_id=NULL`）的失败记录，legacy 口径 `passed=False`。

    与批次归因分开展示、分别统计（方案 7.1）；不进批次回归比较。
    """
    return sorted(
        (r for r in crud.list_runs(db) if r.batch_id is None and not r.passed),
        key=lambda r: r.id,
    )


def _distribution(runs: list[ExecutionRecord]) -> dict:
    """给定失败记录集算五类分布；分母严格等于传入条数。"""
    counts = {defect: 0 for defect in DEFECT_TYPES}
    for run in runs:
        counts[classify_failure(run.error)] += 1
    total = len(runs)
    shares = {
        defect: (round(count / total * 100, 1) if total else None)
        for defect, count in counts.items()
    }
    return {"total": total, "counts": counts, "shares": shares, "runs": runs}


def attribution(db, batch_id: int | None = None) -> dict:
    """**批次**五类归因分布；分母严格等于批次失败数，不含单条执行。"""
    return _distribution(collect_failed_runs(db, batch_id))


def single_attribution(db) -> dict:
    """**单条执行**五类归因分布，独立分母，与批次分开展示。"""
    return _distribution(collect_single_failed_runs(db))


def route_group_matrix(db) -> dict:
    """路由分组 × 批次失败数矩阵（9.1 第二条的页面数据源）。

    行按失败总数降序、列名按批次 id 升序；只数失败，与归因分布同一批输入。
    """
    batches = list(reversed(crud.list_batches(db)))  # id 升序，表头从左到右读
    batch_ids = [b.id for b in batches]
    rows: dict[str, dict[int, int]] = {}
    for batch in batches:
        for run in _failed_runs_for_batch(db, batch):
            group = route_group(run.path)
            rows.setdefault(group, {}).setdefault(batch.id, 0)
            rows[group][batch.id] += 1
    # 单条执行的失败没有批次列，用哨兵 id=0 单独累计，不混进任何批次列。
    singles = [r for r in crud.list_runs(db)
               if r.batch_id is None and not r.passed]
    for run in singles:
        group = route_group(run.path)
        rows.setdefault(group, {})
        rows[group][0] = rows[group].get(0, 0) + 1

    ordered = sorted(
        rows.items(),
        key=lambda item: (-sum(item[1].values()), item[0]),
    )
    return {
        "batch_ids": batch_ids,
        "rows": [
            {"group": group,
             "counts": {bid: cells.get(bid, 0) for bid in batch_ids},
             "single": cells.get(0, 0),
             "total": sum(cells.values())}
            for group, cells in ordered
        ],
    }


def _first_runs_for_batch(db, batch: ExecutionBatch) -> list[ExecutionRecord]:
    """某批的首次执行记录（9.5 第三条第 1 半句）。

    新批次按 `case_set.cases[].run_id` 关联快照里 state=executed 的首次执行，
    避免把复跑（将来 `retries`）或未执行记录混进来；legacy 没有快照，
    按 `batch_id` 直接关联 `test_runs`（方案 5.6）。
    """
    runs = crud.list_runs_for_batch(db, batch.id)
    snapshot = _snapshot(batch)
    if snapshot is None:
        return runs
    executed_ids = {
        e.get("run_id") for e in snapshot.get("cases", [])
        if e.get("state") == "executed" and e.get("run_id") is not None
    }
    return [r for r in runs if r.id in executed_ids]


def slowest_runs(db, batch_id: int | None = None, limit: int = 10) -> list[ExecutionRecord]:
    """最慢用例 Top N：耗时观察，不是性能测试（方案 9.5）。

    口径四条，逐条钉住：
    1. 只要 `duration_ms` 非空且 > 0 的首次执行；未执行 / 无耗时 / 0 一律排除，
       **不按 0 参与排序**（否则会造出「最快用例」假象）；
    2. 按耗时降序、同耗时按记录 id 升序，结果可复现；取前 limit 条；
    3. 新批次按快照 run_id 关联、legacy 与单条执行按 `batch_id` 直接关联；
    4. 全程只读——不写库、不改 `passed` / `outcome`、不动任何批次统计。

    batch_id 给定时只看该批；不指定时覆盖全部批次 + 单条执行。
    """
    candidates: list[ExecutionRecord] = []
    if batch_id is not None:
        batch = crud.get_batch(db, batch_id)
        if batch is not None:
            candidates.extend(_first_runs_for_batch(db, batch))
    else:
        for batch in crud.list_batches(db):
            candidates.extend(_first_runs_for_batch(db, batch))
        candidates.extend(r for r in crud.list_runs(db) if r.batch_id is None)
    valid = [r for r in candidates if r.duration_ms is not None and r.duration_ms > 0]
    valid.sort(key=lambda r: (-r.duration_ms, r.id))
    return valid[:limit]


def trend(db) -> dict:
    """批次趋势数据（方案 9.3）：通过率 + 执行量 + 相邻集合变化，只读。

    只纳入非 legacy 批次——legacy 没有 case_set，无法可靠还原用例集合，
    按 5.6「趋势图与集合变化标记只对非 legacy 批次计算」。集合变化直接比
    相邻两批 `case_ids` 的 ID 集合（不是比数量）：A{1,2,3,4,5} → B{1,2,3,4,6}
    数量相同但 5 删除、6 新增，必须打标记并展开四项明细。
    """
    from app.models import batch_pass_rate

    batches = [b for b in crud.list_batches(db) if not is_legacy(b)]
    batches.sort(key=lambda b: b.id)  # 时间正序，左旧右新
    rows: list[dict] = []
    previous_ids: set[int] | None = None
    for batch in batches:
        snapshot = _snapshot(batch)
        counts = snapshot.get("counts", {}) or {}
        case_ids = set(snapshot.get("case_ids", []))
        added = len(case_ids - previous_ids) if previous_ids is not None else None
        removed = len(previous_ids - case_ids) if previous_ids is not None else None
        disabled = counts.get("disabled_count", 0)
        not_run = counts.get("not_run_count", 0)
        # 任一不为 0 就打集合变化记号；第一批没有可比的上一批，不打标记。
        changed = (previous_ids is not None and
                   bool(added or removed or disabled or not_run))
        rate = batch_pass_rate(batch)
        rows.append({
            "batch_id": batch.id,
            "pass_rate": rate,
            "executed_count": counts.get("executed_count", 0),
            "not_run_count": not_run,
            "redirect_count": counts.get("redirect_count", 0),
            "set_changed": changed,
            "added": added,
            "removed": removed,
            "disabled": disabled,
            "pending": counts.get("pending_count", 0),
            "rejected": counts.get("rejected_count", 0),
        })
        previous_ids = case_ids
    return {"rows": rows, "excluded_legacy": True}
