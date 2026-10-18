"""封面图片处理管线：缩略图 / 压缩全图 / 上传落盘 / 模板助手。

与封面下载脚本（scripts/import_bangumi.py、scripts/fetch_covers.py）的分工：
那些脚本负责把 Bangumi 远端图落到本地（cover_storage_dir，URL 形如
/covers/cover_265.jpg）；本模块负责「图落盘之后」的一切——派生图、
按需服务、上传校验。页面不直接引用原图：

- 列表卡片走 /media/thumb/<原文件名>：400px 宽 WebP 缩略图，万部作品的
  列表页流量主要靠它省；前端加载失败时隐藏图片，回退渐变占位；
- 详情页走 /media/full/<原文件名>：≤1200px、≤400KB 的压缩全图，
  比直接铺原图每次访问省一半以上流量；
- 两个端点命中磁盘缓存直接返回，未命中用 Pillow 现场生成并原子落盘；
  生成失败（源图损坏等）服务端降级返回原图，原图也没有才 404；
- 管理员上传（POST /manage/contents/{id}/cover）复用同一套校验与派生
  管线：只认魔术字节、不信客户端文件名，落盘名称由服务端生成。
- 批量导入时低热作品不下封面（内容.remote_image_url 记远端地址），详情页
  第一次被打开时由 fetch_cover_on_demand 补齐并回填——省预付带宽又不留空洞。

预留：image_url 允许存远端完整地址（历史数据 / 手动维护），模板助手
对 http(s) 开头的值原样透传；将来图片切 COS / CDN 只改 MEDIA_BASE_URL
一项配置，数据与模板零迁移。
"""

from __future__ import annotations

import io
import mimetypes
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from app.config import settings

# 派生图统一 WebP：同画质体积约为 JPEG 的三分之一，Pillow 编解码无外部依赖。
VARIANT_SUFFIX = ".webp"
VARIANT_MEDIA_TYPE = "image/webp"

# 全图压缩的画质阶梯：先保尺寸再降画质；保底也接受最小画质——
# 封面可见性优先于体积达标（极端大图兜底）。
_FULL_QUALITY_STEPS = (82, 75, 68, 60, 52, 45, 38)

# 识别出的格式名 -> 存档扩展名（上传原图按真实格式保存，不沿用客户端声明）。
_EXTENSIONS = {"jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}


class MediaError(RuntimeError):
    """图片派生失败：源图不是受支持格式 / 解码错误 / 写入失败。"""


class UploadError(MediaError):
    """上传校验失败：空文件 / 超限 / 非图片 / 无法解码。

    code 供页面回显定位原因（too_big / not_image / corrupt / empty）。
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# ---------- 路径与文件名 ----------


def cover_root() -> Path:
    """封面根目录：相对路径按项目根解析（与 main.py 的 /covers 挂载一致）。"""
    root = Path(settings.cover_storage_dir)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[1] / root
    return root


# 头像统一正方形尺寸（页面最大显示 96px，256 足够 2x 屏）。
AVATAR_SIZE = 256


def avatar_root() -> Path:
    """头像根目录：相对路径按项目根解析（与 main.py 的 /avatars 挂载一致）。"""
    root = Path(settings.avatar_storage_dir)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[1] / root
    return root


def _variant_root(kind: str) -> Path:
    return cover_root() / {"thumb": "thumbs", "full": "full"}[kind]


def safe_cover_name(filename: str) -> str | None:
    """校验封面文件名，只允许单纯基名；不合法返回 None。

    派生图端点按文件名拼盘路径，这里是防路径穿越的唯一关口：
    「cover_265.jpg」合法；「../x」「a/b.jpg」「.env」「x.tar.gz」
    （去扩展名后仍含点）、空值、超长名全部拒绝。
    """
    if not filename or len(filename) > 120:
        return None
    if filename != Path(filename).name or filename.startswith("."):
        return None
    stem, dot, ext = filename.rpartition(".")
    if not stem or not dot or not ext:
        return None
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    if not set(stem) <= allowed or not set(ext) <= allowed:
        return None
    return filename


def _variant_path(kind: str, cover_name: str) -> Path:
    """派生图路径：thumbs/<去扩展名>.webp、full/<去扩展名>.webp。"""
    stem = cover_name.rpartition(".")[0]
    return _variant_root(kind) / f"{stem}{VARIANT_SUFFIX}"


def _atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再 os.replace：并发首访不会读到半张图。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp-", suffix=VARIANT_SUFFIX
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


# ---------- 图片识别与变换 ----------


def sniff_image(data: bytes) -> str | None:
    """按魔术字节识别图片格式（jpeg/png/webp/gif）；不认识返回 None。

    只认文件头：客户端提供的文件名与 Content-Type 都可能是谎话。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


def _open_image(data: bytes) -> Image.Image:
    """打开并完整解码图片；损坏 / 截断在这里抛异常（调用方转 MediaError）。"""
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _flatten(image: Image.Image) -> Image.Image:
    """统一转 RGB：带透明通道的图先贴白底，避免 WebP 输出出现黑块。"""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return image.convert("RGB")


def _resize_to_width(image: Image.Image, width: int) -> Image.Image:
    """按宽度等比缩放；已经更窄的图原样返回（只缩不放）。"""
    if image.width <= width:
        return image
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _encode_webp(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=quality)
    return buffer.getvalue()


def _build_variant(kind: str, source: Path) -> bytes:
    """生成一种派生图的字节流（thumb / full）；失败抛 MediaError。"""
    try:
        image = _flatten(_open_image(source.read_bytes()))
    except Exception as exc:  # 读取 / 解码失败统一转 MediaError
        raise MediaError(f"封面图无法处理：{exc}") from exc
    if kind == "thumb":
        return _encode_webp(_resize_to_width(image, settings.cover_thumb_width), quality=80)
    resized = _resize_to_width(image, settings.cover_full_max_width)
    budget = settings.cover_full_max_bytes
    smallest = b""
    for quality in _FULL_QUALITY_STEPS:
        smallest = _encode_webp(resized, quality)
        if len(smallest) <= budget:
            return smallest
    return smallest  # 保底：最小画质仍超限也接受，可见性优先


def generate_variant(kind: str, source: Path) -> Path:
    """生成并原子落盘一种派生图，返回其路径。"""
    path = _variant_path(kind, source.name)
    _atomic_write(path, _build_variant(kind, source))
    return path


def ensure_variant(kind: str, cover_name: str) -> Path | None:
    """取派生图：命中缓存返回路径；未命中现场生成；任何失败返回 None。

    None 的语义是「没有可用的派生图」，由调用方决定降级（服务端回原图）
    还是 404——本函数不抛异常，图片请求不因处理失败而 500。
    """
    name = safe_cover_name(cover_name)
    if name is None:
        return None
    cached = _variant_path(kind, name)
    if cached.is_file():
        return cached
    source = cover_root() / name
    if not source.is_file():
        return None
    try:
        return generate_variant(kind, source)
    except MediaError:
        return None


# ---------- 上传与清理 ----------


def save_upload(data: bytes, content_id: int) -> str:
    """校验并落盘管理员上传的封面，返回站内地址 /covers/<文件名>。

    三道校验：大小上限 -> 魔术字节 -> Pillow 实际解码；文件名由服务端
    生成（cover_u<条目id>_<毫秒时间戳>.<真实扩展名>），天然免疫路径
    穿越与恶意扩展名。原图落盘后当场生成缩略图 / 全图，与 Bangumi
    导入的封面走同一条派生管线。
    """
    if not data:
        raise UploadError("没有选择要上传的文件", "empty")
    limit = settings.cover_upload_max_bytes
    if len(data) > limit:
        raise UploadError(f"图片不能超过 {limit // (1024 * 1024)} MB", "too_big")
    image_format = sniff_image(data)
    if image_format is None:
        raise UploadError("只支持 JPEG / PNG / WebP / GIF 格式的图片", "not_image")
    try:
        _open_image(data)
    except Exception as exc:
        raise UploadError("图片文件无法解码，可能已损坏", "corrupt") from exc

    name = f"cover_u{content_id}_{int(time.time() * 1000)}.{_EXTENSIONS[image_format]}"
    root = cover_root()
    root.mkdir(parents=True, exist_ok=True)
    original = root / name
    original.write_bytes(data)
    for kind in ("thumb", "full"):
        try:
            generate_variant(kind, original)
        except MediaError:
            pass  # 原图已就位：页面端点会降级返回原图，上传本身算成功
    return f"/covers/{name}"


def _square(image: Image.Image) -> Image.Image:
    """居中裁成正方形：头像不做变形拉伸。"""
    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def save_avatar(data: bytes, user_id: int) -> str:
    """校验并落盘用户头像，返回站内地址 /avatars/<文件名>。

    与封面上传同一套校验（大小 -> 魔术字节 -> 实际解码）；服务端生成文件名、
    居中裁方、缩到 256×256 后编 WebP，避免原图直接上站。
    """
    if not data:
        raise UploadError("没有选择要上传的图片", "empty")
    limit = settings.avatar_upload_max_bytes
    if len(data) > limit:
        raise UploadError(f"头像不能超过 {limit // (1024 * 1024)} MB", "too_big")
    image_format = sniff_image(data)
    if image_format is None:
        raise UploadError("只支持 JPEG / PNG / WebP / GIF 格式的图片", "not_image")
    try:
        image = _open_image(data)
    except Exception as exc:
        raise UploadError("图片文件无法解码，可能已损坏", "corrupt") from exc

    try:
        square = _square(_flatten(image)).resize(
            (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS
        )
        payload = _encode_webp(square, 88)
    except Exception as exc:
        raise UploadError("头像处理失败，请换一张图片", "corrupt") from exc

    root = avatar_root()
    root.mkdir(parents=True, exist_ok=True)
    name = f"avatar_u{user_id}_{int(time.time() * 1000)}.webp"
    _atomic_write(root / name, payload)
    return f"/avatars/{name}"


def remove_avatar_file(avatar_url: str | None) -> bool:
    """删除被替换掉的旧头像（只认本站 /avatars/ 下的 .webp）。"""
    if not avatar_url or is_remote_url(avatar_url):
        return False
    if not avatar_url.startswith("/avatars/"):
        return False
    name = avatar_url.rpartition("/")[2]
    if not name or "/" in name or "\\" in name or not name.endswith(".webp"):
        return False
    try:
        (avatar_root() / name).unlink()
        return True
    except OSError:
        return False


def remove_cover_file(image_url: str | None) -> bool:
    """删除被替换掉的旧封面（仅限本站 /covers/ 下由本程序生成的文件）。

    连同过期派生图一起清理；远端地址 / 非本站路径一律忽略。
    """
    if not image_url or is_remote_url(image_url) or not image_url.startswith("/covers/"):
        return False
    name = safe_cover_name(image_url.rpartition("/")[2])
    if name is None:
        return False
    removed = False
    for path in (cover_root() / name, _variant_path("thumb", name), _variant_path("full", name)):
        try:
            path.unlink()
            removed = True
        except OSError:
            pass
    return removed


# ---------- 封面按需下载 ----------

# 远端封面下载的护栏：单张大小上限与超时。封面图本身只有几十到几百 KB，
# 超过 3 MB 一律不收，避免异常响应把磁盘撑满。
_ON_DEMAND_MAX_BYTES = 3 * 1024 * 1024
_ON_DEMAND_TIMEOUT = 15.0
_ON_DEMAND_HEADERS = {"User-Agent": "acgn-test-platform/0.1 (cover on demand)"}
# 只允许 Bangumi 自家 CDN，防止库里被塞进任意地址后被当成下载器用。
_ALLOWED_COVER_HOSTS = ("lain.bgm.tv", "bgm.tv", "api.bgm.tv")


def fetch_cover_on_demand(content) -> str | None:
    """下载 ``content`` 的远端封面并回填 image_url，返回站内地址；不可用返回 None。

    存在的意义：批量导入只给高热作品立刻下封面，低热作品只把地址存进
    ``contents.remote_image_url``。等这部作品真的被人打开时，由详情页调用本函数
    补齐封面并顺手清掉 remote_image_url——**省掉长尾的预付带宽，同时不留永久空洞**
    （谁访问谁补）。

    失败一律静默返回 None：封面不是关键路径，取不到就让页面回退渐变占位图，
    绝不让详情页因它报错或变慢。
    """
    remote = getattr(content, "remote_image_url", None)
    if not remote:
        return None
    if getattr(content, "image_url", None):
        return content.image_url  # 已有本地封面，不需要再下

    host = urllib.parse.urlsplit(remote).hostname or ""
    if host not in _ALLOWED_COVER_HOSTS:
        return None

    try:
        request = urllib.request.Request(remote, headers=_ON_DEMAND_HEADERS)
        with urllib.request.urlopen(request, timeout=_ON_DEMAND_TIMEOUT) as response:
            body = response.read(_ON_DEMAND_MAX_BYTES + 1)
    except Exception:
        return None  # 网络/超时/404 一律降级

    if not body or len(body) > _ON_DEMAND_MAX_BYTES:
        return None
    if sniff_image(body) is None:
        return None

    bangumi_id = getattr(content, "bangumi_id", None) or content.id
    suffix = Path(urllib.parse.urlsplit(remote).path).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        suffix = ".jpg"
    name = f"cover_{bangumi_id}{suffix}"

    root = cover_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        original = root / name
        _atomic_write(original, body)
        for kind in ("thumb", "full"):
            try:
                generate_variant(kind, original)
            except MediaError:
                pass
    except OSError:
        return None

    content.image_url = f"/covers/{name}"
    content.remote_image_url = None
    return content.image_url


# ---------- 模板助手 ----------


def is_remote_url(value: str | None) -> bool:
    """是否是远端完整地址（http/https 开头）。"""
    return bool(value) and value.startswith(("http://", "https://"))


def media_url(site_path: str) -> str:
    """站内相对地址拼 MEDIA_BASE_URL（留空即原样返回）。"""
    base = settings.media_base_url.strip().rstrip("/")
    return f"{base}{site_path}" if base else site_path


def _variant_src(kind: str, image_url: str | None) -> str:
    """本地封面走派生图端点；远端地址原样透传；空值返回空串。"""
    if not image_url:
        return ""
    if is_remote_url(image_url):
        return image_url
    name = safe_cover_name(image_url.rpartition("/")[2])
    return media_url(f"/media/{kind}/{name}") if name else image_url


def thumb_src(image_url: str | None) -> str:
    """列表缩略图地址（模板用）：本地封面 -> /media/thumb/<文件名>。"""
    return _variant_src("thumb", image_url)


def full_src(image_url: str | None) -> str:
    """详情全图地址（模板用）：本地封面 -> /media/full/<文件名>。"""
    return _variant_src("full", image_url)


def guess_media_type(path: Path) -> str:
    """按扩展名猜 MIME（原图降级返回时用）；猜不出按通用二进制流。"""
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
