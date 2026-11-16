"""路径参数样本：从列表接口取真实值，把模板路径渲染成可执行路径。

设计约束（方案 6.7）：

- **显式声明，不按路径前缀猜列表接口**。按前缀猜出来的 ``/contents`` 是 HTML
  列表页而不是 JSON，取不到 id 还会静默失败，比不测更糟。
- **没进表的模板路径一律不生成用例**。宁可少覆盖，也不把猜出来的样本
  写进用例集——用例一旦带着错的期望值进回归，后面每批都要为它解释。
- **取不到就不发请求**。``missing_sample`` / ``invalid_sample`` 都记 not_run，
  不拿写死的超大 id 顶替：那只能测到 404 分支，测不到 200 分支，而
  「详情页能正常打开」恰恰是要覆盖的东西。

新增一条配置只需在 ``PATH_SAMPLES`` 里加一项；不需要改执行器。
"""

# 模板路径 → 参数来源。key 必须与路由表里的 path 完全一致（含占位符名）。
PATH_SAMPLES: dict[str, dict] = {
    "/api/contents/{content_id}": {
        "params": {
            "content_id": {
                "source_path": "/api/contents",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
                "query": {"page_size": 1},
            },
        },
    },
    "/contents/{content_id}": {
        "params": {
            "content_id": {
                "source_path": "/api/contents",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
                "query": {"page_size": 1},
            },
        },
    },
    "/api/batches/{batch_id}": {
        "params": {
            "batch_id": {
                "source_path": "/api/batches",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
    "/api/batches/{batch_id}/regression": {
        "params": {
            "batch_id": {
                "source_path": "/api/batches",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
    "/batches/{batch_id}": {
        "params": {
            "batch_id": {
                "source_path": "/api/batches",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
    "/api/runs/{run_id}": {
        "params": {
            "run_id": {
                "source_path": "/api/runs",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
    "/runs/{run_id}": {
        "params": {
            "run_id": {
                "source_path": "/api/runs",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
    "/api/testcases/{case_id}": {
        "params": {
            "case_id": {
                "source_path": "/api/testcases",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
    "/testcases/{case_id}/edit": {
        "params": {
            "case_id": {
                "source_path": "/api/testcases",
                "response_path": "items",
                "id_field": "id",
                "type": "int",
                "identity": "staff",
            },
        },
    },
}

# 列表响应常见的包裹键：配置里的 response_path 优先，取不到时按这几个兜底。
_FALLBACK_KEYS = ("items", "data", "list")


def needs_sample(path: str) -> bool:
    """路径里带占位符就需要样本。"""
    return "{" in path and "}" in path


def spec_for(path: str) -> dict | None:
    """取该模板路径的样本配置；未配置返回 None。"""
    return PATH_SAMPLES.get(path)


def pick_value(payload, param_spec: dict) -> tuple[object | None, str | None, str | None]:
    """从列表响应里取第一个可用值。

    返回 ``(value, reason, detail)``：reason 为 None 表示成功，
    否则是 not_run 的原因（``missing_sample`` / ``invalid_sample``）。
    """
    rows = payload
    response_path = param_spec.get("response_path")
    if response_path:
        for part in str(response_path).split("."):
            rows = rows.get(part) if isinstance(rows, dict) else None
            if rows is None:
                break
    if not isinstance(rows, list):
        # 配置路径没命中时按常见包裹键兜底，避免一条配置写错就整类用例不跑。
        rows = None
        if isinstance(payload, dict):
            for key in _FALLBACK_KEYS:
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
    if not isinstance(rows, list):
        return None, "invalid_sample", "列表响应里找不到数组"
    if not rows:
        return None, "missing_sample", "列表接口返回空数组"

    first = rows[0]
    if not isinstance(first, dict):
        return None, "invalid_sample", "列表元素不是对象"

    field = param_spec.get("id_field", "id")
    value = first.get(field)
    if value in (None, ""):
        for candidate in param_spec.get("id_key_candidates", ()):
            if first.get(candidate) not in (None, ""):
                value = first[candidate]
                break
    if value in (None, ""):
        return None, "invalid_sample", f"列表首条没有 {field} 字段"
    return value, None, None


def render_path(template: str, values: dict) -> str:
    """把占位符替换成实际值；未提供的占位符原样留下，由调用方判非法。"""
    path = template
    for name, value in values.items():
        path = path.replace("{" + name + "}", str(value))
    return path
