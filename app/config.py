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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
