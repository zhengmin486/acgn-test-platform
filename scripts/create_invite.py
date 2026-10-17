"""注册邀请码管理脚本：批量生成 / 查看邀请码（公开注册关闭后的准入控制）。

用法示例：
    # 生成 5 个 7 天有效的邀请码
    python scripts/create_invite.py --count 5 --days 7 --note "给朋友"
    # 生成 1 个永不过期的邀请码
    python scripts/create_invite.py --days 0 --note "长期备用"
    # 列出全部邀请码（含状态与使用痕迹）
    python scripts/create_invite.py --list

设计要点：
- 只操作 invite_codes 表，不触碰用户、内容、测试用例等任何业务数据；
- 邀请码明文存储：本脚本生成的码直接打印到终端，请通过安全渠道转交；
- 与管理页 /admin/invites 共用同一数据层（crud.create_invite_codes），
  行为完全一致，只是没有浏览器的入口。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402


def _list_codes(db) -> None:
    """列出全部邀请码（含状态、有效期与使用痕迹）。"""
    codes = crud.list_invite_codes(db)
    if not codes:
        print("当前没有任何邀请码。")
        return
    print(f"共 {len(codes)} 个邀请码：")
    for row in codes:
        status = crud.INVITE_STATUS_LABELS[crud.invite_code_status(row)]
        expires = row.expires_at.strftime("%Y-%m-%d %H:%M") if row.expires_at else "永不过期"
        used = (
            f"使用者=#{row.used_by}（{row.used_at:%Y-%m-%d %H:%M}）"
            if row.used_at
            else "未使用"
        )
        note = f"  备注={row.note}" if row.note else ""
        print(f"  {row.code}  [{status}]  有效期至={expires}  {used}{note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成或查看注册邀请码（REGISTRATION_MODE=invite 时注册必须提供）。"
    )
    parser.add_argument("--count", type=int, default=1,
                        help=f"生成数量（1-{crud.INVITE_CODE_MAX_BATCH}，默认 1）")
    parser.add_argument("--days", type=int, default=0,
                        help="有效期天数，0 表示永不过期（默认 0）")
    parser.add_argument("--note", default="", help="备注：发给谁 / 用途，便于日后核对")
    parser.add_argument("--list", action="store_true", help="列出全部邀请码后退出")
    parser.add_argument("--database-url", default=settings.database_url,
                        help="数据库连接串（默认取配置 / .env）")
    args = parser.parse_args(argv)

    engine = build_engine(args.database_url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = session_factory()
    try:
        # 建表（如无），保证首次运行即可用；不会改动已有表与数据。
        crud.init_db(engine)
        if args.list:
            _list_codes(db)
            return 0

        expires_at = (
            datetime.now() + timedelta(days=args.days) if args.days > 0 else None
        )
        try:
            codes = crud.create_invite_codes(
                db, count=args.count, expires_at=expires_at, note=args.note
            )
        except (ValueError, RuntimeError) as exc:
            print(f"生成失败：{exc}")
            return 1

        print(f"注册模式：{settings.registration_mode}"
              f"（{'需要邀请码' if settings.registration_invite_only else '公开注册，邀请码暂不参与校验'}）")
        print(f"已生成 {len(codes)} 个邀请码：")
        for row in codes:
            expires = row.expires_at.strftime("%Y-%m-%d %H:%M") if row.expires_at else "永不过期"
            print(f"  {row.code}  有效期至={expires}")
        print("请通过安全渠道转交；每个邀请码一次性有效。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
