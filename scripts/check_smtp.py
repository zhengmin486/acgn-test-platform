"""SMTP 连通性自检：在**部署服务器**上验证 Gmail SMTP 能否连上并登录。

为什么要有这个脚本：本地能连不代表服务器能连（云厂商常封 25/465/587 出网口，
Gmail 对中国大陆 IP 的可用性也不稳定）。只有在这台机器上跑通，才能说
「注册验证码邮件真的能发出去」。

用法（先配好 .env）：
    python scripts/check_smtp.py              # 连接 + 登录 + NOOP
    python scripts/check_smtp.py --send-to you@example.com   # 额外发一封测试邮件

退出码：0 = 可用；1 = 不可用（连接失败 / 认证失败 / 发送失败）。
诊断信息打印到 stdout，详细异常写日志；**不打印任何密码**。
"""

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import mailer  # noqa: E402
from app.config import settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 Gmail SMTP 是否可用")
    parser.add_argument(
        "--send-to",
        default="",
        help="可选：给该地址发一封测试验证码邮件（用于验证投递而不只是登录）",
    )
    args = parser.parse_args(argv)

    print(f"SMTP_HOST={settings.smtp_host or '(未配置)'}")
    print(f"SMTP_PORT={settings.smtp_port}")
    print(f"SMTP_USERNAME={settings.smtp_username or '(未配置)'}")
    print(f"SMTP_FROM_ADDRESS={settings.smtp_from_address or '(未配置)'}")
    print(f"连接模式={'SSL 直连' if mailer._use_ssl() else 'STARTTLS'}")

    ok, detail = mailer.probe_smtp()
    print(f"连通性：{'OK' if ok else 'FAILED'} - {detail}")
    if not ok:
        return 1

    if args.send_to:
        code = mailer.send_registration_code(args.send_to, "000000")
        sent, message = code
        print(f"测试邮件：{'已发送' if sent else '发送失败'} - {message}")
        if not sent:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
