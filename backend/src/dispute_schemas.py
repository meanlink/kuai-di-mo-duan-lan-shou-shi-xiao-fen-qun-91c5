from typing import Literal

from pydantic import Field

from .schemas import Input


class DisputeDecision(Input):
    decision: Literal['upheld', 'reassigned']
    note: str = Field(min_length=12, max_length=400)
