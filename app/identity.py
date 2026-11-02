"""Trusted server identity seam; stage11 supplies authentication. No HTTP fallback."""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True)
class AuthenticatedCustomer:
    customer_id: int

    def __post_init__(self):
        if type(self.customer_id) is not int or not 1 <= self.customer_id < 2**63:
            raise ValueError("Invalid authenticated customer")


IdentityResolver = Callable[[Request], AuthenticatedCustomer | None]


def deny_identity(_request: Request) -> None:
    return None
