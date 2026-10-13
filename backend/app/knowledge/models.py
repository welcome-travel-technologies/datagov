import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class KnowledgeSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('catalog.Organization', on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=24, choices=[('upload', 'Uploads'), ('snapshot', 'Snapshot')])
    config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    access = models.CharField(max_length=20, choices=[('organization', 'Organization'), ('restricted', 'Restricted')], default='restricted')
    allowed_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True)
    allowed_groups = models.ManyToManyField('auth.Group', blank=True)
    cron = models.CharField(max_length=120, blank=True, default='')
    timezone = models.CharField(max_length=80, default='UTC')
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'id']
        constraints = [models.UniqueConstraint(fields=['organization', 'name'], name='knowledge_source_org_name')]


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(KnowledgeSource, on_delete=models.PROTECT, related_name='documents')
    external_id = models.CharField(max_length=500)
    title = models.CharField(max_length=500)
    mime_type = models.CharField(max_length=150)
    source_type = models.CharField(max_length=40, default='upload')
    source_url = models.URLField(max_length=2000, blank=True, default='')
    current_version = models.ForeignKey('DocumentVersion', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    deleted_at = models.DateTimeField(null=True, blank=True)
    deletion_reason = models.CharField(max_length=24, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', 'id']
        constraints = [models.UniqueConstraint(fields=['source', 'external_id'], name='knowledge_document_identity')]


class DocumentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='versions')
    content_hash = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=300, blank=True, default='')
    size = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=20, default='queued', choices=[(s, s) for s in ['queued', 'processing', 'ready', 'needs_ocr', 'failed']])
    pages = models.JSONField(default=list, blank=True)
    chunks = models.JSONField(default=list, blank=True)
    error = models.CharField(max_length=1000, blank=True, default='')
    index_generation = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', 'id']
        constraints = [models.UniqueConstraint(fields=['document', 'content_hash'], name='knowledge_version_content')]


class IngestionRun(models.Model):
    ACTIVE = ['queued', 'running', 'cancel_requested']
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(KnowledgeSource, on_delete=models.PROTECT, related_name='runs')
    versions = models.ManyToManyField(DocumentVersion, blank=True)
    configuration = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=200)
    status = models.CharField(max_length=24, default='queued', choices=[(s, s) for s in ACTIVE + ['succeeded', 'partial', 'failed', 'cancelled']])
    stage = models.CharField(max_length=80, default='queued')
    progress = models.PositiveSmallIntegerField(default=0)
    counters = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=1000, blank=True, default='')
    worker_id = models.CharField(max_length=200, blank=True, default='')
    result_hash = models.CharField(max_length=64, blank=True, default='')
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempt = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['source', 'idempotency_key'], name='knowledge_run_idempotency'),
            models.UniqueConstraint(fields=['source'], condition=Q(status__in=['queued', 'running', 'cancel_requested']), name='knowledge_one_active_run'),
        ]
