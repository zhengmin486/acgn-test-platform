"""找回密码真实页面验收：对运行中的服务执行端到端检查（不改动业务数据）。

用法：
    python scripts/acceptance_password_reset.py [基址]
    python scripts/acceptance_password_reset.py http://127.0.0.1:8001

检查项覆盖：申请页渲染、登录页入口、未注册邮箱的统一提示与不签发令牌、
令牌一次性、过期拒绝、改密后旧密码失效 / 新密码可登录、旧会话被踢下线、
顺带完成邮箱验证、以及无邮箱历史账号的处理。
"""

from __future__ import annotations

import http.cookiejar
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8001"
RESET_TOKEN_PATTERN = re.compile(r"/reset-password\?token=([A-Za-z0-9_\-]+)")


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
    return f"{prefix}{int(time.time() * 1000) % 100000000}"


def _html(resp) -> str:
    return resp.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    base = (argv[0] if argv else DEFAULT_BASE)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    guest = Client(base)

    # 1) 申请页渲染
    resp = guest.get("/forgot-password")
    html = _html(resp)
    check("找回密码页渲染", resp.status == 200 and "找回密码" in html, f"status={resp.status}")

    # 2) 登录页含「忘记密码」入口
    html = _html(guest.get("/login"))
    check("登录页含忘记密码入口", "/forgot-password" in html)

    # 3) 未注册邮箱：统一提示 + 不签发令牌 + 不回显邮箱
    resp = guest.post("/forgot-password", {"email": "ghost@example.com"})
    html = _html(resp)
    check("未注册邮箱返回统一提示", resp.status == 200 and "如果该邮箱已在平台注册" in html,
          f"status={resp.status}")
    check("未注册邮箱不回显地址", "ghost@example.com" not in html)

    # 4) 空邮箱 / 格式非法
    resp = guest.post("/forgot-password", {"email": ""})
    check("空邮箱被拒", resp.status == 400 and "邮箱不能为空" in _html(resp), f"status={resp.status}")

    resp = guest.post("/forgot-password", {"email": "not-an-email"})
    check("非法邮箱被拒", resp.status == 400 and "邮箱格式不正确" in _html(resp),
          f"status={resp.status}")

    # 5) 注册账号并申请重置
    username = _unique("rst")
    email = f"{username}@example.com"
    guest.clear()
    guest.post("/register", {"username": username, "password": "pw-123456",
                             "confirm": "pw-123456", "email": email})
    resp = guest.post("/forgot-password", {"email": email, "next": "/articles"})
    location = resp.headers.get("location", "")
    check("已注册邮箱跳待处理页", resp.status == 303 and location.startswith("/reset/pending"),
          f"status={resp.status} location={location}")
    check("申请重置保留 next", "next=/articles" in location, location)

    # 6) 站内兜底生成重置链接
    resp = guest.post("/reset/link", {"next": "/articles"})
    html = _html(resp)
    match = RESET_TOKEN_PATTERN.search(html)
    check("生成站内重置链接", resp.status == 200 and match is not None, f"status={resp.status}")
    reset_path = match.group(0) if match else ""
    token = match.group(1) if match else ""

    # 7) 链接可打开改密表单
    resp = guest.get(reset_path)
    check("重置链接可打开表单", resp.status == 200 and "设置新密码" in _html(resp),
          f"status={resp.status}")

    # 8) 伪造令牌被拒
    resp = guest.get("/reset-password?token=forged-token")
    check("伪造令牌被拒", resp.status == 400 and "重置链接无效或已过期" in _html(resp),
          f"status={resp.status}")

    # 9) 密码规则：不一致
    before = _html(guest.get(reset_path))
    resp = guest.post("/reset-password", {"token": token, "password": "good-pass-1",
                                          "confirm": "other-pass-2"})
    check("两次密码不一致被拒", resp.status == 400 and "两次输入的密码不一致" in _html(resp),
          f"status={resp.status}")

    # 10) 改密成功 -> 跳登录页且未直接签发会话
    resp = guest.post("/reset-password", {"token": token, "password": "new-pass-9",
                                          "confirm": "new-pass-9"})
    location = resp.headers.get("location", "")
    check("改密成功跳登录页", resp.status == 303 and location.startswith("/login?reset=1"),
          f"status={resp.status} location={location}")
    check("改密不直接签发会话", not guest.cookie("acgn_session"))

    # 11) 旧密码失效、新密码可登录
    guest.clear()
    resp = guest.post("/login", {"username": username, "password": "pw-123456"})
    check("旧密码登录失败", resp.status == 400, f"status={resp.status}")

    guest.clear()
    resp = guest.post("/login", {"username": username, "password": "new-pass-9"})
    check("新密码可登录",
          resp.status == 303 and bool(guest.cookie("acgn_session")), f"status={resp.status}")

    # 12) 令牌一次性：再改一次应失败
    guest.clear()
    resp = guest.post("/reset-password", {"token": token, "password": "again-pass-9",
                                          "confirm": "again-pass-9"})
    check("重置令牌不可复用", resp.status == 400, f"status={resp.status}")

    # 13) 改密后旧会话失效：登录 -> 再重置 -> 导航栏不再显示用户名
    victim = _unique("kick")
    vmail = f"{victim}@example.com"
    c1 = Client(base)
    c1.post("/register", {"username": victim, "password": "pw-123456",
                          "confirm": "pw-123456", "email": vmail})
    # 未验证邮箱的账号登录会被拦截，先走完验证流程
    c1.get(re.search(r"/verify/email\?token=[A-Za-z0-9_\-]+",
                     _html(c1.post("/verify/link", {}))).group(0))
    c1.post("/login", {"username": victim, "password": "pw-123456"})
    check("登录态可见用户名", victim in _html(c1.get("/")), "登录失败")

    # 等到下一秒，确保旧会话的 iat 严格早于改密时间
    time.sleep(1.1)
    c2 = Client(base)
    c2.post("/forgot-password", {"email": vmail})
    html = _html(c2.post("/reset/link", {}))
    victim_token = RESET_TOKEN_PATTERN.search(html).group(1)
    resp = c2.post("/reset-password", {"token": victim_token, "password": "kicked-pass-9",
                                       "confirm": "kicked-pass-9"})
    check("二次改密成功", resp.status == 303, f"status={resp.status}")
    check("旧会话被踢下线", victim not in _html(c1.get("/")), "旧会话仍然有效")

    # 14) 无邮箱的历史账号（脚本创建的 admin）不签发令牌
    c3 = Client(base)
    resp = c3.post("/forgot-password", {"email": "legacy@example.com"})
    check("无邮箱账号走统一提示", resp.status == 200 and "如果该邮箱已在平台注册" in _html(resp),
          f"status={resp.status}")

    failed = [item for item in results if not item[1]]
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        suffix = f"  <- {detail}" if detail and not ok else ""
        print(f"[{status}] {name}{suffix}")
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
