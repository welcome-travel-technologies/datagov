from django.apps import AppConfig


class DocumentAnalyzerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'document_analyzer'
    verbose_name = 'Document Analyzer'

    def ready(self):
        from . import permissions
        if permissions.enabled():
            # Weak or shared service tokens are logged at start, not only on first use.
            for name in permissions.TOKEN_SETTINGS:
                permissions.token_problem(name)
