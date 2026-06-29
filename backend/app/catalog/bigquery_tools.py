"""Guarded BigQuery tools for the Pydantic AI chatbot agent."""
import re
from decimal import Decimal
from typing import Any


MAX_BYTES_BILLED = 1_000_000_000  # 1 GB safety cap per query
MAX_RESULT_ROWS = 50

_LEADING_READ_ONLY = re.compile(r'^\s*(select|with)\b', re.IGNORECASE | re.DOTALL)
_FORBIDDEN_SQL = re.compile(
    r'\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|call|execute|begin|commit|rollback|load|export)\b',
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r'/\*.*?\*/', '', sql or '', flags=re.DOTALL)
    sql = re.sub(r'--.*?(?=\n|$)', '', sql)
    return sql.strip()


def validate_read_only_sql(sql: str) -> tuple[bool, str]:
    """Return (ok, message) for the BigQuery read-only guardrail."""
    cleaned = _strip_sql_comments(sql)
    if not cleaned:
        return False, 'SQL is required.'
    if not _LEADING_READ_ONLY.match(cleaned):
        return False, 'Only read-only SELECT/WITH queries are allowed.'
    if _FORBIDDEN_SQL.search(cleaned):
        return False, 'Query contains a forbidden write/admin statement.'
    if cleaned.count(';') > 1 or (cleaned.endswith(';') and ';' in cleaned[:-1]):
        return False, 'Only one SQL statement is allowed.'
    return True, cleaned


def _stringify(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, Decimal):
        return str(value)
    text = str(value)
    return text.replace('\n', ' ')[:300]


def _fmt_rows(rows: list[dict], max_rows: int = MAX_RESULT_ROWS) -> str:
    if not rows:
        return 'Rows returned: 0\n\nNo rows returned.'
    cols = list(rows[0].keys())
    lines = [
        f'Rows returned: {len(rows)}',
        '',
        '| ' + ' | '.join(cols) + ' |',
        '| ' + ' | '.join(['---'] * len(cols)) + ' |',
    ]
    for row in rows[:max_rows]:
        lines.append('| ' + ' | '.join(_stringify(row.get(col)) for col in cols) + ' |')
    if len(rows) > max_rows:
        lines.append(f'\n*Showing first {max_rows} rows.*')
    return '\n'.join(lines)


def _schema_lines(fields, prefix: str = '') -> list[str]:
    lines = []
    for field in fields:
        name = f'{prefix}{field.name}'
        lines.append(
            f'| `{name}` | {field.field_type} | {field.mode} | '
            f'{(field.description or "")[:160]} |'
        )
        if getattr(field, 'fields', None):
            lines.extend(_schema_lines(field.fields, prefix=f'{name}.'))
    return lines


def make_bigquery_tools(client):
    def bigquery_execute_query(sql: str, max_rows: int = MAX_RESULT_ROWS) -> str:
        """
        Executes a read-only SQL query against BigQuery and returns capped results.

        Use this WHEN:
        - The user asks for live BigQuery data.
        - The user provides a SELECT/WITH SQL query to run.
        - You need a small sample or aggregate result from BigQuery.

        Safety: only SELECT/WITH queries are allowed. The tool performs a dry run
        first, rejects write/admin statements, caps bytes billed, and returns at
        most ``max_rows`` rows (default 50).
        """
        ok, cleaned_or_error = validate_read_only_sql(sql)
        if not ok:
            return f'BigQuery query rejected: {cleaned_or_error}'

        try:
            dry_job = client.dry_run_query(cleaned_or_error, maximum_bytes_billed=MAX_BYTES_BILLED)
            bytes_processed = int(getattr(dry_job, 'total_bytes_processed', 0) or 0)
            if bytes_processed > MAX_BYTES_BILLED:
                return (
                    'BigQuery query rejected: dry-run estimate exceeds the 1 GB safety cap '
                    f'({bytes_processed:,} bytes). Please add filters or aggregate first.'
                )
            rows = client.execute_query(cleaned_or_error, maximum_bytes_billed=MAX_BYTES_BILLED)
        except Exception as exc:
            return f'BigQuery error: {type(exc).__name__}: {exc}'

        max_rows = max(1, min(int(max_rows or MAX_RESULT_ROWS), MAX_RESULT_ROWS))
        return f'Dry-run bytes processed: {bytes_processed:,}\n\n{_fmt_rows(rows, max_rows=max_rows)}'

    def bigquery_get_table_schema(table_fqn: str) -> str:
        """
        Returns BigQuery table schema and metadata for a table/view.

        Use this WHEN the user asks about BigQuery columns, data types,
        partitioning, clustering, row counts, or table structure.

        ``table_fqn`` may be table, dataset.table, or project.dataset.table.
        """
        try:
            table = client.get_table(table_fqn)
        except Exception as exc:
            return f'BigQuery error fetching schema: {type(exc).__name__}: {exc}'

        parts = [
            f'Table: `{table.full_table_id.replace(":", ".")}`',
            f'Type: {getattr(table, "table_type", "TABLE")}',
            f'Rows: {getattr(table, "num_rows", "?")}',
        ]
        if getattr(table, 'time_partitioning', None):
            field = table.time_partitioning.field or '_PARTITIONTIME'
            parts.append(f'Partitioned by: {field}')
        if getattr(table, 'clustering_fields', None):
            parts.append(f'Clustered by: {", ".join(table.clustering_fields)}')
        parts.extend(['', '| Column | Type | Mode | Description |', '| --- | --- | --- | --- |'])
        parts.extend(_schema_lines(table.schema))
        return '\n'.join(parts)

    def bigquery_list_datasets(limit: int = 50) -> str:
        """Lists BigQuery datasets accessible in the configured project."""
        try:
            datasets = client.list_datasets(max_results=max(1, min(int(limit or 50), 100)))
        except Exception as exc:
            return f'BigQuery error listing datasets: {type(exc).__name__}: {exc}'
        if not datasets:
            return 'No BigQuery datasets found.'
        lines = ['| Dataset | Project |', '| --- | --- |']
        for ds in datasets:
            ref = ds.reference
            lines.append(f'| `{ref.dataset_id}` | `{ref.project}` |')
        return '\n'.join(lines)

    def bigquery_list_tables(dataset_id: str, limit: int = 100) -> str:
        """Lists BigQuery tables/views in a dataset."""
        try:
            tables = client.list_tables(dataset_id, max_results=max(1, min(int(limit or 100), 200)))
        except Exception as exc:
            return f'BigQuery error listing tables: {type(exc).__name__}: {exc}'
        if not tables:
            return f'No BigQuery tables found in `{dataset_id}`.'
        lines = ['| Table | Type |', '| --- | --- |']
        for table in tables:
            lines.append(f'| `{table.full_table_id.replace(":", ".")}` | {table.table_type} |')
        return '\n'.join(lines)

    return [
        bigquery_execute_query,
        bigquery_get_table_schema,
        bigquery_list_datasets,
        bigquery_list_tables,
    ]