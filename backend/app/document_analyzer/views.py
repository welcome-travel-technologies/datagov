"""Document Analyzer endpoints DataGov serves itself: the page's status, and the helpers the
chat tool and the comparison share (conversation ids, earlier-turn bounds). luccy's own page
and its routes are relayed by ``page_gate``.

luccy keeps a thread's memory by conversation id alone, so DataGov never forwards an id it
did not make: the web chat's is derived from its session (``chat_conversation``) and keyed
with ``SECRET_KEY`` so nobody can name another person's thread. Ids starting with "dg" are
DataGov's own.
"""
import hashlib
import hmac
import re

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import client, permissions

MAX_HISTORY_TURNS = 50
MAX_TURN_CHARS = 16000
MAX_HISTORY_CHARS = 64000
CONVERSATION_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')
# DataGov's own conversation ids: "dg<kind>-" and 40 hex characters (at most 47).
RESERVED_CONVERSATION_PREFIX = 'dg'
CONVERSATION_DIGEST_CHARS = 40
# luccy's own rule (a leading letter or digit, never ".."), so no id is a dot segment.
DOC_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,299}')


def conversation_id(kind, *parts):
    """``dg<kind>-`` + the first 40 hex of HMAC-SHA256(SECRET_KEY, "<kind>:<parts>")."""
    message = ':'.join([kind, *(str(part) for part in parts)]).encode('utf-8')
    digest = hmac.new(settings.SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return f'{RESERVED_CONVERSATION_PREFIX}{kind}-{digest[:CONVERSATION_DIGEST_CHARS]}'


def chat_conversation(session):
    """The web chat session's luccy conversation id."""
    return conversation_id('chat', session.pk)


def bounded_history(turns, cap=MAX_HISTORY_TURNS):
    """Earlier turns ``[{"who", "text"}]`` held to luccy's request bounds: each cut to
    ``MAX_TURN_CHARS``, the last ``cap`` kept, then the oldest dropped until they hold at
    most ``MAX_HISTORY_CHARS`` in all."""
    out = [{'who': turn['who'], 'text': turn['text'][:MAX_TURN_CHARS]} for turn in turns]
    out = out[-cap:] if cap > 0 else []
    while out and sum(len(turn['text']) for turn in out) > MAX_HISTORY_CHARS:
        out.pop(0)
    return out


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def status(request):
    org, code, reason = permissions.evaluate(request.user)
    available = code == 200
    tool_available = False
    if available:
        from catalog.services.assistant_tools import member_policy
        tool_available = bool(member_policy(org, request.user)['analyzer'])
    return Response({
        'enabled': permissions.enabled(),
        'configured': permissions.configured(),
        'available': available,
        # Probed only for people who may use it; null otherwise.
        'healthy': client.healthy() if available else None,
        'reason': reason,
        'sources': list(permissions.SOURCES),
        'tool_available': tool_available,
    })
