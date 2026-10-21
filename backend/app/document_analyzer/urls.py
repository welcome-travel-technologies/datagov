from django.urls import path, re_path

from . import evaluation, internal, page_gate, views

urlpatterns = [
    # luccy's own chat page and its routes, behind DataGov's sign-in (page_gate).
    path('luccy/', page_gate.gate),
    re_path(r'^luccy/(?P<path>.+)$', page_gate.gate),
    path('status/', views.status),
    # Bearer-token machine endpoints: luccy's sync job and the comparison harness.
    path('internal/documents/', internal.documents),
    path('internal/documents/digest/', internal.digest),
    path('internal/eval/answer/', evaluation.eval_answer),
    path('internal/eval/runs/', evaluation.eval_runs),
]
