from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'catalog'

    def ready(self):
        from . import signals  # noqa: F401

        # Register the recurring health check (DB + disk → Slack alert).
        # Idempotent; silently skipped when the DB isn't migrated yet.
        from .health import ensure_monitor_schedule
        ensure_monitor_schedule()
