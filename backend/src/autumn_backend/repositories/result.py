"""区分首次受理与幂等重放，避免重复扣额或排队。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Creation[T]:
    record: T
    created: bool
