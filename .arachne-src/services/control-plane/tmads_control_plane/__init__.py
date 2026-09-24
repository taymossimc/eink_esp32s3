"""TMAds brokerage and control-plane service."""

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Import the web application lazily so integration clients stay lightweight."""
    from .main import create_app as factory

    return factory(*args, **kwargs)

__all__ = ["create_app"]
