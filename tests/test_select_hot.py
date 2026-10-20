"""热门选品器（scripts/select_hot.py）的行为测试。

全部用桩函数替换网络，不触网、不写真实库。守住三条关键约定：

- **路径必须是 /{cat}/browser**：分区首页的 ?page=N / ?sort= 参数全部被忽略，
  翻页拿到的还是同一批；只有 browser 路径才真正翻页。踩过这个坑，
  用测试钉死，免得日后「优化」回去。
- **分层按收藏数**：S/A/B/C 的阈值是资源分配依据，改动会让批量下载量失衡。
- **按需封面分层决定**：C 层不批量下封面（省带宽），改走详情页按需下载。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import select_hot  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Bangumi 真实条目类型编码：1 书籍（漫画/小说按 platform 拆）、2 动画、
# 3 音乐、4 游戏、6 真人剧。**没有「6=漫画」这回事**（Archive README 写错了）。
SUBJECT_TYPE_OF = {"anime": 2, "game": 4, "comic": 1, "novel": 1}


def make_candidate(sid, ctype="anime", collect=0, score=0.0, title="x"):
    # 类型显式传 resolved_type：书籍(1) 光看 subject_type 分不出漫画还是小说，
    # 真实流程里由 resolve_content_type 从 payload 判好后落进来。
    return select_hot.Candidate(
        bangumi_id=sid,
        source_category="anime",
        subject_type=SUBJECT_TYPE_OF[ctype],
        resolved_type=ctype,
        collect=collect,
        score=score,
        title=title,
        detail_ok=True,
    )


class TestTypeMapping:
    def test_site_types_resolvable(self):
        """四类型都要能从 Bangumi payload 解析出来，否则 book 分区白抓。

        钉住两个坑：
        - 只看 ``type`` 不够——漫画与小说同属书籍(type=1)，得按 platform 拆；
        - 真人剧是 type=6 而不是漫画，音乐是 type=3，两者都不收。
        """
        assert select_hot.resolve_content_type({"type": 2}) == "anime"
        assert select_hot.resolve_content_type({"type": 4}) == "game"
        assert select_hot.resolve_content_type({"type": 1, "platform": "漫画"}) == "comic"
        assert (
            select_hot.resolve_content_type(
                {"type": 1, "platform": "小说", "infobox": [{"key": "出版社", "value": "集英社"}]}
            )
            == "novel"
        )
        assert select_hot.resolve_content_type({"type": 3}) is None  # 音乐
        assert select_hot.resolve_content_type({"type": 6}) is None  # 真人剧，不是漫画


class TestBrowserPath:
    def test_uses_browser_path_with_sort_and_page(self):
        """必须请求 /anime/browser?sort=trends&page=1 —— 首页路径翻页无效。"""
        seen: list[str] = []

        class Opener:
            def open(self, request, timeout=None):
                seen.append(request.full_url)
                html = b'<a href="/subject/111">a</a>'
                return FakeResponse(html)

        ids, ok = select_hot.fetch_category_ids(
            "anime", 1, opener=Opener(), sorts=("trends",)
        )

        assert ids == [111]
        assert ok == 1
        assert seen[0] == "https://bgm.tv/anime/browser?sort=trends&page=1"
        assert "/anime/browser/" not in seen[0]  # 带尾斜杠会 403

    def test_empty_page_stops_paging(self):
        """某页没有 ID 说明翻到头了，不该继续刷。"""
        calls: list[str] = []

        class Opener:
            def open(self, request, timeout=None):
                calls.append(request.full_url)
                if "page=1" in request.full_url:
                    return FakeResponse(b'<a href="/subject/1">x</a>')
                return FakeResponse(b"<html>no more</html>")

        ids, ok = select_hot.fetch_category_ids(
            "anime", 8, opener=Opener(), sorts=("trends",), sleep=lambda _s: None
        )

        assert ids == [1]
        assert ok == 1
        assert len(calls) == 2  # 第 2 页空 -> 停止

    def test_retries_transient_disconnect(self):
        """bgm.tv 会主动断连，单页失败要重试而不是直接丢一批候选。"""
        attempts = {"n": 0}

        class FlakyOpener:
            def open(self, request, timeout=None):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise OSError("远程主机强迫关闭了一个现有的连接")
                return FakeResponse(b'<a href="/subject/7">x</a>')

        ids, ok = select_hot.fetch_category_ids(
            "anime", 1, opener=FlakyOpener(), sorts=("trends",), sleep=lambda _s: None
        )

        assert attempts["n"] == 2
        assert ids == [7]
        assert ok == 1


class TestTiers:
    def test_tier_by_collection_threshold(self):
        assert make_candidate(1, collect=60000).tier == "S"
        assert make_candidate(2, collect=12000).tier == "A"
        assert make_candidate(3, collect=5000).tier == "B"
        assert make_candidate(4, collect=100).tier == "C"

    def test_high_score_promotes_low_collection(self):
        """收藏不高但评分很亮的冷门高分作也升到 B，避免被埋掉。"""
        assert make_candidate(5, collect=200, score=8.6).tier == "B"
        assert make_candidate(6, collect=200, score=7.0).tier == "C"

    def test_cover_policy_by_tier(self):
        """S/A/B 批量下封面；C 不下，交给详情页按需下载。"""
        assert make_candidate(1, collect=60000).need_cover_now is True
        assert make_candidate(2, collect=12000).need_cover_now is True
        assert make_candidate(3, collect=5000).need_cover_now is True
        assert make_candidate(4, collect=100).need_cover_now is False


class TestAllocate:
    def _report(self, spec):
        report = select_hot.SelectionReport()
        report.candidates = [
            make_candidate(sid, ctype=ctype, collect=collect)
            for sid, ctype, collect in spec
        ]
        return report

    def test_respects_natural_quota(self):
        """自然比例配额：动画 50% / 游戏 20% / 小说 18% / 漫画 12%。"""
        spec = []
        sid = 1
        for _ in range(600):
            spec.append((sid, "anime", 1000)); sid += 1
        for _ in range(300):
            spec.append((sid, "game", 1000)); sid += 1
        for _ in range(250):
            spec.append((sid, "novel", 1000)); sid += 1
        for _ in range(200):
            spec.append((sid, "comic", 1000)); sid += 1

        picked = select_hot.allocate(self._report(spec), total=1000)

        counts: dict[str, int] = {}
        for c in picked:
            counts[c.content_type] = counts.get(c.content_type, 0) + 1
        assert counts["anime"] == 500
        assert counts["game"] == 200
        assert counts["novel"] == 180
        assert counts["comic"] == 120

    def test_picks_hottest_within_each_type(self):
        """每类内部按收藏降序取，权重高的先占配额。"""
        spec = [
            (1, "anime", 90000),
            (2, "anime", 100),
            (3, "anime", 50000),
        ]
        picked = select_hot.allocate(
            self._report(spec), total=2, quotas={"anime": 1.0, "game": 0.0, "novel": 0.0, "comic": 0.0}
        )

        assert [c.bangumi_id for c in picked][:1] == [1]

    def test_backfills_when_quota_short(self):
        """某类候选不够时，余额让给其它类型，尽量凑满 total。"""
        spec = [(i, "anime", 1000) for i in range(1, 21)]
        picked = select_hot.allocate(
            self._report(spec), total=10, quotas={"anime": 0.05, "game": 0.0, "novel": 0.0, "comic": 0.0}
        )

        assert len(picked) == 10
        assert all(c.content_type == "anime" for c in picked)

    def test_no_duplicate_ids_even_if_same_work_from_two_categories(self):
        """同一部作品可能同时出现在 anime 与 book 分区的候选池里，抽取时只取一次。"""
        spec = [
            (1, "anime", 90000),   # 同一 bangumi_id 在两个分区各出现一次
            (1, "novel", 90000),
            (2, "anime", 80000),
            (2, "game", 80000),
            (3, "anime", 70000),
        ]
        picked = select_hot.allocate(
            self._report(spec),
            total=3,
            quotas={"anime": 1.0, "game": 0.0, "novel": 0.0, "comic": 0.0},
        )

        assert len({c.bangumi_id for c in picked}) == len(picked)


class TestCollectCandidates:
    def test_filters_unsupported_types(self, monkeypatch):
        """音乐(3)/三次元(5) 不属于本站四类型，详情拿到也要丢掉。"""
        monkeypatch.setattr(
            select_hot,
            "fetch_category_ids",
            lambda *a, **k: ([10, 11], 1),
        )

        def fake_detail(sid, opener=None, sleep=None):
            return {
                "id": sid,
                "type": {10: 2, 11: 3}[sid],  # 10=动画，11=音乐
                "name_cn": f"作品{sid}",
                "rating": {"score": 8.0},
                "collection": {"collect": 5000},
                "images": {"medium": "http://x/m.jpg", "large": "http://x/l.jpg"},
            }

        report = select_hot.collect_candidates(
            opener=object(),
            fetch_detail_fn=fake_detail,
            sleep=lambda _s: None,
        )

        assert [c.bangumi_id for c in report.candidates] == [10]
        assert report.candidates[0].content_type == "anime"

    def test_book_entries_split_by_platform(self, monkeypatch):
        """书籍(type=1)按 platform 拆成 comic/novel —— 不能整类丢掉。

        曾经这里只查 TYPE_MAPPING（只有 2/4），book 分区抓回来的书全被跳过，
        小说 18% / 漫画 12% 配额恒为 0、余额全溢出给动画。
        """
        monkeypatch.setattr(
            select_hot, "fetch_category_ids", lambda *a, **k: ([30, 31, 32], 1)
        )

        def fake_detail(sid, opener=None, sleep=None):
            data = {
                30: {"type": 1, "platform": "漫画"},
                31: {"type": 1, "platform": "小说"},  # 无中文出版社信号 -> 留作日本文学
                32: {"type": 1, "platform": "画集"},  # 本站不收
            }[sid]
            data.update(
                {
                    "name_cn": f"书{sid}",
                    "rating": {"score": 8.0},
                    "collection": {"collect": 5000},
                }
            )
            return data

        report = select_hot.collect_candidates(
            opener=object(), fetch_detail_fn=fake_detail, sleep=lambda _s: None
        )

        assert [c.content_type for c in report.candidates] == ["comic", "novel"]

    def test_skips_titleless_entries(self, monkeypatch):
        """标题为空（只有原名也空）的条目不入库。"""
        monkeypatch.setattr(select_hot, "fetch_category_ids", lambda *a, **k: ([20], 1))

        def fake_detail(sid, opener=None, sleep=None):
            return {"id": sid, "type": 2, "name_cn": "", "name": ""}

        report = select_hot.collect_candidates(
            opener=object(), fetch_detail_fn=fake_detail, sleep=lambda _s: None
        )

        assert report.candidates == []