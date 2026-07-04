"""Stateless MCP Streamable-HTTP endpoint: ``POST /api/mcp/``.

One JSON-RPC message per POST, answered with a single ``application/json``
response (the Streamable HTTP spec's allowed alternative to SSE streaming);
notifications get an empty ``202``. No ``Mcp-Session-Id`` is issued — the
server is fully stateless, so any gunicorn worker/replica can serve any
request. ``GET`` is 405: we never open server-initiated streams.

Supported methods: ``initialize``, ``ping``, ``tools/list``, ``tools/call``
(and ``notifications/*`` as no-ops). Auth is a bearer ``McpApiKey``
(``auth.authenticate_request``); the toolset is per-key
(``registry.build_tool_specs``). Tool failures are returned as MCP tool
results with ``isError: true`` — never HTTP 500 — matching the chat agent's
``make_safe_tool`` philosophy of feeding errors back to the model.
"""
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .auth import authenticate_request
from .registry import build_tool_specs

logger = logging.getLogger(__name__)

# Newest first. initialize echoes the client's version when we support it,
# else answers with our default (per the MCP version-negotiation rules).
SUPPORTED_PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
DEFAULT_PROTOCOL_VERSION = '2025-06-18'

SERVER_INFO = {
    'name': 'welcome-data-catalog',
    'title': 'Welcome Data Catalog',
    'version': '0.1.0',
}

SERVER_INSTRUCTIONS = (
    'Data-catalog and analytics tools. ALWAYS call get_catalog_overview '
    'first: it returns the authoritative inventory (PowerBI measures, '
    'reports, tables; dbt models; BigQuery schema) and the default '
    'workspace. Every other tool resolves items by EXACT name taken '
    'verbatim from that listing.'
)


def _rpc_result(msg_id, result: dict) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {'jsonrpc': '2.0', 'id': msg_id, 'error': {'code': code, 'message': message}}


def _tool_text_result(msg_id, text: str, is_error: bool = False) -> dict:
    return _rpc_result(msg_id, {
        'content': [{'type': 'text', 'text': text}],
        'isError': is_error,
    })


def _handle_initialize(msg_id, params) -> dict:
    requested = (params or {}).get('protocolVersion')
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    return _rpc_result(msg_id, {
        'protocolVersion': version,
        'capabilities': {'tools': {'listChanged': False}},
        'serverInfo': SERVER_INFO,
        'instructions': SERVER_INSTRUCTIONS,
    })


def _handle_tools_list(msg_id, key) -> dict:
    tools = []
    for spec in build_tool_specs(key):
        tools.append({
            'name': spec.name,
            'description': spec.description,
            'inputSchema': spec.input_schema,
            # Every registered tool is read-only against our stores (live
            # query tools run SELECT/EVALUATE-guarded queries only).
            'annotations': {'readOnlyHint': True},
        })
    return _rpc_result(msg_id, {'tools': tools})


def _handle_tools_call(msg_id, key, params) -> dict:
    params = params or {}
    name = params.get('name') or ''
    arguments = params.get('arguments') or {}
    if not isinstance(arguments, dict):
        return _rpc_error(msg_id, -32602, 'arguments must be an object')

    spec = next((s for s in build_tool_specs(key) if s.name == name), None)
    if spec is None:
        return _rpc_error(msg_id, -32602, f'Unknown tool: {name}')

    allowed = set(spec.input_schema.get('properties', {}))
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        return _tool_text_result(
            msg_id,
            f'Unexpected argument(s) {", ".join(unexpected)} for tool `{name}`. '
            f'Allowed: {", ".join(sorted(allowed)) or "(none)"}.',
            is_error=True,
        )
    missing = [p for p in spec.input_schema.get('required', []) if p not in arguments]
    if missing:
        return _tool_text_result(
            msg_id,
            f'Missing required argument(s) {", ".join(missing)} for tool `{name}`.',
            is_error=True,
        )

    try:
        result = spec.func(**arguments)
    except Exception as exc:  # tool errors are results, not protocol errors
        logger.exception('MCP tool %s failed', name)
        return _tool_text_result(msg_id, f'Tool `{name}` failed: {exc}', is_error=True)
    return _tool_text_result(msg_id, str(result))


def _dispatch(message, key):
    """Handle one JSON-RPC message. Returns a response dict, or ``None`` for
    notifications (no ``id`` ⇒ nothing to answer)."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _rpc_error(None, -32600, 'Invalid Request')

    method = message.get('method')
    msg_id = message.get('id')
    params = message.get('params')
    is_notification = 'id' not in message

    if not isinstance(method, str):
        return None if is_notification else _rpc_error(msg_id, -32600, 'Invalid Request')

    if method.startswith('notifications/'):
        return None

    handlers = {
        'initialize': lambda: _handle_initialize(msg_id, params),
        'ping': lambda: _rpc_result(msg_id, {}),
        'tools/list': lambda: _handle_tools_list(msg_id, key),
        'tools/call': lambda: _handle_tools_call(msg_id, key, params),
    }
    handler = handlers.get(method)
    if handler is None:
        return None if is_notification else _rpc_error(msg_id, -32601, f'Method not found: {method}')
    response = handler()
    return None if is_notification else response


@csrf_exempt
@require_http_methods(['POST'])
def mcp_endpoint(request):
    key, err = authenticate_request(request)
    if err is not None:
        return err

    try:
        payload = json.loads(request.body or b'')
    except (ValueError, TypeError):
        return JsonResponse(_rpc_error(None, -32700, 'Parse error'), status=400)

    # Single message is the 2025-06-18+ shape; a list is tolerated for
    # older (2025-03-26) batching clients.
    if isinstance(payload, list):
        responses = [r for r in (_dispatch(m, key) for m in payload) if r is not None]
        if not responses:
            return HttpResponse(status=202)
        return JsonResponse(responses, safe=False)

    response = _dispatch(payload, key)
    if response is None:
        return HttpResponse(status=202)
    return JsonResponse(response)
