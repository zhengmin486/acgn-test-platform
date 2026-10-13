"""输入校验模型：测试用例的新建/编辑，API 与表单共用同一套规则。"""

from pydantic import BaseModel, Field, field_validator

ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


class TestCaseCreate(BaseModel):
    """用例定义输入；路径只允许本站本地路径，禁止外部 URL。"""

    name: str = Field(min_length=1, max_length=100)
    method: str = Field(min_length=1, max_length=10)
    path: str = Field(min_length=1, max_length=300)
    expected_status: int = Field(ge=100, le=599)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("用例名称不能为空白")
        return value

    @field_validator("method")
    @classmethod
    def method_allowed(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in ALLOWED_METHODS:
            raise ValueError(f"不支持的请求方法：{value or '(空)'}")
        return value

    @field_validator("path")
    @classmethod
    def path_is_local(cls, value: str) -> str:
        """必须以单个 / 开头的本站路径；拒绝绝对 URL 与协议相对 URL。"""
        value = value.strip()
        if not value.startswith("/") or value.startswith("//") or "://" in value:
            raise ValueError("路径必须是本站路径（以 / 开头），不允许外部 URL")
        if any(ch.isspace() for ch in value):
            raise ValueError("路径不能包含空白字符")
        return value
