"""
PowerBI tool functions for the Pydantic AI chatbot agent.

HOW IT WORKS
------------
Each tool is a plain Python function whose docstring becomes the LLM's
"when to use this tool" instruction. All tools are closures that capture a
pre-authenticated ``PowerBIClient`` instance — the LLM never sees credentials.

ACTIVE vs INACTIVE TOOLS
-------------------------
``make_powerbi_tools(client)`` returns only the *active* tools.
All remaining tools are fully implemented and documented below; to enable one,
simply move it into the ``active_tools`` list in the factory at the bottom.

NATURAL CHAINING
----------------
The agent typically chains catalog tools → PowerBI live tools automatically:
  1. ``get_pb_item_details("Revenue")``      → gets DAX expression + dataset_id
  2. ``powerbi_run_dax_query(...)``        → executes the expression live
This way users can ask "what is the current value of [measure]?" and the agent
resolves both the expression and the live result in a single turn.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import List

from .powerbi_client import PowerBIClient, PowerBIRequestError

logger = logging.getLogger(__name__)

_DAX_PLACEHOLDER_RE = re.compile(r"DateTable|MeasureName|Table\'\[Col\]|\[MeasureName\]", re.IGNORECASE)
_DAX_DATE_LITERAL_RE = re.compile(r"\bDATE\s*\(\s*(\d{4})\s*,", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Result formatting helpers
# ---------------------------------------------------------------------------

def _fmt_rows(rows: list, max_rows: int = 50) -> str:
    """Convert a list of DAX result row dicts to a compact Markdown table.

    The returned string begins with a metadata line so the LLM always has the
    row count available when composing its response.
    """
    if not rows:
        return 'Rows returned: 0\n\nNo rows returned.'
    cols = list(rows[0].keys())
    # Strip the bracketed prefix that DAX adds, e.g. "[RowCount]" -> "RowCount"
    clean = [c.lstrip('[').rstrip(']') for c in cols]
    header = ' | '.join(clean)
    sep = ' | '.join(['---'] * len(cols))
    data_rows = []
    for row in rows[:max_rows]:
        data_rows.append(' | '.join(str(row.get(c, '')) for c in cols))
    table = f'| {header} |\n| {sep} |\n' + '\n'.join(f'| {r} |' for r in data_rows)
    truncation_note = ''
    if len(rows) > max_rows:
        truncation_note = f'\n\n*Showing first {max_rows} of {len(rows)} rows.*'
    # Prepend metadata so the LLM can surface stats without extra tool calls
    meta = f'Rows returned: {len(rows)}'
    return f'{meta}\n\n{table}{truncation_note}'


def _fmt_list(items: list, name_key: str = 'name', id_key: str = 'id') -> str:
    """Format a list of API objects as a Markdown table with name + id."""
    if not items:
        return 'No items found.'
    lines = ['| Name | ID |', '| --- | --- |']
    for item in items:
        name = item.get(name_key) or item.get('displayName', '—')
        item_id = item.get(id_key, '—')
        lines.append(f'| {name} | `{item_id}` |')
    return '\n'.join(lines)


# ===========================================================================
# ██████████  ACTIVE TOOLS  ████████████████████████████████████████████████
# ===========================================================================

def _make_run_dax_query(client: PowerBIClient):
    def powerbi_run_dax_query(
        dataset_id: str,
        dax_query: str,
        workspace_id: str = '',
    ) -> str:
        """
        Executes a DAX query against a live PowerBI dataset and returns the results.

        Use this WHEN:
        - The user wants the *current live value* of a measure (e.g. "what is Total Revenue right now?").
        - You already have the DAX expression from ``get_pb_item_details`` and need to evaluate it.
        - The user asks to run a custom DAX calculation or aggregation.

        ``dataset_id``: the PowerBI dataset ID (available from ``get_pb_item_details``
        output or from ``powerbi_list_datasets``).
        ``dax_query``: a valid DAX query starting with EVALUATE, e.g.
            ``EVALUATE ROW("Revenue", [Total Revenue])``
        ``workspace_id``: optional — leave blank to use the default workspace.

        IMPORTANT: Always wrap a measure reference in EVALUATE ROW(...) syntax
        before passing it as dax_query. Never pass a raw DAX expression directly.
        """
        dax_query = (dax_query or '').strip()
        if not dax_query:
            return 'DAX query rejected: dax_query is required.'
        if not dax_query.upper().startswith('EVALUATE'):
            return (
                'DAX query rejected: query must start with EVALUATE. '
                'For a scalar measure use: EVALUATE ROW("Result", [Measure Name])'
            )
        if _DAX_PLACEHOLDER_RE.search(dax_query):
            return (
                'DAX query rejected: query still contains placeholder names. '
                'Resolve the exact measure and dimension/date DAX references from '
                'get_pb_measure_schema before executing.'
            )
        if dax_query.count(';') > 1 or (dax_query.endswith(';') and ';' in dax_query[:-1]):
            return 'DAX query rejected: only one DAX statement is allowed.'

        current_year = datetime.now(timezone.utc).year
        valid_years = {current_year - 1, current_year, current_year + 1}
        for match in _DAX_DATE_LITERAL_RE.finditer(dax_query):
            year = int(match.group(1))
            if year not in valid_years:
                return (
                    f'DAX query rejected: DATE({year},...) uses a year outside the '
                    f'valid range {sorted(valid_years)}. The current year is '
                    f'{current_year}. Re-read the CURRENT DATE CONTEXT block in your '
                    'system prompt and copy the ISO dates verbatim — do not invent '
                    'a year from training data.'
                )

        ws = workspace_id.strip() or None
        try:
            result = client.execute_dax_query(dataset_id, dax_query, workspace_id=ws)
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except Exception as exc:
            return f'Unexpected error executing DAX query: {exc}'

        try:
            results_list = result.get('results', [])
            if not results_list:
                return 'The query returned no results.'
            rows = results_list[0].get('tables', [{}])[0].get('rows', [])
            return _fmt_rows(rows)
        except (KeyError, IndexError, TypeError):
            return f'Result received but could not be parsed:\n```json\n{json.dumps(result, indent=2)}\n```'

    return powerbi_run_dax_query


def _make_get_refresh_history(client: PowerBIClient):
    def powerbi_get_refresh_history(
        dataset_id: str,
        workspace_id: str = '',
        top: int = 5,
    ) -> str:
        """
        Returns the recent refresh history for a PowerBI dataset.

        Use this WHEN the user asks when a dataset was last refreshed, what the
        refresh status is, or whether a refresh succeeded or failed.

        ``dataset_id``: the PowerBI dataset ID.
        ``workspace_id``: optional workspace ID.
        ``top``: number of recent refresh entries to return (default 5).
        """
        ws = workspace_id.strip() or None
        try:
            history = client.get_refresh_history(dataset_id, workspace_id=ws, top=top)
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

        if not history:
            return f'No refresh history found for dataset `{dataset_id}`.'

        lines = [
            f'**Refresh history for dataset `{dataset_id}`** (last {len(history)} entries)\n',
            '| # | Status | Start Time | End Time | Type |',
            '| --- | --- | --- | --- | --- |',
        ]
        for i, entry in enumerate(history, 1):
            status = entry.get('status', '—')
            start = entry.get('startTime', '—')
            end = entry.get('endTime', '—')
            rtype = entry.get('refreshType', '—')
            lines.append(f'| {i} | {status} | {start} | {end} | {rtype} |')
        return '\n'.join(lines)

    return powerbi_get_refresh_history


def _make_list_workspaces(client: PowerBIClient):
    def powerbi_list_workspaces() -> str:
        """
        Lists all PowerBI workspaces accessible to the service principal.

        Use this WHEN the user asks what workspaces or environments exist in PowerBI,
        or when you need to discover a workspace_id that is not available in the catalog.
        """
        try:
            workspaces = client.get_workspaces()
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

        return _fmt_list(workspaces, name_key='name', id_key='id')

    return powerbi_list_workspaces


# ===========================================================================
# ░░░░░░░░░░  INACTIVE TOOLS (defined, not registered)  ░░░░░░░░░░░░░░░░░░░
# To activate a tool: move its factory function into the ``active_tools``
# list inside ``make_powerbi_tools()`` at the bottom of this file.
# ===========================================================================

def _make_list_datasets(client: PowerBIClient):
    def powerbi_list_datasets(workspace_id: str = '') -> str:
        """
        [INACTIVE] Lists all datasets in a PowerBI workspace (or all accessible datasets).

        Use this WHEN the user asks what datasets are available in a workspace and
        the catalog does not have the answer.

        ``workspace_id``: optional — leave blank to list all accessible datasets.
        """
        ws = workspace_id.strip() or None
        try:
            datasets = client.get_datasets(workspace_id=ws)
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

        return _fmt_list(datasets, name_key='name', id_key='id')

    return powerbi_list_datasets


def _make_get_dataset_schema(client: PowerBIClient):
    def powerbi_get_dataset_schema(dataset_id: str, workspace_id: str = '') -> str:
        """
        Returns the table, column, and measure schema for a PowerBI dataset from the live API.

        Use this WHEN the user wants to understand the structure of a dataset
        beyond what is stored in the local catalog, or when a grouped/date DAX
        query fails because a column/table reference may be wrong.

        ``dataset_id``: the PowerBI dataset ID.
        ``workspace_id``: optional workspace ID.
        """
        ws = workspace_id.strip() or None
        try:
            tables = client.get_dataset_tables(dataset_id, workspace_id=ws)
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

        if not tables:
            return f'No tables found for dataset `{dataset_id}`.'

        parts = [
            f'**Schema for dataset `{dataset_id}`**\n',
            'Use DAX references exactly as shown, including single quotes around table names.\n',
        ]
        for tbl in tables:
            table_name = tbl.get("name", "—")
            parts.append(f'### Table: {table_name}')
            cols = tbl.get('columns', [])
            if cols:
                parts.append('| Column | DAX Reference | Data Type |')
                parts.append('| --- | --- | --- |')
                for col in cols:
                    col_name = col.get("name", "—")
                    parts.append(f"| {col_name} | `'{table_name}'[{col_name}]` | {col.get('dataType', '—')} |")
            measures = tbl.get('measures', [])
            if measures:
                parts.append('\n| Measure | DAX Reference |')
                parts.append('| --- | --- |')
                for m in measures:
                    m_name = m.get("name", "—")
                    parts.append(f'| {m_name} | `[{m_name}]` |')
            parts.append('')
        return '\n'.join(parts)

    return powerbi_get_dataset_schema


def _make_list_reports(client: PowerBIClient):
    def powerbi_list_reports(workspace_id: str = '') -> str:
        """
        [INACTIVE] Lists all PowerBI reports in a workspace (or all accessible reports).

        Use this WHEN the user asks what reports exist in a workspace and
        the catalog does not have the answer.

        ``workspace_id``: optional — leave blank to list all accessible reports.
        """
        ws = workspace_id.strip() or None
        try:
            reports = client.get_reports(workspace_id=ws)
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

        return _fmt_list(reports, name_key='name', id_key='id')

    return powerbi_list_reports


def _make_analyze_data_quality(client: PowerBIClient):
    def powerbi_analyze_data_quality(dataset_id: str, workspace_id: str = '') -> str:
        """
        [INACTIVE] Analyzes data quality for each table in a dataset by running
        COUNTROWS() DAX queries and reporting row counts and schema size.

        Use this WHEN the user wants a data quality overview or wants to understand
        which tables have data and how many rows each table contains.

        ``dataset_id``: the PowerBI dataset ID.
        ``workspace_id``: optional workspace ID.
        """
        ws = workspace_id.strip() or None
        try:
            tables = client.get_dataset_tables(dataset_id, workspace_id=ws)
        except PowerBIRequestError as exc:
            return f'PowerBI API error fetching schema: {exc}'

        if not tables:
            return f'No tables found for dataset `{dataset_id}`.'

        results = [
            f'**Data Quality Analysis — dataset `{dataset_id}`**\n',
            '| Table | Row Count | Columns | Measures |',
            '| --- | --- | --- | --- |',
        ]
        for tbl in tables:
            tbl_name = tbl.get('name', '')
            col_count = len(tbl.get('columns', []))
            measure_count = len(tbl.get('measures', []))
            try:
                dax = f"EVALUATE ROW(\"RowCount\", COUNTROWS('{tbl_name}'))"
                raw = client.execute_dax_query(dataset_id, dax, workspace_id=ws)
                rows = raw['results'][0]['tables'][0]['rows']
                row_count = list(rows[0].values())[0] if rows else '?'
            except Exception:
                row_count = 'error'
            results.append(f'| {tbl_name} | {row_count} | {col_count} | {measure_count} |')

        return '\n'.join(results)

    return powerbi_analyze_data_quality


def _make_export_table_to_csv(client: PowerBIClient):
    def powerbi_export_table_to_csv(
        dataset_id: str,
        table_name: str,
        workspace_id: str = '',
        max_rows: int = 500,
    ) -> str:
        """
        [INACTIVE] Exports up to ``max_rows`` rows from a PowerBI table as CSV text.

        Use this WHEN the user wants to see raw data from a specific table or
        export a sample of a dataset for inspection.

        ``dataset_id``: the PowerBI dataset ID.
        ``table_name``: the exact table name within the dataset.
        ``workspace_id``: optional workspace ID.
        ``max_rows``: maximum rows to return (default 500, maximum 1000).
        """
        ws = workspace_id.strip() or None
        capped = min(max_rows, 1000)
        dax = f"EVALUATE TOPN({capped}, '{table_name}')"
        try:
            raw = client.execute_dax_query(dataset_id, dax, workspace_id=ws)
            rows = raw['results'][0]['tables'][0].get('rows', [])
        except PowerBIRequestError as exc:
            return f'PowerBI API error: {exc}'
        except (KeyError, IndexError):
            return 'Could not parse query result.'
        except Exception as exc:
            return f'Unexpected error: {exc}'

        if not rows:
            return f'Table `{table_name}` returned no rows.'

        cols = list(rows[0].keys())
        csv_lines = [','.join(c.lstrip('[').rstrip(']') for c in cols)]
        for row in rows:
            values = [str(row.get(c, '')).replace(',', ';') for c in cols]
            csv_lines.append(','.join(values))

        return (
            f'**Export of `{table_name}` ({len(rows)} rows)**\n\n'
            f'```csv\n{chr(10).join(csv_lines)}\n```'
        )

    return powerbi_export_table_to_csv


def _make_create_measure(client: PowerBIClient):
    def powerbi_create_measure(
        dataset_id: str,
        table_name: str,
        measure_name: str,
        dax_expression: str,
        workspace_id: str = '',
    ) -> str:
        """
        [INACTIVE] Creates a new DAX measure in a PowerBI dataset table.

        Use this WHEN the user explicitly asks to create a new measure in PowerBI.
        ALWAYS confirm the measure name, table, and expression before executing.

        ``dataset_id``: the PowerBI dataset ID.
        ``table_name``: the table in which to create the measure.
        ``measure_name``: the name for the new measure.
        ``dax_expression``: the DAX expression for the measure.
        ``workspace_id``: optional workspace ID.
        """
        ws = workspace_id.strip() or None
        if ws:
            endpoint = f'/groups/{ws}/datasets/{dataset_id}/tables/{table_name}/measures'
        else:
            endpoint = f'/datasets/{dataset_id}/tables/{table_name}/measures'

        body = {'name': measure_name, 'expression': dax_expression}
        try:
            result = client._post(endpoint, body)  # noqa: SLF001
            return (
                f'✅ Measure **{measure_name}** created successfully in table `{table_name}`.\n'
                f'Expression: `{dax_expression}`'
            )
        except PowerBIRequestError as exc:
            return f'PowerBI API error creating measure: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

    return powerbi_create_measure


def _make_create_calculated_column(client: PowerBIClient):
    def powerbi_create_calculated_column(
        dataset_id: str,
        table_name: str,
        column_name: str,
        dax_expression: str,
        workspace_id: str = '',
    ) -> str:
        """
        [INACTIVE] Creates a new calculated column in a PowerBI dataset table.

        Use this WHEN the user explicitly asks to add a calculated column.
        ALWAYS confirm the column name, table, and DAX expression before executing.

        ``dataset_id``: the PowerBI dataset ID.
        ``table_name``: the target table.
        ``column_name``: the name for the new column.
        ``dax_expression``: the DAX expression for the column.
        ``workspace_id``: optional workspace ID.
        """
        ws = workspace_id.strip() or None
        if ws:
            endpoint = f'/groups/{ws}/datasets/{dataset_id}/tables/{table_name}/columns'
        else:
            endpoint = f'/datasets/{dataset_id}/tables/{table_name}/columns'

        body = {'name': column_name, 'dataType': 'String', 'expression': dax_expression}
        try:
            client._post(endpoint, body)  # noqa: SLF001
            return (
                f'✅ Calculated column **{column_name}** created successfully in table `{table_name}`.\n'
                f'Expression: `{dax_expression}`'
            )
        except PowerBIRequestError as exc:
            return f'PowerBI API error creating column: {exc}'
        except Exception as exc:
            return f'Unexpected error: {exc}'

    return powerbi_create_calculated_column


# ===========================================================================
# Factory — called by tools.py to get the registered tool list
# ===========================================================================

def make_powerbi_tools(client: PowerBIClient) -> List:
    """
    Return the list of active PowerBI tool functions bound to *client*.

    ACTIVE (registered with the agent):
      - powerbi_run_dax_query
      - powerbi_get_refresh_history
      - powerbi_list_workspaces
      - powerbi_list_datasets

    INACTIVE (defined above, move here to enable):
      - powerbi_get_dataset_schema     superseded by get_pb_measure_schema
      - powerbi_list_reports
      - powerbi_analyze_data_quality
      - powerbi_export_table_to_csv
      - powerbi_create_measure            ⚠ mutates PowerBI data
      - powerbi_create_calculated_column  ⚠ mutates PowerBI data
    """
    active_tools = [
        _make_run_dax_query(client),
        _make_get_refresh_history(client),
        _make_list_workspaces(client),
        _make_list_datasets(client),
    ]

    # --- INACTIVE — uncomment to enable ---
    # active_tools.append(_make_get_dataset_schema(client))  # use get_pb_measure_schema instead
    # active_tools.append(_make_list_reports(client))
    # active_tools.append(_make_analyze_data_quality(client))
    # active_tools.append(_make_export_table_to_csv(client))
    # active_tools.append(_make_create_measure(client))           # ⚠ MUTATES DATA
    # active_tools.append(_make_create_calculated_column(client)) # ⚠ MUTATES DATA

    return active_tools
