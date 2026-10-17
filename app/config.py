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

    # ---------- 封面图 ----------
    # 封面本地存储目录：Bangumi 导入时下载到此，Content.image_url 存站内
    # 相对地址（/covers/...）。默认在项目内；想放到其它盘符（如 D:/acgn-covers）
    # 改成绝对路径即可，main.py 会原样挂载到 /covers。
    cover_storage_dir: str = "app/static/covers"
    # 导入时是否下载封面；置 False 则只记录远程 URL（不推荐，热链易失效）。
    cover_download_enabled: bool = True

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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
