"""模型仅能建议三种下一步；身份、预算与执行资格不属于输出协议。"""

from typing import Annotated, Literal, Protocol

from pydantic import Field, TypeAdapter, ValidationError

from autumn_backend.agent.tools import Input, ToolCall
from autumn_backend.errors import InvalidInputError
from autumn_backend.services.execution import ModelResult


class ToolPlan(Input):
    kind: Literal["tool"]
    call: ToolCall


class InputPlan(Input):
    kind: Literal["input"]
    prompt: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = Field(default=(), max_length=10)


class ReplyPlan(Input):
    kind: Literal["reply"]
    text: str = Field(min_length=1, max_length=16000)


Plan = Annotated[ToolPlan | InputPlan | ReplyPlan, Field(discriminator="kind")]
PLAN: TypeAdapter[ToolPlan | InputPlan | ReplyPlan] = TypeAdapter(Plan)


def parse_plan(text: str) -> ToolPlan | InputPlan | ReplyPlan:
    if len(text.encode("utf-8")) > 100000:
        raise InvalidInputError("模型输出超过协议大小")
    try:
        return PLAN.validate_json(text)
    except ValidationError as error:
        raise InvalidInputError("模型没有返回允许的下一步协议") from error


class ModelDriver(Protocol):
    provider: str
    model: str

    async def generate(
        self,
        prompt: str,
        *,
        max_output_tokens: int,
        external_idempotency_key: str,
    ) -> ModelResult: ...

    async def cancel(self, *, external_idempotency_key: str) -> None:
        """供应商支持时停止请求；不支持时适配器返回，仍由提交闸门丢弃输出。"""
        ...
