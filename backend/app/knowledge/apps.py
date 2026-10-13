from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge'
    verbose_name = 'Document Explorer'

    def ready(self):
        from django.db.models.signals import post_migrate
        from .scheduling import ensure_schedule
        post_migrate.connect(ensure_schedule, sender=self, dispatch_uid='document_explorer_schedule')
