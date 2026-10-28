"""首页海报轮播：全站名单的候选、更新与配置（数据层，无路由）。

与页面内的手动浏览（纯前端切换）严格分层——本模块只负责**服务端的一份
全站名单**：从当季新番里挑候选、按稳定序轮转生成、校验后整批生效；
失败时保留旧名单，绝不把首页换成空白。

候选口径（复用既有档期数据，不重复开发）：
- 来源是 anime_schedules 的**当前季度**（list_schedule_seasons 取最新一季，
  与首页「本日新番」同源），category 为空的季度档期；
- 只要动画：档期表本身就是新番档期（yuc.wiki 动画分区），且关联到
  contents 时要求 type == anime，不混入漫画 / 游戏 / 轻小说；
- 有海报（contents.image_url 本地封面优先，其次档期外链海报）才进候选；
- 跳转目标：有关联作品走 /contents/{id}，否则按标题搜索（与档期卡片一致）。

更新模式（HomePosterConfig.mode）：
- off（默认）：定时任务不跑；保留当前名单与排序，首页照常展示；
- auto：daily / weekly 从候选轮转生成新名单整批替换；
- manual：管理员手动指定与排序，定时任务一律不碰。

并发与重复执行：
- flock 文件锁（同机 cron 多次触发 / 手动「立即执行」与 cron 撞车）；
- 60 秒时间窗去重（cron 补跑、连点），窗口内非 force 直接跳过；
- 先生成并校验新名单，再在单个事务里替换 + 推进游标 + 落状态。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

try:  # 文件锁只在 Linux（线上）有意义；Windows 本地开发没有 fcntl，退化为不锁。
    import fcntl
except ImportError:  # pragma: no cover - 仅 Windows 开发机走此分支
    fcntl = None

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnimeSchedule, Content, HomePoster, HomePosterConfig

logger = logging.getLogger(__name__)

# 首页海报轮播同时展示的作品数（一屏 5 张可见：C 位 + 左右各 2）。
COVERFLOW_SIZE = 12

# 自动更新的固定执行时间（服务器本地时间，与「本日新番」的 datetime.now() 同口径）：
# 每天 / 每周一 的 RUN_HOUR 点之后，cron 本轮才会真正执行；不足点则顺延到下轮。
RUN_HOUR = 6

# 重复执行保护窗口：距上次成功不足这么久且非 force，直接跳过。
RERUN_WINDOW = timedelta(seconds=60)

MODE_OFF = "off"
MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_LABELS = {
    MODE_OFF: "关闭自动更新",
    MODE_AUTO: "自动更新",
    MODE_MANUAL: "手动指定",
}
FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_LABELS = {FREQ_DAILY: "每日", FREQ_WEEKLY: "每周"}

# 项目根目录（app/ 的上一级）：锁文件放 .ops/locks/，与服务器 run-cron.sh 同址。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LOCK_PATH = _PROJECT_ROOT / ".ops" / "locks" / "home_posters.lock"


# ---------- 配置（单行） ----------


def ensure_config(db: Session) -> HomePosterConfig:
    """取出配置行，不存在则建（幂等；init_db 也会调用）。"""
    config = db.get(HomePosterConfig, 1)
    if config is None:
        config = HomePosterConfig(id=1, mode=MODE_OFF, frequency=FREQ_DAILY)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def get_config(db: Session) -> HomePosterConfig:
    return db.get(HomePosterConfig, 1) or ensure_config(db)


def set_mode(db: Session, *, mode: str, frequency: str = "") -> HomePosterConfig:
    """切换更新模式 / 频率（管理页用）。frequency 为空表示不变。"""
    if mode not in MODE_LABELS:
        raise ValueError(f"未知更新模式：{mode}")
    config = ensure_config(db)
    config.mode = mode
    if frequency in FREQ_LABELS:
        config.frequency = frequency
    config.updated_at = datetime.now()
    db.commit()
    db.refresh(config)
    return config


# ---------- 候选（当季新番） ----------


def list_candidates(db: Session, *, limit: int = 400) -> list[dict]:
    """当前季度的新番候选（稳定序），供自动轮转与管理页手动挑选用。

    稳定序：站内评分降序（无评分排最后）→ 星期 → 主标题。
    季度切换后本函数自然跟随（每次现算最新 season_code），无需额外迁移。
    """
    seasons = _latest_season_code(db)
    if not seasons:
        return []
    rows = db.scalars(
        select(AnimeSchedule).where(
            AnimeSchedule.category.is_(None),
            AnimeSchedule.season_code == seasons,
        )
    ).all()
    # 关联作品一次性取出，避免逐行 get（N+1）。
    content_ids = {row.content_id for row in rows if row.content_id}
    contents = {}
    if content_ids:
        for item in db.scalars(select(Content).where(Content.id.in_(content_ids))).all():
            contents[item.id] = item

    candidates: list[dict] = []
    for row in rows:
        item = contents.get(row.content_id) if row.content_id else None
        if item is not None:
            # 有关联作品：只收动画（不混漫画 / 游戏 / 轻小说），且必须有本地封面。
            if item.type != "anime" or not item.image_url:
                continue
            candidates.append(
                {
                    "content_id": item.id,
                    "title": item.title,
                    "image_url": item.image_url,
                    "rating": item.rating or 0.0,
                    "type": item.type,
                    "weekday": row.weekday,
                    "href": f"/contents/{item.id}",
                }
            )
        elif row.image_url:
            # 未关联作品库：用档期外链海报，跳转按标题搜索（与档期卡片一致）。
            candidates.append(
                {
                    "content_id": None,
                    "title": row.title_main,
                    "image_url": row.image_url,
                    "rating": 0.0,
                    "type": "anime",
                    "weekday": row.weekday,
                    "href": f"/contents?q={row.title_main}",
                }
            )
    candidates.sort(
        key=lambda c: (-c["rating"], c["weekday"] is None, c["weekday"] or 0, c["title"])
    )
    return candidates[:limit]


def _latest_season_code(db: Session) -> str | None:
    """最新季度档期码（category 为空的季度档期里的最大 season_code）。"""
    return db.scalar(
        select(AnimeSchedule.season_code)
        .where(AnimeSchedule.category.is_(None))
        .order_by(AnimeSchedule.season_code.desc())
        .limit(1)
    )


# ---------- 名单生成与生效 ----------


def build_auto_list(db: Session, config: HomePosterConfig) -> list[dict]:
    """按轮转游标生成新名单（候选稳定序 + 游标循环取 COVERFLOW_SIZE 条）。

    候选不足 COVERFLOW_SIZE 时全取；候选为 0 时返回空（调用方据此保留旧名单）。
    """
    candidates = list_candidates(db)
    if not candidates:
        return []
    cursor = config.rotation_cursor % len(candidates)
    picked = [candidates[(cursor + i) % len(candidates)] for i in range(COVERFLOW_SIZE)]
    return picked[: len(candidates)]


def apply_auto_update(db: Session, *, force: bool = False, now: datetime | None = None) -> int:
    """生成 + 校验 + 整批生效，返回写入条数；失败抛异常（调用方保留旧名单）。

    单个事务内完成：删旧名单 → 写新名单 → 推进游标 → 落状态，中途出错整体
    回滚，首页继续用旧名单。
    """
    now = now or datetime.now()
    config = get_config(db)
    candidates = list_candidates(db)
    if not candidates:
        raise RuntimeError("当季候选不足：没有可用的新番海报候选")
    new_list = build_auto_list(db, config)
    if not _validate(new_list):
        raise RuntimeError("新名单未通过校验（缺标题 / 海报 / 跳转目标）")

    # 步长：候选多时整窗平移（COVERFLOW_SIZE），候选少时逐格挪——
    # 保证「不能仅反复生成完全相同的名单」，又不会无规律地大幅洗牌。
    step = min(COVERFLOW_SIZE, max(len(candidates) - COVERFLOW_SIZE, 1))
    db.execute(sa_delete(HomePoster))
    for index, item in enumerate(new_list):
        content_id = item.get("content_id")
        if not content_id:
            # 未关联作品库的候选没有 content_id，无法写名单（名单按 content_id 关联）；
            # 轮转生成时全部跳过这类，保证名单里每条都能直跳详情页。
            continue
        db.add(HomePoster(content_id=int(content_id), sort_order=index, source="auto"))
    config.rotation_cursor = (config.rotation_cursor + step) % len(candidates)
    config.last_run_at = now
    config.last_status = "ok"
    config.last_error = None
    config.last_count = len(new_list)
    config.updated_at = now
    db.commit()
    return len(new_list)


def set_manual_list(db: Session, content_ids: list[int]) -> int:
    """管理员手动指定名单（按给定顺序），同步把模式切到 manual。

    手动保存后定时任务一律不再替换（mode=manual），只有管理员主动改才变。
    """
    cleaned: list[int] = []
    for raw in content_ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in cleaned:
            cleaned.append(value)
    # 只保留库里真实存在、有封面、可跳转的作品（防止伪造 id / 坏链上海报墙）。
    ordered: list[int] = []
    if cleaned:
        valid = {
            item.id
            for item in db.scalars(
                select(Content).where(Content.id.in_(cleaned), Content.image_url.is_not(None))
            ).all()
        }
        ordered = [cid for cid in cleaned if cid in valid]

    config = ensure_config(db)
    db.execute(sa_delete(HomePoster))
    for index, content_id in enumerate(ordered):
        db.add(HomePoster(content_id=content_id, sort_order=index, source="manual"))
    config.mode = MODE_MANUAL
    config.last_status = "manual"
    config.last_error = None
    config.last_count = len(ordered)
    config.updated_at = datetime.now()
    db.commit()
    return len(ordered)


def _validate(items: list[dict]) -> bool:
    """生效前校验：每条都要有标题 / 海报 / 跳转目标。"""
    if not items:
        return False
    for item in items:
        if not item.get("title") or not item.get("image_url") or not item.get("href"):
            return False
    return True


# ---------- 首页渲染数据 ----------


def list_active_posters(db: Session) -> list[dict]:
    """当前生效名单（按 sort_order），取不到作品 / 封面的行跳过。

    名单为空时返回空列表——首页回落「当季实时候选」（不落库、不改变全站
    名单），由 business_pages 负责拼接，保证首页永远不会空白。
    """
    rows = db.scalars(select(HomePoster).order_by(HomePoster.sort_order, HomePoster.id)).all()
    if not rows:
        return []
    contents = {}
    ids = {row.content_id for row in rows}
    for item in db.scalars(select(Content).where(Content.id.in_(ids))).all():
        contents[item.id] = item
    posters: list[dict] = []
    for row in rows:
        item = contents.get(row.content_id)
        if item is None or not item.image_url:
            continue
        posters.append(
            {
                "content_id": item.id,
                "title": item.title,
                "image_url": item.image_url,
                "rating": item.rating or 0.0,
                "type": item.type,
                "href": f"/contents/{item.id}",
                "source": row.source,
            }
        )
    return posters


# ---------- 定时频率判断 ----------


def should_run(config: HomePosterConfig, *, now: datetime | None = None) -> bool:
    """按模式与频率判断本轮 cron 是否该执行（固定执行时间 RUN_HOUR）。"""
    if config.mode != MODE_AUTO:
        return False
    now = now or datetime.now()
    if now.hour < RUN_HOUR:
        return False
    last = config.last_run_at
    if last is None:
        return True
    if config.frequency == FREQ_WEEKLY:
        # 每周固定一次：周一 6 点后，且距上次成功 ≥7 天（双条件防补跑连击）。
        return now.weekday() == 0 and (now - last) >= timedelta(days=7)
    # 每日：今天日期比上次成功日期新（跨天即跑，同一天重复触发不跑）。
    return now.date() > last.date()


def run_scheduled(db: Session, *, force: bool = False) -> tuple[bool, str]:
    """cron 入口：该跑才跑；带文件锁 + 时间窗去重，绝不并发覆盖有效名单。"""
    config = get_config(db)
    if not force and not should_run(config):
        return False, f"未到执行时间（模式 {MODE_LABELS.get(config.mode, config.mode)}）"
    if not force and config.last_run_at:
        if datetime.now() - config.last_run_at < RERUN_WINDOW:
            return False, "距上次成功不足 60 秒，跳过（防重复执行）"
    with _update_lock() as acquired:
        if not acquired:
            return False, "另一进程正在更新，本次跳过（不排队、不覆盖）"
        # 锁内再取一次配置：等锁期间可能已被另一进程更新。
        config = get_config(db)
        if not force and not should_run(config):
            return False, "等待锁期间已由其它进程执行，跳过"
        try:
            count = apply_auto_update(db, force=force)
        except Exception as exc:  # noqa: BLE001 - 失败只记状态，保留旧名单
            db.rollback()
            config = get_config(db)
            config.last_status = "failed"
            config.last_error = str(exc)[:500]
            config.updated_at = datetime.now()
            db.commit()
            logger.warning("首页海报自动更新失败（保留旧名单）：%s", exc)
            return False, f"更新失败：{exc}"
        return True, f"已更新 {count} 张海报"


@contextmanager
def _update_lock(path: Path | None = None):
    """跨进程文件锁：cron 多次触发 / 手动执行撞车时只有一方真正更新。

    yield True = 拿到锁（继续更新）；False = 另一进程正持有（调用方应跳过）。
    """
    lock_path = path or _LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        if fcntl is None:
            acquired = True  # Windows 开发机：无锁直通（线上 Linux 才需要互斥）。
        else:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                # 非阻塞拿锁失败 = 另一进程正在更新，本次直接跳过（不排队、不覆盖）。
                acquired = False
        yield acquired
    finally:
        if acquired and fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
