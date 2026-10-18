"""归档选品器（scripts/select_from_archive.py）的行为测试。

官方归档（github.com/bangumi/Archive）是选品的数据源：rank 类别内排名 /
score 评分 / infobox 原始 wiki 字符串。这里钉住三条关键约定：

- wiki infobox 字符串必须能转成 API 的 [{key, value}] 结构（主创识别靠它）；
- rating 必须有效（score>0）才构造 payload——本站不伪造评分；
- 选品按 rank 升序、每类配额上限、nsfw 一律排除。
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.select_from_archive import (  # noqa: E402
    build_payload,
    iter_subjects,
    natural_quota,
    parse_wiki_infobox,
    select_from_archive,
)


def _make_dump(tmp_path: Path, entries: list[dict]) -> Path:
    """造一个最小归档 zip（JSON Lines 格式，与官方一致）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
        archive.writestr("dump/subject.json", body)
    path = tmp_path / "dump.zip"
    path.write_bytes(buffer.getvalue())
    return path


def _entry(sid, bgm_type=2, rank=10, score=8.0, nsfw=False, **extra):
    entry = {
        "id": sid,
        "type": bgm_type,
        "name": f"Subject {sid}",
        "name_cn": f"作品{sid}",
        "summary": f"这是 {sid} 的简介",
        "nsfw": nsfw,
        "score": {"score": score, "rank": rank, "score_details": {"8": 1000, "9": 500}},
        "tags": [{"name": "科幻", "count": 100}],
        "infobox": "|中文名: 作品\r\n|话数: 12",
        "date": "2020-01-01",
    }
    entry.update(extra)
    return entry


class TestWikiInfobox:
    def test_basic_pairs(self):
        raw = "|中文名: 作品\r\n|话数: 12\r\n|制作: 公司A"
        pairs = parse_wiki_infobox(raw)
        assert pairs == [
            {"key": "中文名", "value": "作品"},
            {"key": "话数", "value": "12"},
            {"key": "制作", "value": "公司A"},
        ]

    def test_multiline_value_belongs_to_last_key(self):
        raw = "|别名: {\n 第一行\n 第二行}\n|话数: 12"
        pairs = parse_wiki_infobox(raw)
        assert len(pairs) == 2
        assert pairs[0]["key"] == "别名"
        assert pairs[1] == {"key": "话数", "value": "12"}

    def test_malformed_lines_skipped(self):
        raw = "|合法键: 值\n没有竖线的行\n|=没键名\n|另一个: 键"
        pairs = parse_wiki_infobox(raw)
        assert [p["key"] for p in pairs] == ["合法键", "另一个"]

    def test_empty(self):
        assert parse_wiki_infobox("") == []


class TestBuildPayload:
    def test_api_shaped_payload(self, tmp_path):
        payload = build_payload(_entry(1))
        assert payload["id"] == 1
        assert payload["type"] == 2
        assert payload["rating"]["score"] == 8.0
        assert payload["rating"]["rank"] == 10
        assert payload["rating"]["total"] == 1500
        assert payload["tags"] == [{"name": "科幻", "count": 100}]
        # infobox 已转成 API 结构（key/value），主创识别可用
        assert {"key": "中文名", "value": "作品"} in payload["infobox"]

    def test_invalid_score_rejected(self, tmp_path):
        assert build_payload(_entry(2, score=0)) is None
        assert build_payload(_entry(3, score=None)) is None

    def test_empty_title_rejected(self):
        entry = _entry(4)
        entry["name"] = ""
        entry["name_cn"] = ""
        assert build_payload(entry) is None

    def test_rank_zero_becomes_zero(self):
        payload = build_payload(_entry(5, rank=0))
        assert payload["rating"]["rank"] == 0


class TestSelectFromArchive:
    def test_picks_best_rank_per_type(self, tmp_path):
        entries = [
            _entry(1, bgm_type=2, rank=5),
            _entry(2, bgm_type=2, rank=1),
            _entry(3, bgm_type=2, rank=9),
            _entry(4, bgm_type=4, rank=2),
        ]
        dump = _make_dump(tmp_path, entries)

        results, counter, scanned = select_from_archive(
            dump, {"anime": 2, "game": 1, "novel": 0, "comic": 0}
        )

        assert scanned == 4
        assert counter["anime"] == 3
        anime = [p for p in results if p["_content_type"] == "anime"]
        # 按 rank 升序，配额 2 → rank 1 和 5，rank 9 被挤掉
        assert [p["_tier_rank"] for p in anime] == [1, 5]
        assert [p["id"] for p in results if p["_content_type"] == "game"] == [4]

    def test_nsfw_excluded(self, tmp_path):
        entries = [_entry(1, rank=1, nsfw=True), _entry(2, rank=2)]
        dump = _make_dump(tmp_path, entries)

        results, counter, _scanned = select_from_archive(
            dump, {"anime": 5, "game": 0, "novel": 0, "comic": 0}
        )

        assert [p["id"] for p in results] == [2]

    def test_rank_zero_and_out_of_range_excluded(self, tmp_path):
        entries = [
            _entry(1, rank=0),      # 无排名
            _entry(2, rank=9999),   # 超出 max_rank
            _entry(3, rank=100),
        ]
        dump = _make_dump(tmp_path, entries)

        results, _counter, _scanned = select_from_archive(
            dump, {"anime": 5, "game": 0, "novel": 0, "comic": 0}, max_rank=3000
        )

        assert [p["id"] for p in results] == [3]

    def test_music_and_real_skipped(self, tmp_path):
        entries = [
            _entry(1, bgm_type=3, rank=1),   # 音乐
            _entry(2, bgm_type=5, rank=2),   # 三次元（API 语义）
            _entry(3, bgm_type=1, rank=3),   # 书籍 -> novel
        ]
        dump = _make_dump(tmp_path, entries)

        results, counter, _scanned = select_from_archive(
            dump, {"anime": 5, "game": 5, "novel": 5, "comic": 0}
        )

        assert 1 not in [p["id"] for p in results]
        assert 2 not in [p["id"] for p in results]
        assert [p["id"] for p in results] == [3]
        assert results[0]["_content_type"] == "novel"

    def test_summary_required(self, tmp_path):
        entries = [_entry(1, rank=1, summary=""), _entry(2, rank=2)]
        dump = _make_dump(tmp_path, entries)

        results, _counter, _scanned = select_from_archive(
            dump, {"anime": 5, "game": 0, "novel": 0, "comic": 0}
        )

        assert [p["id"] for p in results] == [2]


class TestNaturalQuota:
    def test_ratios(self):
        assert natural_quota(1000) == {
            "anime": 500, "game": 200, "novel": 180, "comic": 120,
        }

    def test_small_total(self):
        assert natural_quota(100) == {
            "anime": 50, "game": 20, "novel": 18, "comic": 12,
        }
