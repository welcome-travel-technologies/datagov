"""
``make_safe_tool`` — wraps a tool function so the LLM gets a structured
``{'status': 'success' | 'error', ...}`` dict instead of a raw exception,
and so the chat view can hook ``before_call`` / ``record_call`` for
real-time status updates and per-turn debug telemetry.
"""
import functools
import inspect
import time
from typing import Callable, Optional

from asgiref.sync import sync_to_async


def make_safe_tool(
    func: Callable,
    before_call: Optional[Callable] = None,
    record_call: Optional[Callable] = None,
    guard: Optional[Callable] = None,
) -> Callable:
    """
    Wraps a tool function to catch exceptions and return them as a structured
    string. This helps the LLM distinguish an execution error from a valid result.

    If ``before_call`` is provided it is invoked with the raw function name and
    the tool's positional/keyword arguments just before it executes:
        before_call(func_name: str, args: tuple, kwargs: dict)

    If ``record_call`` is provided it is invoked AFTER the tool returns with a
    structured entry suitable for persisting as debug metadata:
        record_call({
            'tool': func_name,
            'args': dict,            # positional args mapped to param names
            'kwargs': dict,
            'duration_ms': int,
            'status': 'success' | 'error',
            'error': str | None,     # populated only on error
        })

    If ``guard`` is provided it is consulted BEFORE executing the tool:
        guard(func_name: str, args: tuple, kwargs: dict) -> str | None
    When it returns a non-empty string the tool body is SKIPPED and that string
    is returned to the model as the tool result (a loop-breaker directive). Used
    to deterministically cap runaway repeat calls the prompt rules can't stop.
    """
    try:
        param_names = list(inspect.signature(func).parameters.keys())
    except (TypeError, ValueError):
        param_names = []

    def _blocked(args, kwargs) -> Optional[str]:
        if guard is None:
            return None
        try:
            return guard(func.__name__, args, kwargs)
        except Exception:
            return None

    def _record(payload: dict) -> None:
        if record_call is not None:
            try:
                record_call(payload)
            except Exception:
                pass

    def _before(*args, **kwargs) -> None:
        if before_call is not None:
            try:
                before_call(func.__name__, args, kwargs)
            except Exception:
                pass  # never let a status update kill the tool

    async def _record_async(payload: dict) -> None:
        if record_call is not None:
            try:
                await sync_to_async(record_call)(payload)
            except Exception:
                pass

    async def _before_async(*args, **kwargs) -> None:
        if before_call is not None:
            try:
                await sync_to_async(before_call)(func.__name__, args, kwargs)
            except Exception:
                pass  # never let a status update kill the tool

    def _payload(status: str, args, kwargs, duration_ms: int, error) -> dict:
        return {
            'tool': func.__name__,
            'args': dict(zip(param_names, args)),
            'kwargs': dict(kwargs),
            'duration_ms': duration_ms,
            'status': status,
            'error': error,
        }

    def _finalize(wrapped: Callable) -> Callable:
        wrapped.__name__ = f'safe_{func.__name__}'
        wrapped.__annotations__ = func.__annotations__.copy()
        wrapped.__annotations__['return'] = dict
        return wrapped

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> dict:
            await _before_async(*args, **kwargs)
            blocked = _blocked(args, kwargs)
            if blocked:
                await _record_async(_payload('success', args, kwargs, 0, None))
                return {'status': 'success', 'data': blocked}
            start = time.monotonic()
            try:
                data = await func(*args, **kwargs)
                duration_ms = int((time.monotonic() - start) * 1000)
                await _record_async(_payload('success', args, kwargs, duration_ms, None))
                return {'status': 'success', 'data': data}
            except Exception as e:
                duration_ms = int((time.monotonic() - start) * 1000)
                await _record_async(
                    _payload('error', args, kwargs, duration_ms, f'{type(e).__name__}: {e}'))
                return {
                    'status': 'error',
                    'error_type': type(e).__name__,
                    'message': str(e),
                }

        return _finalize(async_wrapper)

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> dict:
        _before(*args, **kwargs)
        blocked = _blocked(args, kwargs)
        if blocked:
            _record(_payload('success', args, kwargs, 0, None))
            return {'status': 'success', 'data': blocked}
        start = time.monotonic()
        try:
            data = func(*args, **kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)
            _record(_payload('success', args, kwargs, duration_ms, None))
            return {'status': 'success', 'data': data}
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            _record(_payload('error', args, kwargs, duration_ms, f'{type(e).__name__}: {e}'))
            return {'status': 'error', 'error_type': type(e).__name__, 'message': str(e)}

    return _finalize(wrapper)
