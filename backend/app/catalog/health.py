"""
Host/app health checks with Slack alerting.

Ported from the propertywise-website health app, trimmed to what this
deployment needs: the ETL workflow writes gigabytes of temporary files
(dbt clone, raw Fabric definitions, CSVs), so the box running the worker
must always have disk headroom — we want a Slack warning at 85% full,
not an ``[Errno 28] No space left on device`` traceback mid-run.

Each check returns a plain dict so it serialises straight to JSON:

    {
        "name":   "Disk space",
        "key":    "disk",
        "status": "ok" | "warn" | "down",
        "detail": "human readable note",
    }

``monitor_and_alert()`` is the entry point for the scheduled run (django-q
Schedule, registered in apps.py) and the ``health_monitor`` management
command. It de-duplicates alerts via the cache: a lingering problem
produces one alert, not one per run, and recovery sends a single message.
"""
import shutil

from django.core.cache import cache
from django.db import connections

OK = 'ok'
WARN = 'warn'
DOWN = 'down'

_SEVERITY = {OK: 0, WARN: 1, DOWN: 2}
_STATE_KEY = 'health:last_alert_status'

# Disk thresholds. Percent catches "the disk is small", the absolute floor
# catches "the disk is big but the ETL working set is bigger".
DISK_WARN_PCT = 85
DISK_DOWN_PCT = 95
DISK_WARN_FREE_GB = 5
DISK_DOWN_FREE_GB = 2


def check_database():
    """Django's default Postgres connection answers a trivial query."""
    try:
        with connections['default'].cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return {'name': 'Database (PostgreSQL)', 'key': 'database', 'status': OK, 'detail': ''}
    except Exception as exc:
        return {'name': 'Database (PostgreSQL)', 'key': 'database', 'status': DOWN,
                'detail': str(exc)[:300]}


def check_disk(path=None):
    """The filesystem the ETL writes to has headroom.

    Defaults to this file's directory — inside the container that is the
    overlay root, i.e. the same filesystem the ETL working files, /tmp and
    docker logs live on.
    """
    import os
    path = path or os.path.dirname(os.path.abspath(__file__))
    try:
        usage = shutil.disk_usage(path)
        pct_used = usage.used / usage.total * 100
        free_gb = usage.free / (1024 ** 3)
        detail = f'{pct_used:.0f}% used, {free_gb:.1f} GB free'
        if pct_used >= DISK_DOWN_PCT or free_gb < DISK_DOWN_FREE_GB:
            status = DOWN
        elif pct_used >= DISK_WARN_PCT or free_gb < DISK_WARN_FREE_GB:
            status = WARN
        else:
            status = OK
        return {'name': 'Disk space', 'key': 'disk', 'status': status, 'detail': detail}
    except Exception as exc:
        return {'name': 'Disk space', 'key': 'disk', 'status': DOWN, 'detail': str(exc)[:300]}


def run_all_checks():
    """Everything, folded into one overall verdict (worst component wins)."""
    checks = [check_database(), check_disk()]
    inverse = {v: k for k, v in _SEVERITY.items()}
    overall = inverse[max(_SEVERITY[c['status']] for c in checks)]
    return {'status': overall, 'checks': checks}


def _send_health_slack(text):
    """Post to every organization's active slack_alerts hook (in practice one).

    Returns the number of channels the message reached."""
    from catalog.models import IntegrationHook
    sent = 0
    hooks = IntegrationHook.objects.filter(hook_type='slack_alerts', is_active=True)
    for hook in hooks:
        channel = hook.slack_alerts_channel or hook.slack_channel
        if not hook.slack_bot_token or not channel:
            continue
        try:
            from slack_sdk import WebClient
            WebClient(token=hook.slack_bot_token).chat_postMessage(channel=channel, text=text)
            sent += 1
        except Exception as e:
            print(f'[Health] Slack alert failed: {e}')
    return sent


def monitor_and_alert(force=False, notify=True):
    """Run all checks; Slack-alert on trouble, once per distinct status.

    Called on a schedule by django-q (see apps.py) and by the
    ``health_monitor`` management command. Returns the result dict so
    callers can print/inspect it.
    """
    result = run_all_checks()
    status = result['status']
    failing = [c for c in result['checks'] if c['status'] != OK]

    if not notify:
        return result

    previous = cache.get(_STATE_KEY, OK)

    if status != OK:
        if previous != status or force:
            lines = [f"• {c['name']}: {c['status'].upper()} ({c['detail'] or 'no detail'})"
                     for c in failing]
            _send_health_slack(
                f"🚨 *Data catalog health: {status.upper()}*\n" + '\n'.join(lines)
            )
        cache.set(_STATE_KEY, status, timeout=None)
    else:
        if previous != OK:
            _send_health_slack('✅ *Data catalog health recovered* — all systems operational.')
        cache.set(_STATE_KEY, OK, timeout=None)

    return result


def ensure_monitor_schedule():
    """Idempotently register the recurring django-q health check.

    Called from CatalogConfig.ready(); safe when the DB isn't migrated yet
    (fails silently — the next boot after migrate will create it).
    """
    try:
        from django_q.models import Schedule
        Schedule.objects.get_or_create(
            name='health_monitor',
            defaults=dict(
                func='catalog.health.monitor_and_alert',
                schedule_type=Schedule.CRON,
                cron='*/15 * * * *',
            ),
        )
    except Exception:
        pass
