"""Invisible organization scoping for catalog read tools.

Chat and MCP tool schemas must describe only the arguments an LLM/client can
choose.  The authenticated organization is therefore captured by a wrapper and
stored in a ``ContextVar`` for the duration of the call instead of being exposed
as an ``organization_id`` tool argument.
"""
from __future__ import annotations

import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional


class OrganizationScopeRequired(RuntimeError):
    """Raised before a catalog read when no authenticated org was bound."""


_organization_id: ContextVar[Optional[int]] = ContextVar(
    'catalog_read_organization_id', default=None,
)


def _organization_pk(organization) -> Optional[int]:
    value = getattr(organization, 'pk', None)
    if value is None:
        value = getattr(organization, 'id', None)
    return value


def get_bound_organization_id() -> Optional[int]:
    """Return the current bound organization id, if this is a scoped call."""
    return _organization_id.get()


def require_bound_organization_id() -> int:
    """Return the current org id or fail before any tenant-owned query runs."""
    organization_id = get_bound_organization_id()
    if organization_id is None:
        raise OrganizationScopeRequired(
            'An authenticated organization scope is required for this catalog tool.'
        )
    return organization_id


@contextmanager
def organization_read_scope(organization_id: Optional[int]) -> Iterator[None]:
    """Bind one organization for a synchronous read-tool invocation."""
    token = _organization_id.set(organization_id)
    try:
        yield
    finally:
        _organization_id.reset(token)


def bind_organization_read_tool(func: Callable, organization) -> Callable:
    """Capture ``organization`` without changing ``func``'s public signature.

    ``functools.wraps`` preserves the name, annotations, documentation and
    introspected signature.  Consequently neither Pydantic AI nor MCP exposes a
    caller-controlled tenant argument.
    """
    organization_id = _organization_pk(organization)

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_scoped(*args, **kwargs):
            with organization_read_scope(organization_id):
                return await func(*args, **kwargs)

        return async_scoped

    @functools.wraps(func)
    def scoped(*args, **kwargs):
        with organization_read_scope(organization_id):
            return func(*args, **kwargs)

    return scoped
