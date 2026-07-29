"""Exclusive ownership of the shared on-disk ETL workspace.

Source extractors currently write to static directories under ``etl/sources``.
Two workers must therefore never extract, transform, load, or clean those files
at the same time, even when they belong to different organizations.
"""

import threading
from contextlib import contextmanager

from django.db import connection


_PIPELINE_LOCK_NAMESPACE = 0x574443  # "WDC"
_PIPELINE_LOCK_KEY = 2
_LOCAL_PIPELINE_LOCK = threading.Lock()


class PipelineLockUnavailable(RuntimeError):
    """Raised when another worker owns the shared ETL workspace."""


def _unavailable():
    return PipelineLockUnavailable(
        'Another source pipeline is already using the shared ETL workspace. '
        'Retry after that source run finishes.'
    )


@contextmanager
def static_etl_files_lock():
    """Try to own the shared ETL files for one extract/transform/load cycle.

    PostgreSQL session advisory locks span transaction boundaries inside loader
    commands and are released automatically if the worker or connection dies.
    The non-PostgreSQL fallback protects concurrent threads in the local/test
    process; production is expected to use PostgreSQL.
    """
    if connection.vendor != 'postgresql':
        acquired = _LOCAL_PIPELINE_LOCK.acquire(blocking=False)
        if not acquired:
            raise _unavailable()
        try:
            yield
        finally:
            _LOCAL_PIPELINE_LOCK.release()
        return

    acquired = False
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_try_advisory_lock(%s, %s)',
            [_PIPELINE_LOCK_NAMESPACE, _PIPELINE_LOCK_KEY],
        )
        row = cursor.fetchone()
        acquired = bool(row and row[0])
    if not acquired:
        raise _unavailable()

    try:
        yield
    finally:
        released = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_advisory_unlock(%s, %s)',
                    [_PIPELINE_LOCK_NAMESPACE, _PIPELINE_LOCK_KEY],
                )
                row = cursor.fetchone()
                released = bool(row and row[0])
        except Exception:
            # Closing the session is PostgreSQL's fail-safe release path.
            try:
                connection.close()
            except Exception:
                pass
        else:
            if not released:
                # The session did not confirm release. Discard it so it cannot
                # retain a lock when returned to a persistent connection pool.
                try:
                    connection.close()
                except Exception:
                    pass
