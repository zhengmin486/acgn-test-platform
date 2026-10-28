# 给合作方的回复（OAuth 风格单点登录接口）

> 状态：**三点调整均已获对方接受，接口已按定稿实现并通过测试。** 本文件即最终契约 + 联调配置，可直接发对方。
> 联调前对方提供的三项（client_id / redirect_uri / client_secret 临时值）已落到本文末尾「联调配置（临时值）」。

---

主题：关于「网站账号登录 App」接口——三点调整已确认，附最终契约与联调配置

感谢确认。三点调整按你方接受态落地，现就两个非阻塞小问题一并答复，并给出联调所需配置。

**1. 令牌端点增加 `client_secret`**（已接受）
令牌端点必须同时带 `client_id` + `client_secret`，secret 仅你们后端持有、永不进浏览器与任何客户端、不入库，服务端恒定时间比对。与「密码不经过平台」同一道防线的补齐。

**2. 不做用户同意页**（已接受）
授权由我们站点单方面决定：用户登录后自动签发票据并回跳，全程无感知，无「是否授权」中间页。

**3. 撤销入口在管理员侧**（已接受）+ **新增自助吊销（self-revoke）**
- 站点管理员可应急吊销（按 token 或 external_user_id 全量吊销）；
- 同时我们开放**自助吊销**：持有效 Bearer 令牌的一方（即你们 App 后端，在用户于 App 内点「断开与网站的连接」时触发）可撤销「自己的」令牌——吊销该持有者名下、同一 client 的全部未令牌。这仍不暴露任何终端用户可见的站内 UI，端点由我们掌控。这样既满足「用户可断开」的体验，又由站点兜底应急吊销。

---

## 两个非阻塞小问题的答复

**Q1 续期（无 refresh token）**
我们当前不提供 refresh_token。你们采用「过期前静默重走授权」续期完全可行：`GET /oauth/authorize` 对已登录用户是幂等的——每次直接签发新一次性票据并回跳，无同意页、用户无感知，成本是一次浏览器跳转。若今后我们增加 refresh_token，会另行告知，届时你们可减少跳转。

**Q2 自助吊销（self-revoke）**
支持，见上面第 3 点。用户在你 App 内点「断开与网站的连接」→ 你们带当前 Bearer 调 `POST /api/auth/revoke`（无需管理员会话）→ 该用户在本站的全部令牌立即失效。若你们不调用它，按你们说的本地解绑处理即可，二者不冲突。

**使用边界确认（已读，无异议）**
- `nickname` / `avatar_url` 仅用于你们客户端内展示，不做回写、不做再分发；
- `GET /api/me` 调用频率与令牌续期绑定，量级可控，不增加我们服务器压力。

---

## 最终接口契约

- **GET /oauth/authorize?client_id&redirect_uri&state**
  浏览器 / WebView 打开；未登录先走我们登录页，登录后自动回跳 `redirect_uri?ticket&state`。
  `redirect_uri` 必须在白名单内（我们配置），`state` 必填（防 CSRF）。

- **POST /api/auth/token**（`application/x-www-form-urlencoded`）
  入参：`ticket`、`client_id`、`client_secret`
  出参：`{ access_token, token_type:"Bearer", expires_in:3600, user:{ external_user_id, nickname, avatar_url } }`
  `external_user_id` = 我们站点的用户主键（稳定不变，适合做账号映射与收藏迁移）；`nickname` 取站点展示手柄。

- **GET /api/me**（`Header: Authorization: Bearer <access_token>`）
  出参：`{ external_user_id, nickname, avatar_url, scope:"identity" }`

- **POST /api/auth/revoke**
  - 自助吊销：带 `Authorization: Bearer <access_token>`（无需登录管理员），撤销该令牌持有者名下、同一 client 的全部未吊销令牌；
  - 管理员应急吊销：需站点管理员会话，入参 `access_token` 或 `external_user_id`，撤销后相关令牌立即失效。

## 安全边界（明确你们拿不到的）

- **用户密码**：全程只在我们站点登录页输入，你们后端不可见；
- **业务写权限**：令牌 `scope` 固定 `identity`，只能读用户资料，不能写任何业务数据（收藏 / 状态读写是二期，需我们再次授权并扩展 scope）；
- **令牌只存哈希**、**票据一次性 + 120 秒**、**令牌 1 小时过期且可撤销**。

---

## 联调配置（临时值，仅用于集成测试）

> ⚠️ 以下为「联调用临时值」，正式上线前必须替换为高强度随机值并单独注入目标服务器，不要沿用本文件的临时 secret。

| 项 | 值 |
|---|---|
| `client_id` | `otaku-app-client`（由我们分配，以本白名单为准） |
| `client_secret`（临时） | `OldkLhxaMWWQPKD_Ji8DD_tbIRYKoID7mXxfqyPih1M` |
| `redirect_uri` 白名单 | `com.otaku.app:/oauth2redirect`（移动端自定义 scheme，Android / iOS） |
| 桌面端 loopback | 待你们客户端模块定下端口后报准确值，再追加到白名单，例如 `http://127.0.0.1:<port>/callback` |
| 票据有效期 | 120 秒（一次性） |
| 令牌有效期 | 3600 秒（1 小时） |

若你们偏好联调只收一个回跳地址，先收上面的移动端 scheme 即可；桌面端 loopback 端口确定后补报。

---

联调量级很小，不会增加我们服务器压力，也不需要你们长期维护。配置就位后即可约联调。
