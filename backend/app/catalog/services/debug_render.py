"""Format the per-message debug section that gets appended to chatbot answers
when ``Organization.debug_responses_enabled`` is on.

The renderer also produces the structured payload persisted to
``ChatMessage.debug_meta`` regardless of the toggle, so we always have an
audit trail of which DAX/SQL the bot ran and which catalog searches it tried.
"""
from __future__ import annotations

from typing import Iterable


# Tools whose ``args.dax_query`` should be surfaced as a code block.
_DAX_TOOLS = {'powerbi_run_dax_query'}

# Tools whose ``args.sql`` should be surfaced as a code block. Keep in sync
# with the BigQuery tool factory in ``bigquery_tools.py``.
_BIGQUERY_SQL_TOOLS = {'bigquery_execute_query'}


def build_debug_payload(tool_calls: list[dict]) -> dict:
    """Return the JSON-serialisable structure persisted to ``debug_meta``.

    Splits raw DAX/SQL out of the call list so consumers (UI, audit) can read
    queries without re-parsing tool args.
    """
    dax_queries: list[dict] = []
    sql_queries: list[dict] = []
    total_ms = 0
    error_count = 0

    for call in tool_calls:
        args = (call.get('args') or {}) | (call.get('kwargs') or {})
        total_ms += int(call.get('duration_ms') or 0)
        if call.get('status') == 'error':
            error_count += 1
        if call.get('tool') in _DAX_TOOLS and args.get('dax_query'):
            dax_queries.append({
                'dataset_id': args.get('dataset_id', ''),
                'workspace_id': args.get('workspace_id', ''),
                'dax_query': args['dax_query'],
                'duration_ms': call.get('duration_ms'),
                'status': call.get('status'),
            })
        elif call.get('tool') in _BIGQUERY_SQL_TOOLS and args.get('sql'):
            sql_queries.append({
                'sql': args['sql'],
                'duration_ms': call.get('duration_ms'),
                'status': call.get('status'),
            })

    return {
        'tool_calls': list(tool_calls),
        'dax_queries': dax_queries,
        'sql_queries': sql_queries,
        'stats': {
            'tool_call_count': len(tool_calls),
            'dax_query_count': len(dax_queries),
            'sql_query_count': len(sql_queries),
            'total_duration_ms': total_ms,
            'error_count': error_count,
        },
    }


# Truncated args fields for the call table. Long DAX / SQL belong in the code
# blocks below, not in the per-call args column.
_TRUNCATED_FIELDS = {'dax_query', 'sql', 'description'}
_TRUNCATE_AT = 80


def _format_args(call: dict) -> str:
    args = (call.get('args') or {}) | (call.get('kwargs') or {})
    if not args:
        return '—'
    parts = []
    for k, v in args.items():
        if v in (None, ''):
            continue
        if k in _TRUNCATED_FIELDS and isinstance(v, str) and len(v) > _TRUNCATE_AT:
            v = v[:_TRUNCATE_AT].rstrip() + '…'
        # Markdown table cells: pipes break the row, newlines collapse it.
        s = str(v).replace('|', '\\|').replace('\n', ' ')
        parts.append(f'{k}={s}')
    return '; '.join(parts) if parts else '—'


def render_debug_section(payload: dict) -> str:
    """Render the debug payload as a markdown block ready to append to a
    chatbot answer. Returns an empty string when there are no recorded calls
    (avoids littering replies that didn't trigger any tools)."""
    calls: Iterable[dict] = payload.get('tool_calls') or []
    if not calls:
        return ''

    lines: list[str] = ['\n\n---\n', '**🔧 Debug**', '']

    lines.append('| # | Tool | Args | Duration | Status |')
    lines.append('| --- | --- | --- | --- | --- |')
    for i, call in enumerate(calls, start=1):
        tool = call.get('tool', '?')
        duration = call.get('duration_ms')
        duration_s = f'{duration} ms' if duration is not None else '—'
        status = call.get('status', '?')
        if status == 'error' and call.get('error'):
            status = f'error: {call["error"]}'
        lines.append(f'| {i} | `{tool}` | {_format_args(call)} | {duration_s} | {status} |')

    for q in payload.get('dax_queries') or []:
        lines.append('')
        scope_bits = []
        if q.get('dataset_id'):
            scope_bits.append(f"dataset={q['dataset_id']}")
        if q.get('workspace_id'):
            scope_bits.append(f"workspace={q['workspace_id']}")
        scope = f' ({"; ".join(scope_bits)})' if scope_bits else ''
        lines.append(f'**DAX executed**{scope}:')
        lines.append('```dax')
        lines.append(q['dax_query'])
        lines.append('```')

    for q in payload.get('sql_queries') or []:
        lines.append('')
        lines.append('**BigQuery SQL executed:**')
        lines.append('```sql')
        lines.append(q['sql'])
        lines.append('```')

    stats = payload.get('stats') or {}
    if stats:
        lines.append('')
        bits = [
            f"{stats.get('tool_call_count', 0)} tool calls",
            f"{stats.get('dax_query_count', 0)} DAX",
            f"{stats.get('sql_query_count', 0)} BigQuery",
            f"{stats.get('total_duration_ms', 0)} ms total",
        ]
        if stats.get('error_count'):
            bits.append(f"{stats['error_count']} error(s)")
        lines.append('**Stats:** ' + ' · '.join(bits))

    return '\n'.join(lines)
