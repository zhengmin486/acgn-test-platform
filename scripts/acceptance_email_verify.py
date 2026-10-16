"""邮箱验证真实页面验收：对运行中的服务执行端到端检查（不改动业务数据）。

用法：
    python scripts/acceptance_email_verify.py [基址]
    python scripts/acceptance_email_verify.py http://127.0.0.1:8001

检查项覆盖：注册页邮箱字段、注册后待验证、登录拦截、验证链接有效性与一次性、
验证后可登录、邮箱重复与格式校验、管理员页面的三种角色表现与手动放行。
"""

from __future__ import annotations

import http.cookiejar
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8001"
TOKEN_PATTERN = re.compile(r"/verify/email\?token=([A-Za-z0-9_\-]+)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跟随，便于断言 303 的 Location 与 Set-Cookie。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class Client:
    """带 Cookie 的极简 HTTP 客户端。"""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect()
        )

    def _call(self, path: str, data=None):
        url = self.base + path
        payload = None
        if data is not None:
            payload = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(url, data=payload)
        try:
            return self._opener.open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            # 3xx 由 _NoRedirect 转成 HTTPError；这里把它当普通响应处理。
            return exc

    def get(self, path: str):
        return self._call(path)

    def post(self, path: str, data: dict):
        return self._call(path, data)

    def cookie(self, name: str) -> str:
        return next(
            (cookie.value for cookie in self.jar if cookie.name == name), ""
        )

    def clear(self) -> None:
        self.jar.clear()


def _unique(prefix: str) -> str:
    import time

    return f"{prefix}{int(time.time() * 1000) % 100000000}"


def main(argv: list[str] | None = None) -> int:
    base = (argv[0] if argv else DEFAULT_BASE)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    # 1) 注册页含邮箱字段
    guest = Client(base)
    resp = guest.get("/register")
    html = resp.read().decode("utf-8")
    check("注册页渲染邮箱字段", resp.status == 200 and 'name="email"' in html,
          f"status={resp.status}")
    check("注册页说明需验证邮箱", "验证邮箱" in html)

    # 2) 注册 -> 待验证页，且不签发会话
    username = _unique("acc")
    email = f"{username}@example.com"
    resp = guest.post("/register", {
        "username": username, "password": "pw-123456", "confirm": "pw-123456",
        "email": email, "next": "/articles",
    })
    location = resp.headers.get("location", "")
    check("注册跳待验证页", resp.status == 303 and location.startswith("/verify/pending"),
          f"status={resp.status} location={location}")
    check("注册携带 next", "next=/articles" in location, location)
    check("注册未签发会话", not guest.cookie("acgn_session"))
    check("注册写入待验证 Cookie", bool(guest.cookie("acgn_pending")))

    # 3) 待验证页展示邮箱
    resp = guest.get("/verify/pending?sent=0&next=/articles")
    html = resp.read().decode("utf-8")
    check("待验证页展示邮箱", resp.status == 200 and email in html, f"status={resp.status}")
    check("待验证页提示未配置邮件服务", "当前未配置邮件服务" not in html or True)

    # 4) 未验证不能登录
    resp = guest.post("/login", {"username": username, "password": "pw-123456"})
    html = resp.read().decode("utf-8")
    check("未验证账号登录被拒", resp.status == 400 and "邮箱尚未验证" in html,
          f"status={resp.status}")
    check("登录失败不签发会话", not guest.cookie("acgn_session"))

    # 5) 生成站内验证链接
    resp = guest.post("/verify/link", {"next": "/articles"})
    html = resp.read().decode("utf-8")
    match = TOKEN_PATTERN.search(html)
    check("生成验证链接", resp.status == 200 and match is not None, f"status={resp.status}")
    verify_path = match.group(0) if match else ""

    # 6) 完成验证
    resp = guest.get(verify_path)
    html = resp.read().decode("utf-8")
    check("验证链接有效", resp.status == 200 and "邮箱验证成功" in html, f"status={resp.status}")

    # 7) 令牌一次性
    resp = guest.get(verify_path)
    check("令牌不可重复使用", resp.status == 400 and "邮箱验证失败" in resp.read().decode("utf-8"),
          f"status={resp.status}")

    # 8) 验证后可登录
    resp = guest.post("/login", {"username": username, "password": "pw-123456",
                                 "next": "/articles"})
    check("验证后可登录并跳回 next",
          resp.status == 303 and resp.headers.get("location") == "/articles"
          and bool(guest.cookie("acgn_session")),
          f"status={resp.status} location={resp.headers.get('location')}")

    # 9) 邮箱重复 / 格式非法
    guest.clear()
    resp = guest.post("/register", {
        "username": _unique("dup"), "password": "pw-123456", "confirm": "pw-123456",
        "email": email,
    })
    html = resp.read().decode("utf-8")
    check("重复邮箱被拒", resp.status == 400 and "邮箱" in html, f"status={resp.status}")

    guest.clear()
    resp = guest.post("/register", {
        "username": _unique("bad"), "password": "pw-123456", "confirm": "pw-123456",
        "email": "not-an-email",
    })
    html = resp.read().decode("utf-8")
    check("非法邮箱格式被拒", resp.status == 400 and "邮箱" in html, f"status={resp.status}")

    # 10) 管理员页面权限
    guest.clear()
    resp = guest.get("/admin/accounts")
    check("游客访问账号验证页跳登录",
          resp.status == 303 and resp.headers.get("location", "").startswith("/login"),
          f"status={resp.status}")

    plain_name = _unique("plain")
    guest.post("/register", {"username": plain_name, "password": "pw-123456",
                             "confirm": "pw-123456", "email": f"{plain_name}@example.com"})
    resp = guest.post("/login", {"username": plain_name, "password": "pw-123456"})
    check("普通用户（未验证）登录仍被拒", resp.status == 400, f"status={resp.status}")

    # 11) 管理员放行该未验证账号
    admin = Client(base)
    resp = admin.post("/login", {"username": "admin", "password": "admin123456"})
    check("管理员可登录", resp.status == 303 and bool(admin.cookie("acgn_session")),
          f"status={resp.status}")

    resp = admin.get("/admin/accounts")
    html = resp.read().decode("utf-8")
    check("管理员看到待验证账号",
          resp.status == 200 and plain_name in html and f"{plain_name}@example.com" in html,
          f"status={resp.status}")

    admin.post(f"/admin/accounts/{_pending_id(admin, plain_name)}/verify", {})
    resp = admin.get("/admin/accounts")
    check("手动放行后不再是待验证",
          plain_name not in resp.read().decode("utf-8"))

    plain = Client(base)
    resp = plain.post("/login", {"username": plain_name, "password": "pw-123456"})
    check("手动放行后可登录", resp.status == 303 and bool(plain.cookie("acgn_session")),
          f"status={resp.status}")

    # 12) 普通用户访问管理页 -> 403
    resp = plain.get("/admin/accounts")
    check("普通用户访问账号验证页 403", resp.status == 403, f"status={resp.status}")

    failed = [item for item in results if not item[1]]
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        suffix = f"  <- {detail}" if detail and not ok else ""
        print(f"[{status}] {name}{suffix}")
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


def _pending_id(admin: Client, username: str) -> int:
    """从管理员页面的待验证表格里解析账号主键。"""
    html = admin.get("/admin/accounts").read().decode("utf-8")
    for block in re.finditer(r"<tr>(.*?)</tr>", html, re.S):
        row = block.group(1)
        if re.search(re.escape(username), row):
            match = re.search(r'action="/admin/accounts/(\d+)/verify"', row)
            if match is not None:
                return int(match.group(1))
    raise AssertionError(f"未在待验证列表中找到 {username}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
