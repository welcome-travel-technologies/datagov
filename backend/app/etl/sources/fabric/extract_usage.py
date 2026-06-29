"""
Power BI Report Usage extraction (per workspace × report × user × month).

Each workspace exposes an auto-generated usage-metrics dataset (created the
first time someone opens 'Usage metrics' on a report inside that workspace).

This targets Microsoft's *modern* usage model, whose schema differs from the
legacy 'Report Usage Metrics Model':
  - the view fact is the row-grain table 'Report views' (one row per view),
    so view_count is COUNTROWS, not a pre-aggregated [GranularViewsCount];
  - report identity + name live on 'Report views'[ReportId]/[ReportName]
    (no separate 'Reports' join needed);
  - the 'Users' table renamed its identity columns: the email/UPN is still
    present in 'Users'[UserId] (and [UniqueUser]), with a stable AAD object id
    in 'Users'[UserGuid], both joined to the fact via the opaque
    'Users'[UserKey]. (The given+family display name is gone; we recover it
    best-effort from the workspace user list — see
    _fetch_workspace_display_names.)
  - per-page detail moved to a separate 'Report page views' table, so the
    report-grain pull leaves report_page blank.
We query 'Report views', resolve UserKey→email via 'Users'[UserId], enrich the
display name from the workspace user list, and emit a single CSV consumed by
the load_data management command.

Built-in retention is ~30 days, so we always pull the most recent N months.
The loader then does a *windowed* replace — it refreshes only the months in
this CSV and leaves older months intact — so usage history accumulates across
runs instead of being truncated to the rolling window.
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Callable, Optional

import requests

PBI_BASE = 'https://api.powerbi.com/v1.0/myorg'
# Accepted usage-dataset names, in priority order. The modern usage metrics
# feature creates 'Usage Metrics Report'; some workspaces still carry the
# legacy 'Report Usage Metrics Model' name even after the schema modernised.
# Prefer the newest when both exist; the 'Report views'-based DAX below targets
# the modern schema (see module docstring).
USAGE_DS_NAMES = ('Usage Metrics Report', 'Report Usage Metrics Model')

# --- Modern schema ('Report views' / 'Report page views' / 'Report rank') ----
# UserKey → (email, UserGuid) lookup. The modern model keeps the email/UPN in
# 'Users'[UserId]; [UserGuid] is the AAD object id we fall back to only when the
# email is blank (deleted / guest accounts).
DAX_USERS_MODERN = (
    "EVALUATE SELECTCOLUMNS('Users', "
    "\"UserKey\", 'Users'[UserKey], "
    "\"UserId\", 'Users'[UserId], "
    "\"UserGuid\", 'Users'[UserGuid])"
)

# --- Legacy schema ('Views' / 'Reports' / 'Users' with UPN + names) ----------
# Kept as a per-workspace fallback: a workspace whose usage dataset hasn't been
# migrated to the modern model still answers these queries (and still exposes
# UPN / display name, which the modern model no longer does).
DAX_REPORTS_LEGACY = (
    "EVALUATE SELECTCOLUMNS('Reports', "
    "\"ReportGuid\", 'Reports'[ReportGuid], "
    "\"DisplayName\", 'Reports'[DisplayName])"
)
DAX_USERS_LEGACY = (
    "EVALUATE SELECTCOLUMNS('Users', "
    "\"UserGuid\", 'Users'[UserGuid], "
    "\"UserPrincipalName\", 'Users'[UserPrincipalName], "
    "\"GivenName\", 'Users'[GivenName], "
    "\"FamilyName\", 'Users'[FamilyName])"
)


def _normalize_guid(g: Optional[str]) -> str:
    """Power BI returns some GUIDs wrapped in {curly braces}, others not."""
    if not g:
        return ''
    return g.strip().strip('{}').upper()


def _fetch_workspace_display_names(headers: dict, ws_id: str) -> dict:
    """Map lower-cased email → display name from the workspace user list.

    The modern usage 'Users' table no longer carries the given+family display
    name, but the workspace's user list (``GET /groups/{id}/users``) still does
    — and it answers with the *same* Power BI token we already hold, so no
    Microsoft Graph permission or extra consent is required. Best-effort: any
    failure (e.g. the service principal lacks the workspace user-list right, or
    the viewer never had a workspace role) just yields an empty/partial map and
    the caller falls back to the email.
    """
    out: dict = {}
    try:
        r = requests.get(f'{PBI_BASE}/groups/{ws_id}/users',
                         headers=headers, timeout=30)
        if r.status_code != 200:
            return out
        for u in r.json().get('value', []):
            if (u.get('principalType') or '') != 'User':
                continue
            email = (u.get('emailAddress') or '').strip().lower()
            name = (u.get('displayName') or '').strip()
            if email and name:
                out[email] = name
    except requests.RequestException:
        pass
    return out


def _first_of_month_n_months_ago(n: int) -> date:
    """Return YYYY-MM-01 for the month that is `n` months before today (UTC)."""
    today = datetime.now(timezone.utc).date()
    year = today.year
    month = today.month - n
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _build_views_dax(start: date) -> str:
    """Aggregate the row-grain 'Report views' fact to (report × user × day ×
    consumption × distribution), counting rows as the view total."""
    d = f'DATE({start.year},{start.month},{start.day})'
    return (
        "EVALUATE SUMMARIZECOLUMNS("
        "'Report views'[ReportId],"
        "'Report views'[ReportName],"
        "'Report views'[UserKey],"
        "'Report views'[Date],"
        "'Report views'[ConsumptionMethod],"
        "'Report views'[DistributionMethod],"
        f"FILTER(ALL('Report views'[Date]), 'Report views'[Date] >= {d}),"
        "\"Views\", COUNTROWS('Report views'))"
    )


def _build_legacy_views_dax(start: date) -> str:
    """Legacy 'Views' fact (pre-aggregated [GranularViewsCount]). Used only as a
    per-workspace fallback when the modern 'Report views' query fails."""
    d = f'DATE({start.year},{start.month},{start.day})'
    return (
        "EVALUATE SUMMARIZECOLUMNS("
        "'Views'[ReportGuid],"
        "'Views'[UserGuid],"
        "'Views'[Date],"
        "'Views'[Platform],"
        "'Views'[DistributionMethod],"
        "'Views'[ReportPage],"
        f"FILTER(ALL('Views'[Date]), 'Views'[Date] >= {d}),"
        "\"Views\", SUM('Views'[GranularViewsCount]))"
    )


def _find_usage_datasets(headers: dict, ws_id: str) -> list[dict]:
    """Return *every* usage-metrics dataset in the workspace, ordered by
    USAGE_DS_NAMES priority (the new 'Usage Metrics Report' before the legacy
    'Report Usage Metrics Model').

    A single workspace frequently carries BOTH: Microsoft auto-creates the
    modern 'Usage Metrics Report' (often empty until the new usage-metrics view
    is opened) while the legacy 'Report Usage Metrics Model' still holds the
    accumulated views. Returning all candidates lets the caller fall through an
    empty modern dataset to the populated legacy one instead of stopping at the
    first name match (which silently zeroed those workspaces).
    """
    r = requests.get(f'{PBI_BASE}/groups/{ws_id}/datasets', headers=headers, timeout=30)
    if r.status_code != 200:
        return []
    by_name = {(d.get('name') or '').strip().lower(): d for d in r.json().get('value', [])}
    return [by_name[name.lower()] for name in USAGE_DS_NAMES if name.lower() in by_name]


def _run_dax(headers: dict, ws_id: str, ds_id: str, dax: str) -> list[dict]:
    r = requests.post(
        f'{PBI_BASE}/groups/{ws_id}/datasets/{ds_id}/executeQueries',
        headers={**headers, 'Content-Type': 'application/json'},
        json={'queries': [{'query': dax}], 'serializerSettings': {'includeNulls': True}},
        timeout=180,
    )
    r.raise_for_status()
    return r.json().get('results', [{}])[0].get('tables', [{}])[0].get('rows', [])


def _log_dataset_schema(headers: dict, ws_id: str, ds_id: str,
                        log: Callable[[str], None], label: str) -> None:
    """On a DAX failure, dump the usage dataset's real table/column names.

    Microsoft periodically migrates the auto-generated usage model to a new
    schema, which breaks the hard-coded 'Views'/'Reports'/'Users' DAX above
    with a 'Cannot find table or column' error. Logging the actual schema here
    means a single re-run surfaces exactly what the DAX must be updated to.
    The INFO.* functions need a Premium/PPU/Fabric capacity; if unsupported we
    fail quietly rather than masking the original error.
    """
    try:
        tables = _run_dax(headers, ws_id, ds_id,
                          'EVALUATE SELECTCOLUMNS(INFO.TABLES(), "n", [Name])')
        names = sorted({(t.get('[n]') or '').strip() for t in tables} - {''})
        if names:
            log(f'         ↳ {label}: actual tables = {", ".join(names)}')
        cols = _run_dax(
            headers, ws_id, ds_id,
            'EVALUATE SELECTCOLUMNS(INFO.COLUMNS(), "n", [ExplicitName])')
        col_names = sorted({(c.get('[n]') or '').strip() for c in cols} - {''})
        if col_names:
            log(f'         ↳ {label}: actual columns = {", ".join(col_names)}')
    except Exception:
        pass


def _collect_modern(headers: dict, ws_id: str, ws_name: str, ds_id: str,
                    views_dax: str, counts: dict) -> tuple:
    """Modern 'Report views' (row-grain) schema. Aggregates view rows into
    ``counts`` and returns ``(ws_total_views, grain_row_count)``.

    The first call is the views query; if the dataset isn't on the modern
    schema it raises ``requests.HTTPError`` *before* mutating ``counts``, so the
    caller can cleanly fall back to the legacy schema with no double-counting.
    """
    views = _run_dax(headers, ws_id, ds_id, views_dax)

    # UserKey → {email, guid}. Best-effort: a failure here must not zero out the
    # view counts we already pulled, so it never aborts the workspace. The
    # modern 'Users' table keeps the email/UPN in [UserId]; [UserGuid] is the
    # fallback identifier used only when the email is blank.
    user_by_key: dict[str, dict] = {}
    try:
        for u in _run_dax(headers, ws_id, ds_id, DAX_USERS_MODERN):
            user_by_key[str(u.get('[UserKey]'))] = {
                'email': (u.get('[UserId]') or '').strip(),
                'guid': _normalize_guid(u.get('[UserGuid]')),
            }
    except requests.HTTPError:
        pass

    # email → display name, from the workspace user list (no Graph, same token).
    display_by_email = _fetch_workspace_display_names(headers, ws_id)

    ws_total = 0
    for row in views:
        report_id = row.get('Report views[ReportId]')
        report_name = row.get('Report views[ReportName]') or ''
        user_key = str(row.get('Report views[UserKey]') or '')
        date_str = row.get('Report views[Date]') or ''
        consumption = row.get('Report views[ConsumptionMethod]') or ''
        distribution = row.get('Report views[DistributionMethod]') or ''
        n = int(row.get('[Views]') or 0)
        if not (report_id and date_str and n):
            continue
        month = date_str[:7] + '-01'
        info = user_by_key.get(user_key) or {}
        email = info.get('email') or ''
        # Prefer the real email; fall back to the AAD GUID, then the opaque
        # UserKey, so distinct-viewer counts always hold.
        user_email = email or info.get('guid') or user_key
        user_display = display_by_email.get(email.lower(), '') if email else ''
        key = (
            month,
            ws_id, ws_name,
            str(report_id), report_name,
            user_email, user_display,
            consumption, distribution, '',
        )
        counts[key] += n
        ws_total += n
    return ws_total, len(views)


def _collect_legacy(headers: dict, ws_id: str, ws_name: str, ds_id: str,
                    views_dax: str, counts: dict) -> tuple:
    """Legacy 'Views'/'Reports'/'Users' schema (pre-aggregated, with UPN +
    display name). Aggregates into ``counts`` and returns
    ``(ws_total_views, grain_row_count)``. All three queries run before any
    mutation, so a failure raises without partially populating ``counts``.
    """
    views = _run_dax(headers, ws_id, ds_id, views_dax)
    reports = _run_dax(headers, ws_id, ds_id, DAX_REPORTS_LEGACY)
    users = _run_dax(headers, ws_id, ds_id, DAX_USERS_LEGACY)

    report_name_by_guid = {
        _normalize_guid(r.get('[ReportGuid]')): r.get('[DisplayName]') for r in reports
    }
    user_by_guid = {
        _normalize_guid(u.get('[UserGuid]')): {
            'upn': u.get('[UserPrincipalName]') or '',
            'name': ' '.join(filter(None, [u.get('[GivenName]'), u.get('[FamilyName]')])),
        }
        for u in users
    }

    ws_total = 0
    for row in views:
        report_guid = _normalize_guid(row.get('Views[ReportGuid]'))
        user_guid = _normalize_guid(row.get('Views[UserGuid]'))
        date_str = row.get('Views[Date]') or ''
        platform = row.get('Views[Platform]') or ''
        distribution = row.get('Views[DistributionMethod]') or ''
        page = row.get('Views[ReportPage]') or ''
        n = int(row.get('[Views]') or 0)
        if not (report_guid and date_str and n):
            continue
        month = date_str[:7] + '-01'
        report_name = report_name_by_guid.get(report_guid, '')
        user_info = user_by_guid.get(user_guid, {'upn': '', 'name': ''})
        user_email = user_info['upn'] or user_guid
        user_display = user_info['name']
        key = (
            month,
            ws_id, ws_name,
            report_guid, report_name,
            user_email, user_display,
            platform, distribution, page,
        )
        counts[key] += n
        ws_total += n
    return ws_total, len(views)


def run_usage_extraction(token: str, workspace_ids: list, etl_dir: str,
                         log: Callable[[str], None], months: int = 3) -> str:
    """
    Iterate each configured workspace, query its 'Report Usage Metrics Model'
    dataset for the last `months` months, aggregate to monthly grain, and
    write the result to <etl_dir>/data/fabric_info_usage.csv.

    Returns the absolute CSV path. The CSV is always written (header-only when
    there is no data) so the loader can rely on its presence.
    """
    headers = {'Authorization': f'Bearer {token}'}
    start = _first_of_month_n_months_ago(months - 1)  # include current month
    log(f'\nExtracting Power BI usage (last {months} months, since {start})...')

    views_dax = _build_views_dax(start)
    legacy_views_dax = _build_legacy_views_dax(start)
    counts: dict[tuple, int] = defaultdict(int)
    workspace_name_by_id: dict[str, str] = {}
    ws_with_data = 0
    errors = 0

    # Get workspace names — used so the CSV carries human-readable labels even
    # if the Item table doesn't have the workspace yet (e.g. usage-only ws).
    ws_resp = requests.get(f'{PBI_BASE}/groups', headers=headers, timeout=30)
    if ws_resp.status_code == 200:
        for w in ws_resp.json().get('value', []):
            workspace_name_by_id[w['id']] = w.get('name', '')

    for ws_id in workspace_ids:
        ws_name = workspace_name_by_id.get(ws_id, '')
        candidates = _find_usage_datasets(headers, ws_id)
        if not candidates:
            log(f'  [skip] {ws_name or ws_id} - no usage dataset '
                f'({" / ".join(USAGE_DS_NAMES)})')
            continue

        # A workspace can carry both the modern 'Usage Metrics Report' and the
        # legacy 'Report Usage Metrics Model'. The modern one is often present
        # but empty, so we can't commit to the first dataset found: try each
        # candidate (modern 'Report views' schema first, then the legacy 'Views'
        # schema) and keep the FIRST that returns rows. We break as soon as a
        # dataset yields grain, so exactly one (dataset, schema) pair feeds
        # `counts` and a workspace is never double-counted. An empty modern
        # query adds nothing to `counts`, so falling through to the legacy
        # dataset is safe.
        ws_total = grain = 0
        schema = None
        last_error = None
        for ds in candidates:
            ds_id = ds['id']
            try:
                ws_total, grain = _collect_modern(
                    headers, ws_id, ws_name, ds_id, views_dax, counts)
                schema = 'modern'
            except requests.HTTPError:
                try:
                    ws_total, grain = _collect_legacy(
                        headers, ws_id, ws_name, ds_id, legacy_views_dax, counts)
                    schema = 'legacy'
                except requests.HTTPError as exc:
                    last_error = exc
                    continue  # this dataset answers neither schema — try next
            if grain:
                break  # got data — stop before another dataset re-adds views

        if schema is None:
            # Every candidate dataset failed both schemas — surface the drift
            # diagnostics for the highest-priority one to guide a DAX update.
            exc = last_error
            body = (exc.response.text[:600]
                    if exc is not None and exc.response is not None else '')
            log(f'  [error] {ws_name or ws_id} - {exc} {body}')
            _log_dataset_schema(headers, ws_id, candidates[0]['id'], log,
                                ws_name or ws_id)
            errors += 1
            continue

        log(f'  [ok]   {ws_name or ws_id} - {grain} grain rows, '
            f'{ws_total} views ({schema})')
        if grain:
            ws_with_data += 1

    fieldnames = [
        'month', 'workspace_id', 'workspace_name',
        'report_id', 'report_name',
        'user_email', 'user_display_name',
        'platform', 'distribution_method', 'report_page',
        'view_count',
    ]
    out_dir = os.path.join(etl_dir, 'data')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'fabric_info_usage.csv')
    rows = sorted(
        (
            {
                'month': k[0],
                'workspace_id': k[1],
                'workspace_name': k[2],
                'report_id': k[3],
                'report_name': k[4],
                'user_email': k[5],
                'user_display_name': k[6],
                'platform': k[7],
                'distribution_method': k[8],
                'report_page': k[9],
                'view_count': v,
            }
            for k, v in counts.items()
        ),
        key=lambda r: (r['month'], r['workspace_name'], r['report_name'], r['user_email']),
    )
    with open(out_path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    log(f'✅ Usage CSV: {len(rows)} rows from {ws_with_data} workspace(s) '
        f'(total {sum(counts.values())} views) → {out_path}')
    if not rows and errors:
        log(f'⚠️  Usage extraction produced 0 rows but hit query errors on '
            f'{errors} workspace(s) — the usage dataset schema has likely '
            f'drifted from the DAX in this module (see the [error]/↳ lines '
            f'above). The header-only CSV yields an empty month set, so the '
            f'loader\'s windowed replace deletes nothing and existing usage '
            f'history is preserved instead of being wiped to zero.')
    return out_path
