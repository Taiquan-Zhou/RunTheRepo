from typing import Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

ModelT = TypeVar("ModelT", bound=BaseModel)


class ModelAdapter(Protocol):
    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT: ...


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: str
    params: dict[str, str | int]
    reason: str
