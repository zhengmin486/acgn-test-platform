"""扫描 covers/ 目录，把已落盘的封面文件回填到 contents.image_url。

为什么需要（2026-10-18）：服务器没有境外出网，Bangumi 封面只能在**本机**下载，
再经「自主传文件通道」打包上传到服务器的 cover_storage_dir。图片文件到位后，
数据库里的 image_url 还是 NULL——本脚本负责把两者接起来：

    扫描 cover_{bangumi_id}.* 文件 → 按文件名里的 ID 匹配 contents 行 →
    写 image_url = /covers/<文件名> → 顺手清掉 remote_image_url（已不需要按需下载）。

幂等：image_url 已正确指向同文件的行直接跳过；重复跑安全。
只回填、不下载、不生成派生图（派生图由 /media 端点按需生成并缓存，
见 app/media.py ensure_variant）。

用法（服务器上）：
    .venv/bin/python scripts/backfill_covers_from_dir.py             # 预览
    .venv/bin/python scripts/backfill_covers_from_dir.py --apply     # 写库
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app import crud  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal, build_engine  # noqa: E402
from app.models import Content  # noqa: E402

# cover_{bangumi_id}.{jpg|jpeg|png|webp|gif}
FILENAME_RE = re.compile(r"^cover_(\d+)\.(jpg|jpeg|png|webp|gif)$")


def scan_cover_dir(root: Path) -> dict[int, str]:
    """扫描目录，返回 {bangumi_id: 文件名}；同名不同扩展取第一个（按字母序稳定）。"""
    mapping: dict[int, str] = {}
    if not root.is_dir():
        return mapping
    for path in sorted(root.iterdir()):
        match = FILENAME_RE.match(path.name)
        if match:
            bid = int(match.group(1))
            mapping.setdefault(bid, path.name)
    return mapping


def backfill(*, apply: bool, database_url: str | None = None) -> tuple[int, int, int]:
    """执行回填，返回 (已回填数, 已是最新跳过数, 没有对应条目的孤儿文件数)。"""
    url = database_url or settings.database_url
    engine = build_engine(url)
    crud.init_db(engine)  # 幂等建表 + 补列（remote_image_url 等），老库也能跑
    session = SessionLocal(bind=engine)
    try:
        covers = scan_cover_dir(Path(settings.cover_storage_dir))
        updated = skipped = orphan = 0
        for bangumi_id, filename in sorted(covers.items()):
            content = session.scalar(
                select(Content).where(Content.bangumi_id == bangumi_id)
            )
            if content is None:
                orphan += 1
                continue
            site_url = f"/covers/{filename}"
            if content.image_url == site_url:
                skipped += 1
                continue
            if apply:
                content.image_url = site_url
                content.remote_image_url = None
                session.commit()
            updated += 1
        if apply:
            session.commit()
        return updated, skipped, orphan
    finally:
        session.close()
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描 covers/ 目录，把已落盘封面回填到 contents.image_url",
    )
    parser.add_argument("--apply", action="store_true", help="写库；缺省只预览")
    parser.add_argument("--database-url", default=None, help="覆盖数据库连接串")
    args = parser.parse_args(argv)

    updated, skipped, orphan = backfill(apply=args.apply, database_url=args.database_url)
    mode = "写库" if args.apply else "预览（加 --apply 正式回填）"
    print(f"模式：{mode}")
    print(f"回填 {updated} 条；已最新跳过 {skipped} 条；孤儿文件（无对应条目）{orphan} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
