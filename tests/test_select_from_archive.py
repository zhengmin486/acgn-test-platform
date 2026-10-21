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
    apply_ingest_policy,
    build_payload,
    has_anime_manga_relation,
    iter_subjects,
    natural_quota,
    parse_wiki_infobox,
    select_from_archive,
)


def _make_dump(tmp_path: Path, entries: list[dict]) -> Path:
    """造一个最小归档 zip（JSON Lines 格式，成员名与官方一致）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
        archive.writestr("dump/subject.jsonlines", body)
    path = tmp_path / "dump.zip"
    path.write_bytes(buffer.getvalue())
    return path


def _entry(sid, bgm_type=2, rank=10, score=8.0, nsfw=False, platform=None, **extra):
    """按官方归档的真实字段形态造条目：score 顶层 float、rank 顶层整数、
    书籍区（type=1）platform 是数字代码（1001=漫画/1002=小说）。"""
    entry = {
        "id": sid,
        "type": bgm_type,
        "name": f"Subject {sid}",
        "name_cn": f"作品{sid}",
        "summary": f"这是 {sid} 的简介",
        "nsfw": nsfw,
        "score": score,
        "rank": rank,
        "score_details": {"8": 1000, "9": 500},
        "tags": [{"name": "科幻", "count": 100}],
        "infobox": "|中文名: 作品\r\n|话数: 12",
        "date": "2020-01-01",
        "platform": platform if platform is not None else ("TV" if bgm_type == 2 else ("PC" if bgm_type == 4 else None)),
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

    def test_music_real_and_artbook_skipped(self, tmp_path):
        """音乐(3)、三次元真人(6)、画集(platform=1003) 都不属于本站四类型。

        回归防护：Archive README 写「6=漫画」是错的——实测 6 是真人剧
        （一升的眼泪/白夜行），曾经按它映射导致日剧入库。
        """
        entries = [
            _entry(1, bgm_type=3, rank=1),                     # 音乐
            _entry(2, bgm_type=6, rank=2),                     # 真人剧
            _entry(3, bgm_type=1, rank=3, platform=1001),      # 漫画
            _entry(4, bgm_type=1, rank=4, platform=1002),      # 小说
            _entry(5, bgm_type=1, rank=5, platform=1003),      # 画集
        ]
        dump = _make_dump(tmp_path, entries)

        results, counter, _scanned = select_from_archive(
            dump, {"anime": 5, "game": 5, "novel": 5, "comic": 5}
        )

        assert {p["id"] for p in results} == {3, 4}
        by_type = {p["id"]: p["_content_type"] for p in results}
        assert by_type == {3: "comic", 4: "novel"}
        assert counter["comic"] == 1
        assert counter["novel"] == 1

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


# ---------- 收录口径过滤（2026-10-21：轻小说 / galge 或相关 IP 游戏） ----------


def _policy_payload(bangumi_id: int, content_type: str, **extra) -> dict:
    """构造 apply_ingest_policy 的输入 payload（含选品内部键 _content_type）。"""
    payload = {
        "id": bangumi_id,
        "type": {"anime": 2, "game": 4, "novel": 1, "comic": 1}.get(content_type, 2),
        "name": f"entry-{bangumi_id}",
        "name_cn": f"条目{bangumi_id}",
        "summary": "简介。",
        "rating": {"score": 7.0, "rank": 0, "total": 10},
        "tags": [],
        "infobox": [],
        "date": "2026-10-01",
        "nsfw": False,
        "platform": 1002 if content_type == "novel" else "",
        "_content_type": content_type,
        "_tier_rank": 1,
    }
    payload.update(extra)
    return payload


class TestHasAnimeMangaRelation:
    def test_anime_type_hits(self):
        assert has_anime_manga_relation([{"id": 9, "type": 2, "relation": "游戏"}])

    def test_wording_fallback(self):
        assert has_anime_manga_relation([{"id": 9, "relation": "漫画化"}])
        assert has_anime_manga_relation([{"id": 9, "relation": "动画化"}])

    def test_unrelated_shapes(self):
        assert not has_anime_manga_relation(None)
        assert not has_anime_manga_relation("bad")
        assert not has_anime_manga_relation([{"id": 9, "type": 4, "relation": "游戏"}])
        assert not has_anime_manga_relation([])


class TestApplyIngestPolicy:
    @staticmethod
    def _run(payloads, related=None, error=False):
        calls = []

        def fake_fetch(subject_id, **kwargs):
            calls.append(subject_id)
            if error:
                raise RuntimeError("mirror down")
            return related

        kept, stats = apply_ingest_policy(payloads, related_fetcher=fake_fetch)
        return kept, stats, calls

    def test_literature_novel_dropped_light_novel_kept(self):
        literature = _policy_payload(
            1, "novel", infobox=[{"key": "レーベル", "value": "新潮文庫"}]
        )
        light = _policy_payload(
            2, "novel", tags=[{"name": "轻小说", "count": 99}],
            infobox=[{"key": "文库", "value": "電撃文庫"}],
        )
        kept, stats, _calls = self._run([literature, light])
        assert [p["id"] for p in kept] == [2]
        assert stats["novel_dropped"] == 1

    def test_galge_game_kept_without_network(self):
        galge = _policy_payload(3, "game", tags=[{"name": "GALGAME", "count": 66}])
        kept, stats, calls = self._run([galge])
        assert [p["id"] for p in kept] == [3]
        assert stats["game_galge"] == 1 and calls == []

    def test_related_game_marked_and_kept(self):
        plain = _policy_payload(4, "game")
        kept, stats, calls = self._run([plain], related=[{"id": 99, "type": 2, "relation": "游戏"}])
        assert [p["id"] for p in kept] == [4]
        assert kept[0]["_site_related_ip"] is True
        assert stats["game_related"] == 1 and calls == [4]

    def test_unrelated_game_dropped(self):
        plain = _policy_payload(5, "game")
        kept, stats, _calls = self._run([plain], related=[{"id": 100, "type": 4, "relation": "游戏"}])
        assert kept == []
        assert stats["game_dropped"] == 1

    def test_fetch_failure_counts_and_drops(self):
        plain = _policy_payload(6, "game")
        kept, stats, _calls = self._run([plain], error=True)
        assert kept == []
        assert stats["game_fetch_failed"] == 1 and stats["game_dropped"] == 1

    def test_anime_and_comic_pass_through(self):
        anime = _policy_payload(7, "anime")
        comic = _policy_payload(8, "comic", platform=1001)
        kept, stats, _calls = self._run([anime, comic])
        assert [p["id"] for p in kept] == [7, 8]
        assert stats == {
            "novel_dropped": 0, "game_galge": 0, "game_related": 0,
            "game_dropped": 0, "game_fetch_failed": 0,
        }

    def test_order_preserved(self):
        payloads = [
            _policy_payload(10, "anime"),
            _policy_payload(11, "novel", tags=[{"name": "轻小说", "count": 1}]),
            _policy_payload(12, "game", tags=[{"name": "GALGAME", "count": 1}]),
        ]
        kept, _stats, _calls = self._run(payloads, related=[])
        assert [p["id"] for p in kept] == [10, 11, 12]
