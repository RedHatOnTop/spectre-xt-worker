"""Authoritative worker-state resolver. Consumers ask the API; they do not classify."""

from .types import RESOLVER_VERSION, SCHEMA_VERSION

__all__ = ["RESOLVER_VERSION", "SCHEMA_VERSION"]
