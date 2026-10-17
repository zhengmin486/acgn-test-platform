"""作品关系识别：从 Bangumi 条目的信息卡（infobox）识别作者 / 公司并归系列。

识别策略（白名单映射，只用标准库，不引入任何第三方依赖）：
- 数据源 = Bangumi API v0 条目 JSON 的 ``infobox`` 字段，实测结构为
  ``[{"key": "作者", "value": "<a ...>Type-Moon</a>"}, ...]``；
- 键白名单 INFOBOX_ROLE_KEYS：只认原作 / 导演 / 音乐 / 角色设计 / 系列构成 /
  动画制作 / 出版社 / 发行 这几类与「谁参与了这部作品」直接相关的键，
  价格 / 发售日 / 页数 / ISBN 等元数据键一概不认（噪声大、无关联价值）；
  键匹配前做 NFKC + casefold + 去空白归一，兼容「作者/著者/原作漫画」
  与日文变体（監督 / 音楽 / アニメーション制作）；
- 值拆分：HTML 标签（``<a>`` / ``<br>``）替换为换行后按换行 / 、 / ；拆分，
  再 html.unescape——信息卡的多值就是这样裸写的；
- 系列：infobox 的「系列」键（INFOBOX_SERIES_KEYS）直接给出系列名；
  另一条路是关联条目端点（见 parse_related_subjects / common_series_name），
  由回填 CLI 使用，仅当配对作品双方都在库内时才写，防止拿单边标题硬凑；
- 截断与裁剪由 crud.replace_content_entity_links 兜底（最多
  MAX_CONTENT_ENTITY_LINKS 项、名称 120 字符上限）。

写入语义（替换式，与作品标签一致）：
- crud.replace_content_entity_links 先删旧关联再建新关联，重复识别幂等、
  不堆叠行；信息卡更新后本地同步更新；
- 识别只认信息卡里真实出现的名字，不做任何推测性关联。

入口：
- Bangumi 导入（scripts/import_bangumi.py）随作品一起自动执行；
- 存量回填用 scripts/link_entities.py（预览 / --apply / --force / --series）。
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

from app import crud
from app.models import Content

# 单部作品最多保留的作者/公司关联数（与 crud.MAX_CONTENT_ENTITY_LINKS 一致）。
MAX_CONTENT_ENTITY_LINKS = crud.MAX_CONTENT_ENTITY_LINKS

# 实体名 / 系列名长度上限（与 crud 常量、模型字段长度对齐）。
ENTITY_NAME_MAX_LENGTH = crud.ENTITY_NAME_MAX_LENGTH
SERIES_NAME_MAX_LENGTH = crud.SERIES_NAME_MAX_LENGTH

# 系列公共前缀的最小长度：不足 3 字符的前缀（如「新」「第」）噪声太大，
# 硬凑出来的系列名比没有系列更糟。
MIN_SERIES_PREFIX = 3

# infobox 键白名单：归一化后的键 -> (角色, 实体类型)。
# 只收录「谁参与了这部作品」类键；元数据键（价格/发售日/页数/ISBN）不收。
INFOBOX_ROLE_KEYS: dict[str, tuple[str, str]] = {
    # 原作系（轻小说 / 漫画通常是不同的人，统一归到「原作」角色）
    "原作": ("原作", crud.ENTITY_TYPE_PERSON),
    "作者": ("原作", crud.ENTITY_TYPE_PERSON),
    "著者": ("原作", crud.ENTITY_TYPE_PERSON),
    "原作漫画": ("原作", crud.ENTITY_TYPE_PERSON),
    "漫画": ("原作", crud.ENTITY_TYPE_PERSON),
    "作画": ("原作", crud.ENTITY_TYPE_PERSON),
    "原案": ("原作", crud.ENTITY_TYPE_PERSON),
    "插画": ("插画", crud.ENTITY_TYPE_PERSON),
    # 导演
    "导演": ("导演", crud.ENTITY_TYPE_PERSON),
    "监督": ("导演", crud.ENTITY_TYPE_PERSON),
    "総監督": ("导演", crud.ENTITY_TYPE_PERSON),
    "总监督": ("导演", crud.ENTITY_TYPE_PERSON),
    # 音乐
    "音乐": ("音乐", crud.ENTITY_TYPE_PERSON),
    "音楽": ("音乐", crud.ENTITY_TYPE_PERSON),
    # 角色设计
    "角色设计": ("角色设计", crud.ENTITY_TYPE_PERSON),
    "キャラクターデザイン": ("角色设计", crud.ENTITY_TYPE_PERSON),
    "人物设计": ("角色设计", crud.ENTITY_TYPE_PERSON),
    # 系列构成
    "系列构成": ("系列构成", crud.ENTITY_TYPE_PERSON),
    # 公司：与详情页「制作：」同源但角色更细（动画制作 / 出版社 / 发行）
    "动画制作": ("动画制作", crud.ENTITY_TYPE_COMPANY),
    "アニメーション制作": ("动画制作", crud.ENTITY_TYPE_COMPANY),
    "动画制作公司": ("动画制作", crud.ENTITY_TYPE_COMPANY),
    "制作公司": ("动画制作", crud.ENTITY_TYPE_COMPANY),
    "出版社": ("出版社", crud.ENTITY_TYPE_COMPANY),
    "発行元": ("出版社", crud.ENTITY_TYPE_COMPANY),
    "发行": ("发行", crud.ENTITY_TYPE_COMPANY),
    "販売元": ("发行", crud.ENTITY_TYPE_COMPANY),
    "发售商": ("发行", crud.ENTITY_TYPE_COMPANY),
}

# infobox 系列键：值直接作为系列名。不收「丛书 / レーベル」（文库标签，
# 不是作品系列；把同文库的作品归成一个「系列」是错误语义）。
INFOBOX_SERIES_KEYS = frozenset({"系列", "series", "シリーズ"})

# 关联条目端点里算「同一系列」的关系类型（续集 / 前传 / 外传 / 衍生等）。
# 不含「不同演绎 / 同一角色」等弱关系——那些是「相关」不是「同系列」。
SERIES_RELATIONS = frozenset(
    {
        "续集",
        "前传",
        "外传",
        "衍生",
        "系列",
        "总集篇",
        "剧场版",
        "sequel",
        "prequel",
        "spinoff",
        "series",
        "summary",
    }
)

# 信息卡值里的 HTML 标签（<a href=...>名字</a> / <br> / <br/>）。
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# 多值分隔：换行（含 <br> 转换来的）、顿号、分号。不拆逗号——公司名里
# 可能出现 "A-1 Pictures, Inc."，拆了会造出错误实体。
_VALUE_SPLIT_RE = re.compile(r"[\n\r、；;]")


@dataclass(frozen=True)
class EntityLink:
    """一条「作品 ↔ 实体 ↔ 角色」识别结果。"""

    name: str
    entity_type: str
    role: str


@dataclass
class EntityRecognition:
    """一部作品的关系识别结果（预览与写入共用同一结构）。

    links：识别出的 (名称, 类型, 角色) 列表，按信息卡出现顺序；
    series：识别出的系列名（可能为 None）。
    """

    content_id: int
    links: list[EntityLink] = field(default_factory=list)
    series: str | None = None

    def has_data(self) -> bool:
        """是否识别到了任何关系数据（CLI 跳过判断与导入 reason 用）。"""
        return bool(self.links or self.series)

    def describe(self) -> str:
        """一行人类可读摘要（导入结果 reason 用）。"""
        parts: list[str] = []
        if self.links:
            parts.append(f"作者/公司 {len(self.links)} 项")
        if self.series:
            parts.append(f"系列『{self.series}』")
        return "，".join(parts)


def normalize_infobox_key(key: str | None) -> str:
    """信息卡键归一：NFKC + casefold + 去除全部空白。

    「动画 制作」与「动画制作」是同一个键；大小写不敏感让英文键
    （Series / series）也能命中。
    """
    if not key:
        return ""
    folded = unicodedata.normalize("NFKC", str(key)).casefold()
    return re.sub(r"\s+", "", folded)


def split_infobox_value(value) -> list[str]:
    """把信息卡的值拆成多个名字。

    - HTML 标签一律替换为换行：``<a>Type-Moon</a>`` -> ``Type-Moon``，
      ``A<br>B`` -> ``A\\nB``（多值最常见的写法）；
    - html.unescape 还原 ``&amp;`` 等实体；
    - 按换行 / 、 / ； 拆分，逐项去空白，丢空项。
    """
    if not isinstance(value, str):
        return []
    text = _HTML_TAG_RE.sub("\n", value)
    text = html.unescape(text)
    return [part.strip() for part in _VALUE_SPLIT_RE.split(text) if part.strip()]


def parse_infobox(payload: dict) -> list[tuple[str, str]]:
    """从条目 JSON 提取信息卡 (键, 值) 对；结构不符时返回空列表。

    防御性解析：payload 非 dict、infobox 非 list、条目非 dict、键值非
    字符串——任何一环不符都只跳过该条，不让识别拖垮导入。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("infobox")
    if not isinstance(raw, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        pairs.append((key, value))
    return pairs


def recognize(content_id: int, payload: dict) -> EntityRecognition:
    """只识别、不写库（预览与 CLI dry-run 用）。

    返回按信息卡顺序排列的关联列表（同 (名称, 角色) 去重，首次出现的
    类型为准）与系列名（取第一个非空的系列键）。列表截断到前
    MAX_CONTENT_ENTITY_LINKS 项——预览看到的就是落库的结果，
    crud.replace_content_entity_links 里的截断只是兜底。
    """
    links: list[EntityLink] = []
    seen: set[tuple[str, str]] = set()
    series: str | None = None

    for key, value in parse_infobox(payload):
        normalized = normalize_infobox_key(key)
        if normalized in INFOBOX_SERIES_KEYS:
            if series is None:
                names = split_infobox_value(value)
                if names:
                    series = crud.normalize_entity_name(names[0])[:SERIES_NAME_MAX_LENGTH]
            continue
        spec = INFOBOX_ROLE_KEYS.get(normalized)
        if spec is None:
            continue
        role, entity_type = spec
        for name in split_infobox_value(value):
            clean = crud.normalize_entity_name(name)[:ENTITY_NAME_MAX_LENGTH]
            if not clean:
                continue
            pair = (clean, role)
            if pair in seen:
                continue
            seen.add(pair)
            links.append(EntityLink(name=clean, entity_type=entity_type, role=role))

    # 截断放在最后而不是边扫边停：信息卡后面的「系列」键仍要被扫到。
    return EntityRecognition(
        content_id=content_id,
        links=links[:MAX_CONTENT_ENTITY_LINKS],
        series=series or None,
    )


# ---------- 关联条目端点（系列回填用） ----------


def parse_related_subjects(payload) -> list[tuple[int, str, str]]:
    """从 /v0/subjects/{id}/subjects 响应提取 (条目 id, 关系, 标题)。

    实测结构未最终确认（社区文档为 ``[{"id": int, "relation": str,
    "name_cn": str, "name": str}, ...]``），故防御性实现：只认 dict +
    整数 id + 字符串 relation + 非空标题，任何异常形状直接跳过——
    系列回填是可选项，不值得为它冒险崩溃。
    """
    if not isinstance(payload, list):
        return []
    related: list[tuple[int, str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        subject_id = item.get("id")
        relation = item.get("relation")
        title = (item.get("name_cn") or "") or (item.get("name") or "")
        if not isinstance(subject_id, int) or isinstance(subject_id, bool):
            continue
        if not isinstance(relation, str) or not relation.strip():
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        related.append((subject_id, relation.strip(), title.strip()))
    return related


def is_series_relation(relation: str | None) -> bool:
    """该关系是否算「同一系列」（归一化后命中 SERIES_RELATIONS）。"""
    return normalize_infobox_key(relation) in SERIES_RELATIONS


def common_series_name(title_a: str | None, title_b: str | None) -> str | None:
    """两个标题的公共前缀作为系列名（纯函数，CLI --series 模式用）。

    - 两个标题都先归一化（NFKC + strip）；任一为空或完全相同返回 None；
    - 取字符级公共前缀，去掉尾部分隔符（空格 / 连接号 / 冒号 / 斜杠 / 间隔号）；
    - 前缀不足 MIN_SERIES_PREFIX 返回 None：短前缀硬凑的系列名是噪声。

    例：「某某 第二季」+「某某 第一季」->「某某」；
        「Re：从零开始的异世界生活」+「Re：从零开始的异世界生活 冰结之绊」
        ->「Re：从零开始的异世界生活」。
    """
    a = crud.normalize_entity_name(title_a)
    b = crud.normalize_entity_name(title_b)
    if not a or not b or a == b:
        return None
    limit = min(len(a), len(b))
    index = 0
    while index < limit and a[index] == b[index]:
        index += 1
    # 尾部分隔符与「第」：「某某 第二季」+「某某 第一季」的公共前缀是
    # 「某某 第」，剥掉尾字才得到真正的系列名。
    prefix = a[:index].rstrip(" 　-–—:：/／·・,，、第")
    if len(prefix) < MIN_SERIES_PREFIX:
        return None
    return prefix


# ---------- 写入 ----------


def apply_recognition(db, recognition: EntityRecognition) -> EntityRecognition:
    """把识别结果写库（替换式）；自行 commit——与导入侧每步独立提交一致。

    关联与系列分两步写，任一失败抛 SQLAlchemyError 由调用方捕获（导入
    流程已有统一 handler，不会中断其余条目）。
    """
    crud.replace_content_entity_links(
        db,
        recognition.content_id,
        [(link.name, link.entity_type, link.role) for link in recognition.links],
    )
    if recognition.series:
        crud.set_content_series(db, recognition.content_id, recognition.series)
    return recognition


def link_content(db, content_id: int, payload: dict) -> EntityRecognition:
    """识别并写入单部作品（导入批次用）；返回识别结果。"""
    recognition = recognize(content_id, payload)
    if recognition.has_data():
        apply_recognition(db, recognition)
    return recognition


def link_payloads(
    db,
    items,
) -> dict[int, EntityRecognition]:
    """批量识别写入多部作品：items 为 [(content_id, 条目 JSON), ...]。

    供回填 CLI 复用（拉取一次、逐条识别写入）；空识别（信息卡没有可用
    键）也会出现在返回字典里，便于 CLI 统计「无匹配」条数。
    """
    results: dict[int, EntityRecognition] = {}
    for content_id, payload in items:
        results[content_id] = link_content(db, content_id, payload)
    return results
