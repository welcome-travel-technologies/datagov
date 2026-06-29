"""
Precise verification that the Pydantic-AI chatbot's ASYNC execution path works
with the new React frontend's `/api/chat/` contract.

The React app's flow is:
    POST /api/chat/                      -> {session_id, task_id}   (enqueues a Django-Q task)
    GET  /api/chat/task/<task_id>/       -> {status, current_status}  (poll)
    GET  /api/chat/sessions/<id>/messages/ -> [{role, content}, ...]   (read result)

The interesting async concern is the worker: pydantic-ai's `Agent` is async, but
Django-Q runs tasks in a *sync* thread. `run_chat_event_sync` bridges them via
`agent.run_sync()` inside a `ThreadPoolExecutor` (hard-timeout). These tests
exercise that bridge with pydantic-ai's offline `FunctionModel` so they're
deterministic and need no LLM key.

Run with sqlite:  DEBUG=True pytest catalog/tests/test_chat_async.py
"""
import pytest
from django.test import override_settings

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from catalog import views
from catalog.models import ChatSession, ChatMessage, CustomUser

# Use an in-process cache so the chat session lock (cache.add) doesn't need the
# DatabaseCache table, which isn't created in the test database.
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _canned_agent(text="PONG"):
    """A real pydantic-ai Agent whose model replies offline with fixed text."""
    def _reply(messages, info):
        return ModelResponse(parts=[TextPart(content=text)])
    return Agent(model=FunctionModel(_reply))


@pytest.fixture
def chat_user(db):
    return CustomUser.objects.create_user(
        username="chatter", email="chatter@example.com", password="testpass",
    )


@override_settings(CACHES=LOCMEM)
@pytest.mark.django_db
def test_worker_runs_agent_sync_and_persists_assistant_message(chat_user, monkeypatch):
    """The worker must run the async agent under a sync thread (agent.run_sync
    inside ThreadPoolExecutor) and persist the reply — no event-loop error."""
    session = ChatSession.objects.create(user=chat_user, title="t")
    ChatMessage.objects.create(session=session, role="user", content="ping")

    monkeypatch.setattr(views, "build_chatbot_agent_for_org",
                        lambda *a, **k: _canned_agent("PONG"))

    out = views.run_chat_event_sync(session.id, "ping")

    assert out == "PONG"
    assistant = session.messages.filter(role="assistant").order_by("created_at").last()
    assert assistant is not None
    assert assistant.content == "PONG"


@override_settings(CACHES=LOCMEM)
@pytest.mark.django_db
def test_worker_persists_graceful_error_when_agent_fails(chat_user, monkeypatch):
    """A failing agent must never hang the queue: the worker catches and writes
    a graceful assistant message so the frontend's poll terminates."""
    session = ChatSession.objects.create(user=chat_user, title="t")
    ChatMessage.objects.create(session=session, role="user", content="ping")

    def _boom(*a, **k):
        raise RuntimeError("model exploded")
    monkeypatch.setattr(views, "build_chatbot_agent_for_org", _boom)

    views.run_chat_event_sync(session.id, "ping")

    assistant = session.messages.filter(role="assistant").order_by("created_at").last()
    assert assistant is not None
    assert assistant.content.startswith("Sorry, I encountered an error")
    assert "model exploded" in assistant.content


@override_settings(CACHES=LOCMEM)
@pytest.mark.django_db
def test_chat_api_enqueues_task_and_creates_user_message(chat_user, monkeypatch):
    """POST /api/chat/ is the React entry point: it must persist the user
    message and enqueue run_chat_event_sync, returning {session_id, task_id}."""
    captured = {}

    def _fake_async_task(path, *args, **kwargs):
        captured["path"] = path
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "fake-task-id"

    monkeypatch.setattr(views, "async_task", _fake_async_task)

    from rest_framework.test import APIClient
    client = APIClient()
    client.force_authenticate(user=chat_user)

    resp = client.post("/api/chat/", {"message": "hello"}, format="json")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("task_id") == "fake-task-id"
    assert "session_id" in body

    session = ChatSession.objects.get(id=body["session_id"])
    assert session.messages.filter(role="user", content="hello").exists()
    assert captured["path"] == "catalog.views.run_chat_event_sync"
    assert captured["args"][0] == session.id
    assert captured["args"][1] == "hello"
    assert captured["kwargs"].get("timeout") == 300


@override_settings(CACHES=LOCMEM)
@pytest.mark.django_db
def test_chat_messages_endpoint_returns_frontend_shape(chat_user):
    """GET messages returns [{role, content}] with the assistant role mapped to
    something the React page renders as a non-user bubble."""
    session = ChatSession.objects.create(user=chat_user, title="t")
    ChatMessage.objects.create(session=session, role="user", content="hi")
    ChatMessage.objects.create(session=session, role="assistant", content="hello there")

    # This endpoint uses Django's @login_required (session auth), which the
    # React app satisfies via the session cookie flowing through the Next proxy.
    from django.test import Client
    client = Client()
    client.force_login(chat_user)

    resp = client.get(f"/api/chat/sessions/{session.id}/messages/")
    assert resp.status_code == 200
    msgs = resp.json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hi"
    # the React page treats any non-"user" role as a model bubble
    assert msgs[1]["role"] != "user"
    assert msgs[1]["content"] == "hello there"
