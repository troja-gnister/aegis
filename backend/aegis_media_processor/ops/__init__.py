"""Media operations; they run only inside a job's rlimited tool child (M2)."""

from __future__ import annotations

from ..protocol import Status


class OperationError(Exception):
    """An operation's protocol outcome other than ``ok``; carries only a safe code."""

    def __init__(self, status: Status, code: str) -> None:
        super().__init__(code)
        self.status: Status = status
        self.code = code
