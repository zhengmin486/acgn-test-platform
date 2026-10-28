"""部署后一键回归：同步用例集(可选) + 执行全部 + 回归对比 + 机器可读退出码。

替代「/testcases 页面点一遍」的手工作业，把 2026-10-22 定下的结论
「每次部署后跑一轮 generate + execute-all」变成一条命令：

1. ``--generate``：先按路由表现状同步 auto 用例集（新增 / 更新 / 下线）；
2. 以最高权限 staff 身份进程内执行全部启用用例（与 runner 同一手法）；
3. 与上一有效批次做回归对比，打印六类变化与通过率；
4. 退出码：0 = 无新增失败；1 = 有回归（regression>0）；2 = 无法执行。

口径与 /manage 总览「回归检查」卡片一致（通过率 = passed/total），
不造第二套算法。missing/added 不堵门（停用用例是人为操作，不是代码回归），
regression>0 才堵——接无人值守流水线时判它即可。

用法：
    python scripts/run_regression.py                 # 只执行 + 对比
    python scripts/run_regression.py --generate      # 先同步用例集再执行
    python scripts/run_regression.py --json          # 附加机器可读 JSON

服务器上跑注意 DATABASE_URL 相对路径铁律（deploy.md）：先 cd 到项目目录。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署后一键回归：执行全部用例并对比上一批次")
    parser.add_argument("--generate", action="store_true",
                        help="先按路由表同步 auto 用例集（新增/更新/下线）")
    parser.add_argument("--json", action="store_true", help="附加机器可读 JSON 输出")
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    from app import case_discovery, crud
    from app.auth import SESSION_COOKIE
    from app.config import settings
    from app.database import SessionLocal
    from app.security import create_session_token

    db = SessionLocal()
    try:
        account = crud.highest_staff_account(db)
        if account is None:
            print("库里没有管理员/站长账号，无法执行回归（执行器需要 staff 身份）")
            return 2

        if args.generate:
            plan = case_discovery.build_plan(db)
            if plan.get("error"):
                print(f"用例集同步失败：{plan['error']}")
                return 2
            selections = [f"{i['method']} {i['path']}"
                          for i in plan["add"] + plan["update"]]
            selections += [f"disable:{c.id}" for c in plan["offline"]]
            summary = case_discovery.apply_plan(db, selections)
            print(f"用例集同步：新增 {summary['added']} / "
                  f"更新 {summary['updated']} / 下线 {summary['disabled']}")

        from app.main import app  # DB 检查通过后再导入应用

        client = TestClient(app)
        client.cookies.set(
            SESSION_COOKIE, create_session_token(account.id, settings.auth_secret_key)
        )

        resp = client.post("/api/batches")
        if resp.status_code != 201:
            print(f"批次执行失败：HTTP {resp.status_code} {resp.text[:200]}")
            return 2
        batch = resp.json()
        rate = f"（{batch['pass_rate']}%）" if batch["pass_rate"] is not None else "（空批次）"
        print(f"批次 #{batch['id']}：{batch['passed']}/{batch['total']} 通过{rate}")
        if batch["total"] == 0:
            print("没有启用的用例；加 --generate 或去 /testcases 启用几条再跑")
            return 2

        comp = client.get(f"/api/batches/{batch['id']}/regression").json()
        if not comp.get("comparable"):
            print(f"回归对比：{comp.get('reason', '不可对比')}"
                  "（首个批次或上一批为空，无新增失败可判）")
            if args.json:
                print(json.dumps({"batch": batch, "regression": comp},
                                 ensure_ascii=False, indent=2))
            return 0

        summary = comp["summary"]
        print(f"回归对比（vs 批次 #{comp['previous_batch_id']}）："
              f"regression={summary['regression']} fixed={summary['fixed']} "
              f"unchanged_fail={summary['unchanged_fail']} "
              f"unchanged_pass={summary['unchanged_pass']} "
              f"added={summary['added']} missing={summary['missing']}")
        for item in comp["items"]:
            if item["change"] in ("regression", "fixed", "added", "missing"):
                print(f"  [{item['change']}] {item['case_name']} — {item['detail']}")

        if args.json:
            print(json.dumps({"batch": batch, "regression": comp},
                             ensure_ascii=False, indent=2))

        if summary["regression"] > 0:
            print("结论：有新增失败（regression>0），上线门槛未过")
            return 1
        print("结论：无新增失败")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
