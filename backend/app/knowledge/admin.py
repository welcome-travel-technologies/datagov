from django.contrib import admin
from .models import Document, DocumentVersion, IngestionRun, KnowledgeSource

class InspectOnlyAdmin(admin.ModelAdmin):
    """Lifecycle changes must go through the API's leases and publication checks."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register([KnowledgeSource, Document, DocumentVersion, IngestionRun], InspectOnlyAdmin)
