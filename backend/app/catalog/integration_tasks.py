"""
Django-Q background tasks for integrations.
All source runs happen here — fully async, with log capture and automatic destination push.
"""
import io
import os
import sys
import time
import traceback
from django.utils import timezone
from django.core.management import call_command
from django.core.cache import cache


class WorkflowCancelled(Exception):
    """Raised when a user requests cancellation of a workflow run."""


class TaskCancelled(Exception):
    """Raised when a user requests cancellation of a running source/destination task."""


def _workflow_cancel_key(workflow_run_id):
    return f'workflow_cancel_{workflow_run_id}'


def _is_workflow_cancelled(workflow_run_id):
    return bool(cache.get(_workflow_cancel_key(workflow_run_id)))


# ── Per-task cooperative cancellation ────────────────────────────────────────
# Standalone source/destination runs (run_source_task / run_destination_task)
# check a cache flag keyed on the target id at checkpoints between major steps.
# Setting the flag (request_*_cancel) asks the worker to stop at its next
# checkpoint and mark the run failed — a long step already in progress (e.g. a
# multi-minute dbt extract) finishes that step first, then the run stops.
#
# All cache access here is best-effort: a cache backend hiccup must never crash
# a long ETL run (and the test DB has no DatabaseCache table). A read failure
# degrades to "not cancelled"; a write failure to "signal not delivered".

def _source_cancel_key(source_id):
    return f'source_cancel_{source_id}'


def _destination_cancel_key(dest_id):
    return f'destination_cancel_{dest_id}'


def _cache_get_safe(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _cache_set_safe(key, value, timeout=86400):
    try:
        cache.set(key, value, timeout=timeout)
        return True
    except Exception:
        return False


def _cache_delete_safe(key):
    try:
        cache.delete(key)
    except Exception:
        pass


def request_source_cancel(source_id):
    """Signal a running ``run_source_task`` to stop at its next checkpoint."""
    return _cache_set_safe(_source_cancel_key(source_id), True)


def request_destination_cancel(dest_id):
    """Signal a running ``run_destination_task`` to stop at its next checkpoint."""
    return _cache_set_safe(_destination_cancel_key(dest_id), True)


def request_workflow_cancel(workflow_run_id):
    """Signal a running ``run_workflow_task`` to stop at its next checkpoint."""
    return _cache_set_safe(_workflow_cancel_key(workflow_run_id), True)


def run_source_task(source_id, triggered_by='manual'):
    """
    Django-Q task: runs a single IntegrationSource end-to-end.

    The source type is resolved via the registry (SOURCE_REGISTRY) so this
    function is agnostic to whether it's PowerBI, dbt, or any future source.

    Steps:
      1. Extract + Transform  — calls src.extract() via registry
      2. Load into Django DB  — calls src.load_command management command
      3. Cleanup local files
      4. Send Slack alert
    """
    from catalog.models import IntegrationSource, SourceRunLog, SourceSchedule
    from etl.hooks.slack.slack_alerts import send_slack_alert
    
    log_lines = []

    # Save original stdout to avoid recursion when extract_fabric overrides sys.stdout
    original_stdout = sys.stdout

    # Throttled live-flush: persist the buffered log to the DB at most every few
    # seconds so the UI shows progress *during* the run. Without this, log_output
    # is only written in the finally block, so a long dbt run (column-lineage can
    # take many minutes) shows "no output" until it finishes.
    _last_flush = [0.0]

    def _persist_log():
        try:
            run_log.log_output = '\n'.join(log_lines)
            run_log.save(update_fields=['log_output'])
        except Exception:
            pass

    def log(msg):
        log_lines.append(str(msg))
        original_stdout.write(str(msg) + '\n')
        now = time.monotonic()
        if now - _last_flush[0] >= 2.0:
            _last_flush[0] = now
            _persist_log()

    # Find or create the run log (may already exist if created by the API view)
    try:
        source = IntegrationSource.objects.get(pk=source_id)
    except IntegrationSource.DoesNotExist:
        print(f'[ERROR] IntegrationSource id={source_id} not found.')
        return 'failed'

    # Find the most recent 'running' log for this source (created by the API trigger)
    run_log = SourceRunLog.objects.filter(source=source, status='running').order_by('-started_at').first()
    if not run_log:
        run_log = SourceRunLog.objects.create(
            source=source,
            status='running',
            triggered_by=triggered_by,
        )

    # Clear any stale cancel flag from a previous (possibly crashed) run so a
    # leftover flag can't instantly kill this fresh run.
    _cache_delete_safe(_source_cancel_key(source_id))

    def raise_if_cancelled():
        """Cooperative cancellation checkpoint between major steps."""
        if _cache_get_safe(_source_cancel_key(source_id)):
            log(f'\n[{timezone.now().isoformat()}] ⏹ Cancellation requested by user.')
            raise TaskCancelled()

    try:
        log(f'[{timezone.now().isoformat()}] ▶ Starting source run: {source.name}')
        log(f'  Source type : {source.source_type}')
        log(f'  Triggered by: {triggered_by}')
        log('')
        raise_if_cancelled()

        # ── Step 1 & 2: Extract + Transform ──────────────────────────────────
        from etl.sources.registry import get_source
        src = get_source(source)

        # Use registry-provided ETL dir and load command — no hardcoded source_type
        etl_dir = src.__class__.get_etl_dir()
        src.extract(etl_dir=etl_dir, log=log)
        raise_if_cancelled()

        # ── Step 3: Load into Django DB ───────────────────────────────────────
        log(f'\n[{timezone.now().isoformat()}] Loading data into Django database...')
        out = io.StringIO()
        call_command(src.load_command, organization_id=source.organization_id,
                     source_id=source.pk, stdout=out)
        log(out.getvalue())
        raise_if_cancelled()

        run_log.status = 'success'
        log(f'\n[{timezone.now().isoformat()}] ✅ Source run completed successfully.')

    except TaskCancelled:
        run_log.status = 'failed'
        log(f'\n[{timezone.now().isoformat()}] ⏹ Source run killed by user.')

    except Exception as e:
        run_log.status = 'failed'
        log(f'\n[{timezone.now().isoformat()}] ❌ Source run FAILED: {str(e)}')
        log(traceback.format_exc())

    finally:
        _cache_delete_safe(_source_cancel_key(source_id))
        run_log.finished_at = timezone.now()
        run_log.log_output = '\n'.join(log_lines)
        run_log.save()

        # Update schedule last_run_at
        try:
            sched = source.schedule
            sched.last_run_at = timezone.now()
            sched.save(update_fields=['last_run_at'])
        except Exception:
            pass

        # ── Step 4: Clean up local ETL files ─────────────────────────────────
        _cleanup_local_files(log)

        # ── Step 5: Clean up old run logs ─────────────────────────────────────
        _cleanup_old_run_logs(source, log)

        # ── Step 6: Send Slack alert ──────────────────────────────────────────
        send_slack_alert(source, run_log)

    return run_log.status


# ─────────────────────────────────────────────────────────────────────────────
# Local file cleanup
# ─────────────────────────────────────────────────────────────────────────────

def _cleanup_local_files(log):
    """
    Remove all locally generated ETL files after a source run completes:
    - raw_fabric_definitions/  (extracted raw JSON/PBIX definitions)
    - data/                    (transformed CSV files)
    """
    import shutil
    etl_dir = _get_etl_dir()
    dbt_etl_dir = _get_dbt_etl_dir()
    dirs_to_clean = [
        os.path.join(etl_dir, 'raw_fabric_definitions'),
        os.path.join(etl_dir, 'data'),
        os.path.join(dbt_etl_dir, 'dbt_artifacts'),
        os.path.join(dbt_etl_dir, 'data'),
    ]
    for d in dirs_to_clean:
        if os.path.exists(d):
            try:
                shutil.rmtree(d)
                log(f'[Cleanup] Removed: {d}')
            except Exception as e:
                log(f'[Cleanup] Failed to remove {d}: {e}')




def _cleanup_old_run_logs(source, log):
    """Delete run logs older than 10 days for this source."""
    from catalog.models import SourceRunLog
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(days=10)
    old_logs = SourceRunLog.objects.filter(source=source, started_at__lt=cutoff)
    count = old_logs.count()
    if count > 0:
        old_logs.delete()
        log(f'[Cleanup] Deleted {count} old run logs (older than 10 days).')


def run_destination_task(dest_id, triggered_by='manual'):
    """
    Django-Q task: runs an IntegrationDestination end-to-end.
    1. Extracts catalog data from the Django Database
    2. Pushes to Google BigQuery
    3. Update DestinationRunLog with status + full log output
    4. Send Slack alert
    """
    from catalog.models import IntegrationDestination, DestinationRunLog, DestinationSchedule
    from etl.destinations.bigquery.push_to_bigquery import push_to_bigquery
    from etl.hooks.slack.slack_alerts import send_slack_dest_alert
    
    log_lines = []
    
    original_stdout = sys.stdout
    
    def log(msg):
        log_lines.append(str(msg))
        original_stdout.write(str(msg) + '\n')

    try:
        dest = IntegrationDestination.objects.get(pk=dest_id)
    except IntegrationDestination.DoesNotExist:
        print(f'[ERROR] IntegrationDestination id={dest_id} not found.')
        return 'failed'

    run_log = DestinationRunLog.objects.filter(destination=dest, status='running').order_by('-started_at').first()
    if not run_log:
        run_log = DestinationRunLog.objects.create(
            destination=dest,
            status='running',
            triggered_by=triggered_by,
        )

    # Clear any stale cancel flag so a leftover can't instantly kill this run.
    _cache_delete_safe(_destination_cancel_key(dest_id))

    def raise_if_cancelled():
        """Cooperative cancellation checkpoint between major steps."""
        if _cache_get_safe(_destination_cancel_key(dest_id)):
            log(f'\n[{timezone.now().isoformat()}] ⏹ Cancellation requested by user.')
            raise TaskCancelled()

    try:
        log(f'[{timezone.now().isoformat()}] ▶ Starting destination push: {dest.name}')
        log(f'  Destination type : {dest.destination_type}')
        log(f'  Triggered by: {triggered_by}')
        log('')
        raise_if_cancelled()

        if dest.destination_type == 'bigquery':
            log(f'\n[{timezone.now().isoformat()}] Pushing to BigQuery: {dest.name}')
            result = push_to_bigquery(dest, None, log)
        else:
            raise ValueError(f'Unknown destination type: {dest.destination_type}')
        raise_if_cancelled()

        run_log.status = 'success'
        log(f'\n[{timezone.now().isoformat()}] ✅ Destination push completed successfully.')

    except TaskCancelled:
        run_log.status = 'failed'
        log(f'\n[{timezone.now().isoformat()}] ⏹ Destination push killed by user.')

    except Exception as e:
        run_log.status = 'failed'
        log(f'\n[{timezone.now().isoformat()}] ❌ Destination push FAILED: {str(e)}')
        log(traceback.format_exc())

    finally:
        _cache_delete_safe(_destination_cancel_key(dest_id))
        run_log.finished_at = timezone.now()
        run_log.log_output = '\n'.join(log_lines)
        run_log.save()

        try:
            sched = dest.schedule
            sched.last_run_at = timezone.now()
            sched.save(update_fields=['last_run_at'])
        except Exception:
            pass

        # Cleanup old run logs for destination
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=10)
        old_logs = DestinationRunLog.objects.filter(destination=dest, started_at__lt=cutoff)
        count = old_logs.count()
        if count > 0:
            old_logs.delete()
            log(f'[Cleanup] Deleted {count} old run logs (older than 10 days).')

        # Send Slack alert in finally so it fires on both success and failure
        duration_secs = (
            int((run_log.finished_at - run_log.started_at).total_seconds())
            if run_log.finished_at and run_log.started_at else None
        )
        send_slack_dest_alert(dest, run_log.status, duration_secs)

    return run_log.status


def _get_etl_dir():
    """Return the absolute path to the Fabric ETL directory."""
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'etl', 'sources', 'fabric')
    )


def run_workflow_scheduled(organization_id, triggered_by='scheduler'):
    """Django-Q entrypoint for scheduled workflow runs.

    The Schedule cannot know the WorkflowRun id ahead of time, so this wrapper
    creates the WorkflowRun row first and then hands off to run_workflow_task.
    """
    from catalog.models import Organization, WorkflowRun
    org = Organization.objects.get(pk=organization_id)
    wf = WorkflowRun.objects.create(
        organization=org, status='pending', triggered_by=triggered_by,
    )
    return run_workflow_task(wf.id, triggered_by=triggered_by)


def run_workflow_task(workflow_run_id, triggered_by='manual'):
    """
    Django-Q task: orchestrates the full ETL pipeline end-to-end.

    ① INIT     — lightweight pre-checks
    ② SOURCES  — run each active source sequentially (extract → transform → load)
    ③ FINAL    — cross-tool bridges, summary calculation
    ④ DESTINATIONS — push to all active destinations
    """
    from catalog.models import (
        IntegrationSource, IntegrationDestination,
        WorkflowRun, SourceRunLog, DestinationRunLog,
        WorkflowRawExport,
    )
    from etl.hooks.slack.slack_alerts import send_slack_alert, send_slack_dest_alert
    from etl.destinations.gcs.raw_export import upload_source_raw_to_gcs

    log_lines = []
    original_stdout = sys.stdout

    # Throttled live-flush so the workflow detail view shows progress during the
    # run (dbt column-lineage alone can take many minutes) instead of staying
    # empty until the finally block writes log_output.
    _last_flush = [0.0]

    def log(msg):
        log_lines.append(str(msg))
        original_stdout.write(str(msg) + '\n')
        now = time.monotonic()
        if now - _last_flush[0] >= 2.0:
            _last_flush[0] = now
            try:
                wf.log_output = '\n'.join(log_lines)
                wf.save(update_fields=['log_output'])
            except Exception:
                pass

    def raise_if_cancelled(wf, active_log=None):
        """
        Cooperative cancellation checkpoint.

        The API marks the WorkflowRun as failed and sets a cache flag. Long-running
        extraction/loading code cannot always be interrupted mid-call, but every
        checkpoint stops the workflow before moving to the next pipeline step and
        prevents a killed run from being overwritten as successful later.
        """
        if _is_workflow_cancelled(wf.id):
            log(f'\n[{timezone.now().isoformat()}] ⏹ Workflow cancellation requested by user.')
            wf.status = 'failed'
            wf.current_stage = 'done'
            wf.finished_at = timezone.now()
            wf.log_output = '\n'.join(log_lines)
            wf.save(update_fields=['status', 'current_stage', 'finished_at', 'log_output'])
            if active_log is not None and getattr(active_log, 'status', None) in ['running', 'queued']:
                active_log.status = 'failed'
                active_log.finished_at = timezone.now()
                active_log.log_output = (getattr(active_log, 'log_output', '') or '') + '\n\n[System] Run killed by user via workflow stop.'
                active_log.save(update_fields=['status', 'finished_at', 'log_output'])
            raise WorkflowCancelled()

    def save_log(wf):
        wf.log_output = '\n'.join(log_lines)
        wf.save(update_fields=['log_output', 'current_stage', 'status'])

    try:
        wf = WorkflowRun.objects.get(pk=workflow_run_id)
    except WorkflowRun.DoesNotExist:
        print(f'[ERROR] WorkflowRun id={workflow_run_id} not found.')
        return 'failed'

    org = wf.organization

    try:
        wf.status = 'running'
        wf.current_stage = 'init'
        wf.save(update_fields=['status', 'current_stage'])

        # ── ① INIT ───────────────────────────────────────────────────────
        log(f'[{timezone.now().isoformat()}] ▶ Starting workflow pipeline')
        log(f'  Organization : {org.name}')
        log(f'  Triggered by : {triggered_by}')
        log('')

        # Run transformation sources before visualization sources — the
        # warehouse must be reshaped (dbt etc.) before BI tools (PowerBI)
        # read it. Within each category we keep the model's default ordering
        # (alphabetical by name) for stable, predictable runs.
        _CATEGORY_ORDER = {
            IntegrationSource.CATEGORY_TRANSFORMATION: 0,
            IntegrationSource.CATEGORY_VISUALIZATION: 1,
        }
        sources = sorted(
            IntegrationSource.objects.filter(organization=org, is_active=True),
            key=lambda s: (_CATEGORY_ORDER.get(s.category, 99), s.name),
        )
        destinations = list(IntegrationDestination.objects.filter(organization=org, is_active=True))
        raw_export = WorkflowRawExport.objects.filter(organization=org).first()

        log(f'  Active sources      : {len(sources)}')
        if sources:
            transformation_count = sum(1 for s in sources if s.category == IntegrationSource.CATEGORY_TRANSFORMATION)
            visualization_count = len(sources) - transformation_count
            log(f'    • transformation : {transformation_count}')
            log(f'    • visualization  : {visualization_count}')
        log(f'  Active destinations : {len(destinations)}')
        log('')
        raise_if_cancelled(wf)

        # ── ② SOURCES ────────────────────────────────────────────────────
        wf.current_stage = 'sources'
        save_log(wf)

        source_results = {}
        current_category = None
        skip_visualization = False
        for source in sources:
            raise_if_cancelled(wf)

            # Barrier between categories: log a clear marker so it's obvious
            # in the run output that transformation finished before
            # visualization starts. If any transformation source failed, skip
            # the visualization stage entirely — running BI extracts on stale
            # warehouse data masks problems and confuses downstream users.
            if source.category != current_category:
                if current_category == IntegrationSource.CATEGORY_TRANSFORMATION:
                    failed_in_prev = [n for n, r in source_results.items() if r == 'failed']
                    if failed_in_prev:
                        log(f'\n[{timezone.now().isoformat()}] ❌ Transformation stage failed: {failed_in_prev}. '
                            f'Skipping visualization stage to avoid running BI extracts on stale data.')
                        skip_visualization = True
                    else:
                        log(f'\n[{timezone.now().isoformat()}] ✅ Transformation stage finished. Starting visualization stage.')
                log(f'\n{"#"*60}')
                log(f'### Stage: {source.category.upper()}')
                log(f'{"#"*60}')
                current_category = source.category

            if skip_visualization and source.category == IntegrationSource.CATEGORY_VISUALIZATION:
                log(f'\n[{timezone.now().isoformat()}] ⏭ Skipped {source.name} ({source.source_type}) — transformation stage failed.')
                source_results[source.name] = 'skipped'
                continue

            log(f'\n{"="*60}')
            log(f'[{timezone.now().isoformat()}] ▶ Running source: {source.name} ({source.source_type})')
            log(f'{"="*60}')

            run_log = SourceRunLog.objects.create(
                source=source, status='running', triggered_by=f'workflow:{triggered_by}',
            )

            try:
                from etl.sources.registry import get_source
                src = get_source(source)

                # Use registry-provided ETL dir and load command (fully abstract)
                etl_dir = src.__class__.get_etl_dir()
                src.extract(etl_dir=etl_dir, log=log)
                raise_if_cancelled(wf, run_log)

                # Optional: zip + upload this source's pre-transform raw API
                # output to GCS so it can be replayed through the transform later.
                if raw_export and raw_export.is_active:
                    try:
                        upload_source_raw_to_gcs(
                            raw_export=raw_export,
                            org=org,
                            source=source,
                            raw_dirs=src.__class__.get_raw_dirs(etl_dir),
                            log=log,
                        )
                    except Exception as e:
                        log(f'  [Raw Export] Unexpected error for {source.name}: {e}')

                log(f'\n[{timezone.now().isoformat()}] Loading data into Django database...')
                out = io.StringIO()
                call_command(src.load_command, organization_id=org.id,
                             source_id=source.pk, stdout=out)
                log(out.getvalue())
                raise_if_cancelled(wf, run_log)

                run_log.status = 'success'
                source_results[source.name] = 'success'
                log(f'\n[{timezone.now().isoformat()}] ✅ Source {source.name} completed successfully.')

            except WorkflowCancelled:
                run_log.status = 'failed'
                source_results[source.name] = 'failed'
                log(f'\n[{timezone.now().isoformat()}] ⏹ Source {source.name} stopped because workflow was killed.')
                raise

            except Exception as e:
                run_log.status = 'failed'
                source_results[source.name] = 'failed'
                log(f'\n[{timezone.now().isoformat()}] ❌ Source {source.name} FAILED: {str(e)}')
                log(traceback.format_exc())

            finally:
                run_log.finished_at = timezone.now()
                run_log.log_output = '\n'.join(log_lines)
                run_log.save()
                send_slack_alert(source, run_log)

        raise_if_cancelled(wf)

        # ── ③ FINAL STEP ─────────────────────────────────────────────────
        wf.current_stage = 'final'
        save_log(wf)

        log(f'\n{"="*60}')
        log(f'[{timezone.now().isoformat()}] ▶ Running final step (bridges + summary)')
        log(f'{"="*60}')

        out = io.StringIO()
        call_command('run_workflow_final', organization_id=org.id, stdout=out)
        log(out.getvalue())
        raise_if_cancelled(wf)
        log(f'[{timezone.now().isoformat()}] ✅ Final step complete.')

        # ── ④ DESTINATIONS ───────────────────────────────────────────────
        wf.current_stage = 'destinations'
        save_log(wf)

        for dest in destinations:
            raise_if_cancelled(wf)
            log(f'\n{"="*60}')
            log(f'[{timezone.now().isoformat()}] ▶ Pushing to destination: {dest.name} ({dest.destination_type})')
            log(f'{"="*60}')

            dest_log = DestinationRunLog.objects.create(
                destination=dest, status='running', triggered_by=f'workflow:{triggered_by}',
            )

            try:
                from etl.destinations.bigquery.push_to_bigquery import push_to_bigquery
                push_to_bigquery(dest, None, log)
                raise_if_cancelled(wf, dest_log)
                dest_log.status = 'success'
                log(f'\n[{timezone.now().isoformat()}] ✅ Destination {dest.name} completed.')

            except WorkflowCancelled:
                dest_log.status = 'failed'
                log(f'\n[{timezone.now().isoformat()}] ⏹ Destination {dest.name} stopped because workflow was killed.')
                raise

            except Exception as e:
                dest_log.status = 'failed'
                log(f'\n[{timezone.now().isoformat()}] ❌ Destination {dest.name} FAILED: {str(e)}')
                log(traceback.format_exc())

            finally:
                dest_log.finished_at = timezone.now()
                dest_log.log_output = '\n'.join(log_lines)
                dest_log.save()

                duration_secs = (
                    int((dest_log.finished_at - dest_log.started_at).total_seconds())
                    if dest_log.finished_at and dest_log.started_at else None
                )
                send_slack_dest_alert(dest, dest_log.status, duration_secs)

        # ── DONE ──────────────────────────────────────────────────────────
        any_failed = any(v == 'failed' for v in source_results.values())
        wf.status = 'failed' if any_failed else 'success'
        wf.current_stage = 'done'
        log(f'\n[{timezone.now().isoformat()}] {"⚠️ Workflow completed with failures." if any_failed else "✅ Workflow completed successfully."}')

    except WorkflowCancelled:
        wf.status = 'failed'
        wf.current_stage = 'done'
        log(f'\n[{timezone.now().isoformat()}] ⏹ Workflow killed by user.')

    except Exception as e:
        wf.status = 'failed'
        log(f'\n[{timezone.now().isoformat()}] ❌ Workflow FAILED: {str(e)}')
        log(traceback.format_exc())

    finally:
        # Always remove locally downloaded ETL files (raw PowerBI/Fabric
        # definitions + transformed CSVs), even if the workflow was cancelled
        # or failed — otherwise the device slowly fills up with leftover
        # downloads from every interrupted run.
        _cleanup_local_files(log)

        wf.finished_at = timezone.now()
        wf.log_output = '\n'.join(log_lines)
        wf.save()
        cache.delete(_workflow_cancel_key(workflow_run_id))

        # Update workflow schedule last_run_at
        try:
            from catalog.models import WorkflowSchedule
            sched = WorkflowSchedule.objects.get(organization=org)
            sched.last_run_at = timezone.now()
            sched.save(update_fields=['last_run_at'])
        except Exception:
            pass

        # Clean up old workflow runs (keep last 20)
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=30)
        old_runs = WorkflowRun.objects.filter(organization=org, started_at__lt=cutoff)
        count = old_runs.count()
        if count > 0:
            old_runs.delete()
            log(f'[Cleanup] Deleted {count} old workflow runs.')

    return wf.status


def _get_dbt_etl_dir():
    """Return the absolute path to the dbt ETL directory."""
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', 'etl', 'sources', 'dbt')
    )
