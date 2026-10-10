from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Stored and computed as Decimal; sent over JSON as a plain number.
Money = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorBody
