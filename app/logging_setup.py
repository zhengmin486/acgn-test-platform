"""应用日志装配：级别走配置（LOG_LEVEL），格式统一、堆栈只进日志不出站。

约定（开发期优化定稿）：
- 级别由 app.config 的 log_level 控制，默认 INFO；uvicorn 自身的访问日志
  由启动参数（--log-level）管，这里不动；
- 业务代码用 logging.getLogger(__name__) 取子 logger，交给根配置统一处理；
- 异常堆栈只在服务端记录（logger.exception / exc_info=True），
  响应里一律给脱敏后的通用错误页 / JSON（见 main.py 的 500 处理器）；
- 不在循环体内逐条打日志：批量操作的进展汇总成一条（计数 / 采样）再输出。
"""

import logging
import sys

from app.config import settings

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging() -> None:
    """按配置装配根 logger；幂等（重复导入 / 多次调用不叠加 handler）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)

    level_name = (settings.log_level or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):  # 防御：非法取值回退 INFO
        level = logging.INFO
    root.setLevel(level)

    # 让本应用与 uvicorn 的非访问日志跟随同一级别（访问日志保留默认行为）。
    for name in ("uvicorn", "uvicorn.error", "app"):
        logging.getLogger(name).setLevel(level)
