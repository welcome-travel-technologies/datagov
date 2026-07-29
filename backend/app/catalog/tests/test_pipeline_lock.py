import threading
from types import SimpleNamespace

import pytest

from catalog.services import pipeline_lock


class _PostgresCursor:
    def __init__(self, events, *, acquire=True, release=True):
        self.events = events
        self.acquire = acquire
        self.release = release
        self.statement = ''

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params):
        self.statement = ' '.join(sql.split())
        self.events.append((self.statement, params))

    def fetchone(self):
        if 'pg_try_advisory_lock' in self.statement:
            return [self.acquire]
        return [self.release]


def _postgres_connection(events, *, acquire=True, release=True):
    return SimpleNamespace(
        vendor='postgresql',
        cursor=lambda: _PostgresCursor(
            events, acquire=acquire, release=release,
        ),
        close=lambda: events.append(('closed', None)),
    )


def test_postgres_try_lock_wraps_work_and_releases(monkeypatch):
    events = []
    monkeypatch.setattr(
        pipeline_lock,
        'connection',
        _postgres_connection(events),
    )

    with pipeline_lock.static_etl_files_lock():
        events.append(('work', None))

    assert events == [
        (
            'SELECT pg_try_advisory_lock(%s, %s)',
            [
                pipeline_lock._PIPELINE_LOCK_NAMESPACE,
                pipeline_lock._PIPELINE_LOCK_KEY,
            ],
        ),
        ('work', None),
        (
            'SELECT pg_advisory_unlock(%s, %s)',
            [
                pipeline_lock._PIPELINE_LOCK_NAMESPACE,
                pipeline_lock._PIPELINE_LOCK_KEY,
            ],
        ),
    ]


def test_postgres_contention_fails_without_running_body(monkeypatch):
    events = []
    monkeypatch.setattr(
        pipeline_lock,
        'connection',
        _postgres_connection(events, acquire=False),
    )

    with pytest.raises(
        pipeline_lock.PipelineLockUnavailable,
        match='Another source pipeline',
    ):
        with pipeline_lock.static_etl_files_lock():
            events.append(('must-not-run', None))

    assert events == [
        (
            'SELECT pg_try_advisory_lock(%s, %s)',
            [
                pipeline_lock._PIPELINE_LOCK_NAMESPACE,
                pipeline_lock._PIPELINE_LOCK_KEY,
            ],
        ),
    ]


def test_non_postgres_fallback_is_try_locked_and_reusable(monkeypatch):
    local_lock = threading.Lock()
    monkeypatch.setattr(pipeline_lock, '_LOCAL_PIPELINE_LOCK', local_lock)
    monkeypatch.setattr(
        pipeline_lock,
        'connection',
        SimpleNamespace(vendor='sqlite'),
    )

    with pipeline_lock.static_etl_files_lock():
        with pytest.raises(pipeline_lock.PipelineLockUnavailable):
            with pipeline_lock.static_etl_files_lock():
                pass

    with pipeline_lock.static_etl_files_lock():
        pass
