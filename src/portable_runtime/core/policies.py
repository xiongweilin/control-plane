"""Policy extension point for future approval and side-effect rules."""

from typing import Protocol


class Policy(Protocol):
    def allows(self, action: str, context: dict[str, object]) -> bool: ...

