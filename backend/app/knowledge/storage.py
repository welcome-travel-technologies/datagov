from django.conf import settings
from django.core.files.storage import FileSystemStorage


def private_storage():
    return FileSystemStorage(location=settings.DOCUMENT_EXPLORER_STORAGE_ROOT, base_url=None)
