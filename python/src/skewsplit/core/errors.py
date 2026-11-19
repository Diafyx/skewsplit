"""The error the core raises for invalid input (spec section 9)."""

from __future__ import annotations

from typing import Literal

ErrorCode = Literal[
    "invalid_input",
    "invalid_column",
    "unsupported_type",
    "invalid_partitions",
    "invalid_rows",
    "invalid_value",
    "value_out_of_range",
    "duplicate_value",
    "invalid_histogram",
]


class SpecError(ValueError):
    """Invalid planner input.

    Attributes:
        code: The error code from spec section 9. It is part of the contract.
        message: A description for people. Its wording is not.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        """Create an error with a spec error code and a message."""
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
