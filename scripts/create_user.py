"""账号管理脚本：创建 / 重置密码 / 查看平台账号（user 与 admin）。

用法示例：
    # 创建管理员
    python scripts/create_user.py --username alice --role admin
    # 创建普通用户（密码从命令行读取，不落历史）
    python scripts/create_user.py --username bob --role user --password '你的密码'
    # 创建时绑定邮箱（管理员创建的账号一律视为已验证，无需再走验证邮件）
    python scripts/create_user.py --username carol --email carol@example.com
    # 重置已有账号密码
    python scripts/create_user.py --username alice --reset-password --password '新密码'
    # 仅列出账号（不显示密码哈希）
    python scripts/create_user.py --list

设计要点：
- 只操作 users 表，不触碰内容、测试用例、执行记录等任何业务数据；
- 密码只保存 PBKDF2-HMAC-SHA256 哈希（app/security.py），明文不落库、不打印；
- 未提供 --password 时使用 getpass 交互输入，避免密码进入 shell 历史。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 直接运行本脚本时，scripts/ 不是包；把项目根目录加入 sys.path 才能导入 app 包。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import build_engine  # noqa: E402
from app.models import User  # noqa: E402


def _read_password(args) -> str:
    """优先取命令行参数，否则交互输入两次确认（不显示、不留历史）。"""
    if args.password:
        return args.password
    first = getpass.getpass("请输入密码：")
    second = getpass.getpass("请再次输入密码：")
    if first != second:
        raise SystemExit("两次输入的密码不一致，已取消。")
    if not first:
        raise SystemExit("密码不能为空，已取消。")
    return first


def _list_users(db) -> None:
    users = list(db.scalars(select(User).order_by(User.id)))
    if not users:
        print("当前没有任何账号。")
        return
    print(f"共 {len(users)} 个账号：")
    for user in users:
        mail = user.email or "（未绑定邮箱）"
        state = "已验证" if user.email_verified else "待验证"
        print(f"  #{user.id}  {user.username}  角色={user.role}  邮箱={mail}"
              f"  {state}  创建于={user.created_at:%Y-%m-%d %H:%M:%S}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建或维护平台账号（user / admin）。")
    parser.add_argument("--username", help="账号用户名")
    parser.add_argument("--email", help="可选：绑定邮箱；管理员创建的账号一律视为已验证")
    parser.add_argument("--role", choices=list(crud.VALID_ROLES), default=crud.ROLE_USER,
                        help="账号角色：user（默认）或 admin")
    parser.add_argument("--password", help="密码；不提供则交互输入（推荐）")
    parser.add_argument("--reset-password", action="store_true",
                        help="账号已存在时重置其密码（并可选更新角色）")
    parser.add_argument("--list", action="store_true", help="列出全部账号后退出")
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
            _list_users(db)
            return 0

        if not args.username:
            parser.error("需要 --username，或使用 --list 查看账号")

        existing = crud.get_user_by_username(db, args.username.strip())
        if existing is not None:
            if not args.reset_password:
                print(f"账号已存在：{existing.username}（角色={existing.role}）。"
                      f"如需重置密码请加 --reset-password。")
                return 1
            crud.set_password(db, existing, _read_password(args))
            if existing.role != args.role:
                existing.role = args.role
                db.commit()
            print(f"已重置账号 {existing.username} 的密码（角色={existing.role}）。")
            return 0

        user = crud.create_user(
            db,
            username=args.username,
            password=_read_password(args),
            role=args.role,
            email=args.email,
            # 管理员亲自创建的账号无需再走验证邮件。
            require_verification=False,
        )
        extra = f"  邮箱={user.email}" if user.email else ""
        print(f"已创建账号 {user.username}（角色={user.role}{extra}）。")
        return 0
    except ValueError as exc:
        print(f"创建失败：{exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
