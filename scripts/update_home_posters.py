"""首页海报全站名单的定时更新（cron 入口，独立于用户访问）。

由服务器 crontab（.ops/run-cron.sh cover）定期调用：
- 按 HomePosterConfig 的模式 / 频率判断本轮是否执行（默认「关闭自动更新」= 不跑）；
- 执行即从当季新番候选轮转生成新名单，校验后整批生效；失败保留旧名单；
- 带文件锁 + 60 秒时间窗去重，cron 补跑 / 与管理员手动执行撞车都不会并发覆盖。

用法：
    python scripts/update_home_posters.py            # 按配置判断，该跑才跑
    python scripts/update_home_posters.py --dry-run  # 只打印决策与候选情况，不写库
    python scripts/update_home_posters.py --force    # 忽略频率窗口，立即执行一次
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import home_posters  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="首页海报全站名单定时更新")
    parser.add_argument("--force", action="store_true", help="忽略频率窗口，立即执行一次")
    parser.add_argument("--dry-run", action="store_true", help="只打印决策，不写库")
    parser.add_argument(
        "--database-url", default=settings.database_url, help="数据库连接串"
    )
    args = parser.parse_args(argv)

    engine = build_engine(args.database_url)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        config = home_posters.get_config(db)
        mode_label = home_posters.MODE_LABELS.get(config.mode, config.mode)
        if args.dry_run:
            candidates = home_posters.list_candidates(db)
            active = home_posters.list_active_posters(db)
            print(
                f"[预览] 模式 {mode_label}"
                f"（频率 {home_posters.FREQ_LABELS.get(config.frequency, config.frequency)}），"
                f"当前名单 {len(active)} 张，当季候选 {len(candidates)} 部，"
                f"最近成功 {config.last_run_at or '从未'}"
            )
            return 0
        ok, message = home_posters.run_scheduled(db, force=args.force)
        print(f"[海报] {message}")
        return 0 if ok or "跳过" in message or "未到" in message else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
