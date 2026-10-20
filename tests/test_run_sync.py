"""定时同步 CLI 测试：scripts/run_sync.py 的参数编排与运行记录。

覆盖：
- 默认数据源：新番档期（Atom 最新一期）+ 多源每日资讯，trigger=scheduled；
- --task / --season / --scope / --limit / --preview 的参数分发；
- 用法错误（未知数据源、bangumi 缺 ID）退出码 2；
- ScopeError / TaskBusyError / 意外异常：该数据源跳过、退出码 1；
- 部分成功（有失败项）同样退出码 1；
- 运行记录落库：sync_task_runs.trigger=scheduled、mode 与状态正确。

网络与同步实现全部打桩（patch app.sync_tasks 的入口与 resolve_latest_season），
不访问外网；数据库用独立临时 SQLite（tmp_path），与 local_dev.db 隔离。
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import crud, sync_tasks
from app.database import build_engine
from app.models import SyncTaskRun
from scripts.import_articles import ArticleImportSummary
from scripts.news_daily import DailyNewsSummary
from scripts.sync_yuc_schedule import SyncSummary

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_sync.py"

# scripts/ 不是包，按文件路径加载被测 CLI 模块（与直接运行脚本等价）。
_spec = importlib.util.spec_from_file_location("run_sync_for_tests", SCRIPT_PATH)
run_sync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_sync
_spec.loader.exec_module(run_sync)


@pytest.fixture
def factory(tmp_path):
    """独立临时库 + 会话工厂（建全部表）。"""
    engine = build_engine(f"sqlite:///{tmp_path / 'run_sync.db'}")
    crud.init_db(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


def make_result(task_key: str, *, failed: int = 0, added: int = 1) -> sync_tasks.TaskResult:
    """构造一个成功的 TaskResult（可按需改成带失败项）。"""
    spec = sync_tasks.get_task(task_key)
    return sync_tasks.TaskResult(
        task_key=spec.key,
        task_label=spec.label,
        source_label=spec.source,
        scope="",
        mode=sync_tasks.MODE_APPLY,
        added=added,
        failed=failed,
    )


class FakeRunTask:
    """记录 run_task 调用的假实现，按脚本返回结果或抛异常。"""

    def __init__(self, script=None):
        self.calls: list[dict] = []
        self._script = dict(script or {})

    def __call__(self, task_key, scope_text, **kwargs):
        self.calls.append({"task_key": task_key, "scope": scope_text, **kwargs})
        step = self._script.get(task_key, "ok")
        if isinstance(step, Exception):
            raise step
        if step == "ok":
            return make_result(task_key)
        return step  # 直接返回给定结果对象


@pytest.fixture
def fake_run_task(monkeypatch):
    fake = FakeRunTask()
    monkeypatch.setattr(sync_tasks, "run_task", fake)
    return fake


# ---------- 参数编排 ----------


@pytest.fixture
def fake_latest_season(monkeypatch):
    """档期「取最新」不触网：固定返回 202610。"""
    monkeypatch.setattr(run_sync, "resolve_latest_season", lambda **kwargs: "202610")
    return "202610"


class TestArgumentDispatch:
    def test_default_runs_yuc_and_news(self, factory, fake_run_task, fake_latest_season):
        code = run_sync.main([], session_factory=factory)
        assert code == 0
        assert [call["task_key"] for call in fake_run_task.calls] == [
            sync_tasks.TASK_YUC,
            sync_tasks.TASK_NEWS,
        ]
        yuc_call = fake_run_task.calls[0]
        assert yuc_call["scope"] == "202610"  # Atom 最新一期（打桩固定值）
        assert yuc_call["apply"] is True
        assert yuc_call["trigger"] == sync_tasks.TRIGGER_SCHEDULED
        assert fake_run_task.calls[1]["scope"] == ""  # 配额由配置决定，无范围

    def test_task_bangumi_requires_scope(self, factory, fake_run_task):
        code = run_sync.main(["--task", "bangumi"], session_factory=factory)
        assert code == run_sync.EXIT_USAGE
        assert fake_run_task.calls == []

    def test_task_bangumi_with_scope(self, factory, fake_run_task):
        code = run_sync.main(
            ["--task", "bangumi", "--scope", "265 975"], session_factory=factory
        )
        assert code == 0
        assert fake_run_task.calls == [
            {
                "task_key": "bangumi",
                "scope": "265 975",
                "apply": True,
                "scope_limit": None,
                "trigger": "scheduled",
                "session_factory": factory,
            }
        ]

    def test_explicit_season(self, factory, fake_run_task):
        code = run_sync.main(
            ["--task", "yuc_schedule", "--season", "202610"], session_factory=factory
        )
        assert code == 0
        assert fake_run_task.calls[0]["scope"] == "202610"

    def test_articles_scope_and_limit(self, factory, fake_run_task):
        code = run_sync.main(
            ["--task", "articles", "--scope", "TVアニメ", "--limit", "5"],
            session_factory=factory,
        )
        assert code == 0
        call = fake_run_task.calls[0]
        assert call["scope"] == "TVアニメ"
        assert call["scope_limit"] == "5"

    def test_preview_passes_apply_false(self, factory, fake_run_task):
        code = run_sync.main(["--preview"], session_factory=factory)
        assert code == 0
        assert all(call["apply"] is False for call in fake_run_task.calls)

    def test_unknown_task_is_usage_error(self, factory, fake_run_task):
        code = run_sync.main(["--task", "nope"], session_factory=factory)
        assert code == run_sync.EXIT_USAGE
        assert fake_run_task.calls == []

    def test_all_keyword_equals_default(self, factory, fake_run_task, fake_latest_season):
        code = run_sync.main(["--task", "all"], session_factory=factory)
        assert code == 0
        assert [call["task_key"] for call in fake_run_task.calls] == [
            sync_tasks.TASK_YUC,
            sync_tasks.TASK_NEWS,
        ]


# ---------- 失败与退出码 ----------


class TestFailureHandling:
    def test_scope_error_skips_and_fails(self, factory, fake_run_task, fake_latest_season, capsys):
        fake_run_task._script[sync_tasks.TASK_YUC] = sync_tasks.ScopeError("档期不存在")
        code = run_sync.main([], session_factory=factory)
        assert code == run_sync.EXIT_TASK_FAILED
        assert "未执行" in capsys.readouterr().out
        # 失败不中断：资讯仍然执行了
        assert len(fake_run_task.calls) == 2

    def test_busy_error_fails(self, factory, fake_run_task, fake_latest_season, capsys):
        fake_run_task._script[sync_tasks.TASK_NEWS] = sync_tasks.TaskBusyError("正在执行")
        code = run_sync.main([], session_factory=factory)
        assert code == run_sync.EXIT_TASK_FAILED
        assert "正在执行" in capsys.readouterr().out

    def test_unexpected_exception_fails(self, factory, fake_run_task, fake_latest_season, capsys):
        fake_run_task._script[sync_tasks.TASK_YUC] = RuntimeError("网络炸了")
        code = run_sync.main([], session_factory=factory)
        assert code == run_sync.EXIT_TASK_FAILED
        assert "网络炸了" in capsys.readouterr().out

    def test_partial_result_counts_as_failure(self, factory, fake_run_task, fake_latest_season):
        fake_run_task._script[sync_tasks.TASK_YUC] = make_result(
            sync_tasks.TASK_YUC, failed=2
        )
        code = run_sync.main([], session_factory=factory)
        assert code == run_sync.EXIT_TASK_FAILED

    def test_all_success_exit_zero(self, factory, fake_run_task, fake_latest_season):
        code = run_sync.main([], session_factory=factory)
        assert code == run_sync.EXIT_OK


# ---------- 运行记录 ----------


class TestRunRecords:
    def test_records_land_with_scheduled_trigger(self, factory, monkeypatch, fake_latest_season):
        """打桩叶子同步函数，让 run_task 真跑：记录落库且 trigger=scheduled。"""
        yuc_calls: list[str] = []
        news_calls: list[dict] = []

        def fake_run_sync(season_code, *, session_factory, apply=False, **kwargs):
            yuc_calls.append(season_code)
            return SyncSummary(
                mode="apply" if apply else "dry-run",
                season_code=season_code,
                season_title="2026 年 10 月",
                season_url="https://yuc.wiki/202610",
                added=1,
                unchanged=2,
            )

        def fake_run_daily(session_factory, *, apply=False, **kwargs):
            news_calls.append({"apply": apply})
            return DailyNewsSummary(
                mode="apply" if apply else "preview", quota=20, added=3, existing=2
            )

        monkeypatch.setattr(sync_tasks, "run_sync", fake_run_sync)
        monkeypatch.setattr(sync_tasks, "run_import_daily", fake_run_daily)

        code = run_sync.main([], session_factory=factory)
        assert code == 0
        assert yuc_calls == ["202610"]
        assert news_calls == [{"apply": True}]

        db = factory()
        try:
            runs = list(db.scalars(select(SyncTaskRun).order_by(SyncTaskRun.id)))
            assert len(runs) == 2
            assert [run.task_key for run in runs] == [
                sync_tasks.TASK_YUC,
                sync_tasks.TASK_NEWS,
            ]
            for run in runs:
                assert run.trigger == "scheduled"
                assert run.mode == "apply"
                assert run.status == "ok"
                assert run.finished_at is not None
        finally:
            db.close()

    def test_preview_records_mode_preview(self, factory, monkeypatch, fake_latest_season):
        def fake_run_sync(season_code, *, session_factory, apply=False, **kwargs):
            return SyncSummary(
                mode="dry-run", season_code=season_code, season_title="t",
                season_url="u", preview=3,
            )

        monkeypatch.setattr(sync_tasks, "run_sync", fake_run_sync)
        monkeypatch.setattr(
            sync_tasks,
            "run_import_daily",
            lambda factory_, **kwargs: DailyNewsSummary(
                mode="preview", quota=20, added=2, requested=5
            ),
        )

        code = run_sync.main(["--preview"], session_factory=factory)
        assert code == 0
        db = factory()
        try:
            runs = list(db.scalars(select(SyncTaskRun)))
            assert len(runs) == 2
            assert {run.mode for run in runs} == {"preview"}
            assert all(run.status == "ok" for run in runs)
        finally:
            db.close()


# ---------- 纯函数 ----------


class TestHelpers:
    def test_current_season_code(self):
        assert run_sync.current_season_code(datetime(2026, 3, 5, 23, 30)) == "202603"
        assert run_sync.current_season_code(datetime(2026, 12, 31, 0, 0)) == "202612"

    def test_resolve_tasks(self):
        assert run_sync.resolve_tasks(None) == list(run_sync.DEFAULT_TASK_KEYS)
        assert run_sync.resolve_tasks("all") == list(run_sync.DEFAULT_TASK_KEYS)
        assert run_sync.resolve_tasks("bangumi") == ["bangumi"]
        with pytest.raises(ValueError):
            run_sync.resolve_tasks("nope")

    def test_build_scope_yuc_uses_latest_season(self, monkeypatch, fake_latest_season):
        args = run_sync.build_parser().parse_args([])
        assert run_sync.build_scope("yuc_schedule", args) == "202610"

    def test_build_scope_bangumi_requires_ids(self):
        args = run_sync.build_parser().parse_args(["--task", "bangumi"])
        with pytest.raises(ValueError, match="必须显式给出 ID"):
            run_sync.build_scope("bangumi", args)
