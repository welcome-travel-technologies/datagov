from django.urls import path, re_path

from . import evaluation, internal, views

urlpatterns = [
    path('status/', views.status),
    path('ask/stream/', views.ask_stream),
    path('ask/stream/<str:job>/', views.ask_stream_attach),
    path('documents/<str:doc_id>/', views.document),
    path('feedback/', views.feedback),
    path('comparisons/', evaluation.comparisons),
    # Only a valid run id (evaluation.RUN_ID_RE, never ".."); anything else is 404.
    re_path(r'^comparisons/(?P<run_id>(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/$', evaluation.comparison),
    # Bearer-token machine endpoints: luccy's sync job and the comparison harness.
    path('internal/documents/', internal.documents),
    path('internal/documents/digest/', internal.digest),
    path('internal/eval/answer/', evaluation.eval_answer),
    path('internal/eval/runs/', evaluation.eval_runs),
]
