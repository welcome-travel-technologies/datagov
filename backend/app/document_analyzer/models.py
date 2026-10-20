from django.db import models

RUN_STATUSES = ['running', 'done', 'failed']


class AnalyzerComparisonRun(models.Model):
    """One luccy/DataGov comparison run, uploaded by the comparison harness and read by
    the bound organization's admins. ``rows`` holds the per-question results, ``summary``
    the headline numbers and ``params`` how the run was made; DataGov stores them as sent.
    """
    organization = models.ForeignKey('catalog.Organization', on_delete=models.CASCADE,
                                     related_name='analyzer_comparison_runs')
    run_id = models.CharField(max_length=128, unique=True)
    status = models.CharField(max_length=16, choices=[(s, s) for s in RUN_STATUSES])
    params = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    rows = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['organization', '-created_at'], name='analyzer_run_org_created')]

    def __str__(self):
        return self.run_id
