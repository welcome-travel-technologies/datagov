from django.urls import path
from . import views

urlpatterns = [
    path('status/', views.status),
    path('sources/', views.sources),
    path('sources/<uuid:source_id>/', views.source_detail),
    path('sources/<uuid:source_id>/sync/', views.sync_source),
    path('uploads/', views.upload),
    path('documents/', views.documents),
    path('documents/<uuid:document_id>/', views.document_detail),
    path('documents/<uuid:document_id>/content/', views.document_content),
    path('runs/', views.runs),
    path('runs/<uuid:run_id>/', views.run_detail),
    path('runs/<uuid:run_id>/<str:action>/', views.run_detail),
    path('internal/jobs/claim/', views.claim_job),
    path('internal/jobs/<uuid:run_id>/heartbeat/', views.heartbeat),
    path('internal/jobs/<uuid:run_id>/complete/', views.complete_job),
    path('internal/jobs/<uuid:run_id>/fail/', views.fail_job),
    path('internal/jobs/<uuid:run_id>/documents/', views.register_document),
    path('internal/jobs/<uuid:run_id>/documents/<uuid:version_id>/content/', views.worker_content),
]
