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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
