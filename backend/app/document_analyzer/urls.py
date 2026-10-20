from django.urls import path

from . import evaluation, internal, views

urlpatterns = [
    path('status/', views.status),
    path('ask/stream/', views.ask_stream),
    path('ask/stream/<str:job>/', views.ask_stream_attach),
    path('documents/<str:doc_id>/', views.document),
    path('feedback/', views.feedback),
    # Bearer-token machine endpoints: luccy's sync job and the comparison harness.
    path('internal/documents/', internal.documents),
    path('internal/documents/digest/', internal.digest),
    path('internal/eval/answer/', evaluation.eval_answer),
    path('internal/eval/runs/', evaluation.eval_runs),
]
