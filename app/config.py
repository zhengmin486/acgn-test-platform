"""应用配置：从环境变量 / .env 读取，密码等敏感信息不写入代码。

DATABASE_URL 取值示例：
- MySQL（目标生产配置）：
  mysql+pymysql://acgn_app:密码@127.0.0.1:3306/acgn_test_platform?charset=utf8mb4
- SQLite（仅本地开发与自动化验证，不是最终生产方案）：
  sqlite:///./local_dev.db
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./local_dev.db"

    # ---------- 认证与访问控制 ----------
    # 会话 Cookie 的签名密钥；必须通过 .env 覆盖后再部署（默认值仅供本地开发）。
    auth_secret_key: str = "dev-only-insecure-secret-change-in-env"
    # 会话有效期（秒），默认 8 小时。
    session_max_age_seconds: int = 8 * 60 * 60
    # users 表为空时创建的初始账号（仅首次初始化生效，不覆盖已有用户）。
    # 生产环境必须通过 .env 覆盖默认口令。
    default_admin_username: str = "admin"
    default_admin_password: str = "admin123456"
    default_user_username: str = "demo"
    default_user_password: str = "demo123456"

    # ---------- 邮箱验证（可插拔） ----------
    # 总开关：置为 False 后注册即登录、登录不再校验邮箱，行为完全退回加该功能之前。
    email_verification_enabled: bool = True
    # 验证令牌有效期（秒），默认 24 小时。
    email_verification_token_ttl_seconds: int = 24 * 60 * 60
    # 找回密码令牌有效期（秒），默认 1 小时——重置链接权限更高，有效期刻意
    # 比邮箱验证短很多。
    password_reset_token_ttl_seconds: int = 60 * 60
    # 同一账号两次发送验证邮件之间的冷却时间（秒），防止被拿来轰炸邮箱。
    email_resend_cooldown_seconds: int = 60
    # 站点对外基址（如 https://acgn.example.com）；留空时取当前请求的 host。
    public_base_url: str = ""

    # ---------- 注册控制 ----------
    # 注册模式：
    # - invite（默认）：关闭公开注册，/register 必须提供有效邀请码
    #   （管理员在 /admin/invites 生成，或 scripts/create_invite.py 批量生成）；
    # - open：任何人可自助注册（加邀请码功能之前的旧行为）。
    # 任何非法取值都按 invite 处理（安全默认），只有显式 open 才放开发布。
    registration_mode: str = "invite"

    # ---------- 阿宅画像（Bangumi 公开收藏 + 可选 LLM） ----------
    # Bangumi API 基址，一般不用改。
    bangumi_api_base: str = "https://api.bgm.tv"
    # Bangumi 要求可识别的 User-Agent；这里带上项目标识。
    bangumi_user_agent: str = "acgn-persona/0.1 (local dev)"
    # 出网代理（如 http://127.0.0.1:9910）；留空则用系统默认（含环境变量代理）。
    bangumi_proxy: str = ""
    # 单次同步最多拉取多少条收藏，避免一次请求过久。
    bangumi_max_items: int = 1000
    # 单次「同步到本站」最多新建多少部作品（收藏上千条时防止一次点击写爆作品库）；
    # 达到上限后页面会提示「可再点一次继续」。管理员 / 站长用下面更大的上限。
    persona_site_sync_limit: int = 200
    persona_site_sync_limit_staff: int = 1000
    # 免登录试算（引流用）：最多拉多少条收藏、同一 IP 多少秒内只允许一次。
    # 试算结果不落库，只渲染当次页面；登录后才保存快照与报告。
    persona_guest_max_items: int = 200
    persona_guest_cooldown_seconds: int = 20
    # 详情页按需补全制作 / 声优 / 客观信息（2 次 Bangumi 请求）；失败静默，不阻塞页面。
    # 批量补全用 scripts/backfill_meta.py，产物一致。
    bangumi_meta_on_demand: bool = True
    # 补全结果的缓存有效期（天）：同一部作品超过这个时间再次访问才重拉。
    bangumi_meta_ttl_days: int = 30
    # Bangumi 请求超时（秒）。
    bangumi_timeout: float = 15.0
    # 可选：LLM 文案生成。国内主流三家都兼容 OpenAI 的 /chat/completions 协议，
    # 只填 PERSONA_LLM_PROVIDER + PERSONA_LLM_API_KEY 即可，地址与模型按预设自动补：
    #   deepseek → https://api.deepseek.com                 / deepseek-chat
    #   doubao   → https://ark.cn-beijing.volces.com/api/v3 / 需填推理接入点 ID(ep-…) 或模型名
    #   glm      → https://open.bigmodel.cn/api/paas/v4     / glm-4-flash
    # 显式填的 BASE_URL / MODEL 优先，因此也能指向任意兼容服务；三项配齐才启用「直接生成」，
    # 否则页面只提供「复制提示词 → 千问 / 豆包 / DeepSeek 网页版 → 贴回」的手动模式。
    persona_llm_provider: str = ""
    persona_llm_base_url: str = ""
    persona_llm_api_key: str = ""
    persona_llm_model: str = ""
    # 单次生成的输出上限；报告较长，给足 token 避免被截断（0 表示不传，用服务端默认）。
    persona_llm_max_tokens: int = 2000

    # ---------- 开发种子数据 ----------
    # contents 为空时是否写入开发假数据（app/fake_data.py）。
    # 开发/测试保持 True；上线置 False 用干净库，再经后台「数据同步」灌真实数据。
    seed_demo_data: bool = True

    # ---------- 数据库备份（scripts/backup_db.py） ----------
    # 备份输出目录（相对项目根或绝对路径）与保留份数（0 表示不清理）。
    backup_dir: str = "backups"
    backup_retain: int = 14

    # ---------- 增量入库通道（app/ingest_api.py） ----------
    # 服务器无境外出网，词条由本机抓取后 POST 回服务器入库；请求头 X-Ingest-Key
    # 比对这个值。留空 = 端点一律 503（默认关闭），配了才启用。
    # 只放生产 .env（chmod 600），不要提交进仓库。
    ingest_api_key: str = ""

    # ---------- 封面图 ----------
    # 封面本地存储目录：Bangumi 导入时下载到此，Content.image_url 存站内
    # 相对地址（/covers/...）。默认在项目内；想放到其它盘符（如 D:/acgn-covers）
    # 改成绝对路径即可，main.py 会原样挂载到 /covers。
    cover_storage_dir: str = "app/static/covers"
    # 导入时是否下载封面；置 False 则只记录远程 URL（不推荐，热链易失效）。
    cover_download_enabled: bool = True

    # ---------- 用户头像 ----------
    # 头像文件目录（挂到 /avatars）；默认在项目内，也可指到其它盘符。
    avatar_storage_dir: str = "app/static/avatars"
    # 上传原图大小上限：头像会被裁成 256×256 WebP，2MB 足够。
    avatar_upload_max_bytes: int = 2 * 1024 * 1024

    # ---------- 封面图片处理（app/media.py） ----------
    # 列表缩略图宽度（px）：只缩不放，输出 WebP；列表页流量主要靠它省。
    cover_thumb_width: int = 400
    # 详情全图双约束：宽度 px 与字节数；超出边缩边降画质直到达标，
    # 保底也接受最小画质——封面可见性优先于体积达标。
    cover_full_max_width: int = 1200
    cover_full_max_bytes: int = 400 * 1024
    # 管理员上传封面的大小上限（字节）；只认魔术字节，不信客户端扩展名。
    cover_upload_max_bytes: int = 5 * 1024 * 1024
    # 图片对外基址（如 https://img.example.com）：留空表示图片与应用同源。
    # 将来切 COS / CDN 只改这一项，模板里的 /media/... 自动带上前缀。
    media_base_url: str = ""

    # ---------- 邮件发送（SMTP，全部留空则不真发） ----------
    # smtp_host 与 smtp_from_address 都为空 = 不发送真实邮件：
    # 验证链接写入日志，并可在待验证页面 / 管理员页面手动生成，保证本地无邮件服务也能走完流程。
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # 465 端口用 smtp_use_ssl=True；587 端口用 smtp_use_tls=True（STARTTLS）。
    smtp_use_ssl: bool = False
    smtp_use_tls: bool = True
    smtp_from_address: str = ""
    smtp_from_name: str = "ACGN 平台"

    @property
    def registration_invite_only(self) -> bool:
        """是否处于「仅邀请码注册」模式。

        只有显式配置为 open 才放开发布；任何其它取值（含拼错、留空）
        都按 invite 处理——公开注册是敏感开关，默认必须落在收紧一侧。
        """
        return self.registration_mode.strip().lower() != "open"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
