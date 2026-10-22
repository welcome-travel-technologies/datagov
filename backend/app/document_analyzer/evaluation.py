"""The luccy/DataGov comparison: answer one question as DataGov's chat would, and keep the runs.

``POST internal/eval/answer/`` and ``POST internal/eval/runs/`` take the bearer token
``LUCCY_EVAL_TOKEN`` and exist only while the analyzer is on and set up
(``LUCCY_ENABLED``, ``permissions.configured``), ``LUCCY_EVAL_ENABLED`` is on and the
token is set and usable (``permissions.token_problem``; 404 otherwise, 401 for a wrong
token). Answers run as
``LUCCY_EVAL_USER_ID``, who must be an active member of ``LUCCY_ORGANIZATION_ID`` (503
otherwise). The one arm, ``chat_analyzer_only``, is the real web-chat agent
(``catalog.views.run_agent_turn``, the chat worker's own run and finalization, verbatim
analyzer answers included) with the tool policy forced to the Document Analyzer alone
for this one call. Its luccy calls may take ``LUCCY_STREAM_TIMEOUT`` (as on the page)
and the turn 60 s more. Evaluation calls never append the organization's debug section.

Evaluation calls never create a ``ChatSession`` or ``ChatMessage``: they call the
worker's turn function directly, with no session and no earlier turns, so there is
nothing to roll back or delete. The forced policy is an argument, never saved. An
analyzer question asked here does not count against the eval user's hourly cap. The
``conversation`` field becomes luccy's conversation id (ids starting with "dg" are
DataGov's own and refused).

The answer reply used by the private comparison runner::

    {"answer", "judged_answer", "citations", "abstained", "gaps", "tool_calls",
     "verbatim", "tool_error", "error", "latency_s", "cost_usd", "cost_source",
     "cost_detail"}

- ``answer``: the reply as DataGov would show it (a verbatim analyzer answer with its
  sources list and footer); ``judged_answer``: the text every arm is judged on (luccy's
  raw answer for a verbatim reply, else the reply itself, never a debug section);
- ``tool_error``: the analyzer was called but no answer of it came back (a failure,
  timeout, busy luccy or spend cap), so the reply is the agent's own;
- ``cost_usd`` / ``cost_source``: ``"measured"`` when every part is priced: luccy's
  answers from their run records (``GET /records/<log>``, ``cost.usd``, free) and the
  agent's model calls priced with genai-prices (pydantic-ai's ``ModelResponse.cost``);
  ``"estimate"`` when only some parts are, and ``cost_usd`` then leaves the rest out;
  both null when nothing is priced.
  ``cost_detail`` says which part is which: ``{"luccy_usd", "agent_usd",
  "agent_models", "agent_input_tokens", "agent_output_tokens", "note"}``.

Comparison runs stay private: there are no browser list/detail endpoints.
``params``, ``summary`` and ``rows`` are stored as the protected runner sent them,
including full golden-set metadata. A run id matches ``RUN_ID_RE`` and holds no
".."; one the harness leaves out is made as
``run-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>``.
"""
import json
import logging
import math
import re
import secrets
import time
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.exceptions import APIException, ParseError
from rest_framework.parsers import BaseParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from catalog.models import Organization, OrganizationMembership

from . import assistant, client, permissions
from .internal import bearer_denied, not_found
from .models import RUN_STATUSES, AnalyzerComparisonRun
from .views import CONVERSATION_RE, RESERVED_CONVERSATION_PREFIX

logger = logging.getLogger(__name__)

ARMS = ('chat_analyzer_only',)
RUN_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}')
MAX_RUN_BYTES = 2 * 1024 * 1024
MAX_ANSWER_REQUEST_BYTES = 64 * 1024
MAX_QUESTION_CHARS = 10000
MAX_ERROR_CHARS = 500
# The chat arm's turn may run this much longer than its luccy call (LUCCY_STREAM_TIMEOUT).
EVAL_TURN_EXTRA_SECONDS = 60
RECORD_TIMEOUT = 30


class TooLarge(APIException):
    status_code = 413
    default_detail = 'The request body is too large.'


class Unavailable(Exception):
    """An arm the eval user cannot run; its text says what to set up."""


def bounded_json(limit):
    """A JSON-object parser that refuses a body over ``limit`` bytes (413) without
    reading more than that."""
    class Parser(BaseParser):
        media_type = 'application/json'

        def parse(self, stream, media_type=None, parser_context=None):
            payload = stream.read(limit + 1) if stream is not None else b''
            if len(payload) > limit:
                raise TooLarge(f'Send at most {limit} bytes.')
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                raise ParseError('Invalid JSON.') from None
            if not isinstance(data, dict):
                raise ParseError('Send a JSON object.')
            return data
    return Parser


def _eval_gate(request):
    token = permissions.service_token('LUCCY_EVAL_TOKEN')
    if not (permissions.enabled() and permissions.configured()
            and getattr(settings, 'LUCCY_EVAL_ENABLED', False) and token):
        return not_found()
    return bearer_denied(request, token)


def _bound_organization():
    org_id = permissions.organization_id()
    return Organization.objects.filter(pk=org_id).first() if org_id is not None else None


def eval_identity():
    """``(organization, user)`` the evaluation answers as; ValueError in plain words
    when that is not set up."""
    org = _bound_organization()
    if org is None:
        raise ValueError('LUCCY_ORGANIZATION_ID is not set to an existing organization.')
    raw = str(getattr(settings, 'LUCCY_EVAL_USER_ID', '') or '').strip()
    membership = None
    if raw.isascii() and raw.isdigit():
        membership = OrganizationMembership.objects.filter(
            organization=org, user_id=int(raw), user__is_active=True).select_related('user').first()
    if membership is None:
        raise ValueError(f'LUCCY_EVAL_USER_ID must be the id of an active member of organization {org.pk}.')
    return org, membership.user


def _only(group):
    from catalog.services.assistant_tools import TOOL_GROUPS
    return {key: key == group for key in TOOL_GROUPS}


# ---- cost ----------------------------------------------------------------------------------------


def _usd(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def luccy_record_usd(user, ref):
    """luccy's own cost of one answer: its run record's ``cost.usd`` (a free bearer
    call); None when the record or its cost is missing."""
    try:
        record = client.request_json('GET', f'/records/{ref}', user, read_timeout=RECORD_TIMEOUT, not_found=True)
    except client.UpstreamError:
        return None
    cost = record.get('cost') if isinstance(record.get('cost'), dict) else {}
    return _usd(cost.get('usd'))


def agent_model_cost(messages):
    """``(usd, detail)`` for the agent's own model calls in ``messages``, priced with
    genai-prices through pydantic-ai's ``ModelResponse.cost``. ``usd`` is None when the
    run is unknown (cut short) or any call's model has no known price."""
    from pydantic_ai.messages import ModelResponse
    detail = {'agent_models': [], 'agent_input_tokens': None, 'agent_output_tokens': None}
    if messages is None:
        return None, detail
    usd, priced, inputs, outputs = 0.0, True, 0, 0
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        if message.model_name and message.model_name not in detail['agent_models']:
            detail['agent_models'].append(message.model_name)
        inputs += message.usage.input_tokens or 0
        outputs += message.usage.output_tokens or 0
        try:
            price = _usd(message.cost().total_price)
        except Exception:  # LookupError: no price for this model; AssertionError: no model name.
            price = None
        if price is None:
            priced = False
        else:
            usd += price
    detail['agent_input_tokens'], detail['agent_output_tokens'] = inputs, outputs
    return (usd if priced else None), detail


def cost_report(parts, detail, notes):
    """The reply's cost fields from ``parts`` ``{name: (usd or None, complete)}``:
    measured when every part is priced and complete, an estimate (of the priced parts
    only) when some are, and nulls when none is."""
    known = [usd for usd, _complete in parts.values() if usd is not None]
    if not known:
        cost, source = None, None
    else:
        cost = round(sum(known), 6)
        source = 'measured' if all(usd is not None and complete for usd, complete in parts.values()) else 'estimate'
    return {'cost_usd': cost, 'cost_source': source,
            'cost_detail': {'luccy_usd': None, 'agent_usd': None, 'agent_models': [], 'agent_input_tokens': None,
                            'agent_output_tokens': None, **detail, 'note': ' '.join(notes) if notes else None}}


def _agent_part(messages, notes):
    usd, detail = agent_model_cost(messages)
    if usd is None:
        names = ', '.join(detail['agent_models']) or 'unknown'
        notes.append(f"DataGov cannot price the agent's model calls (model: {names}); cost_usd leaves them out."
                     if messages is not None else "The agent's run was cut short, so its model cost is not known.")
    return usd, {**detail, 'agent_usd': None if usd is None else round(usd, 6)}


def _chat_cost(user, turn, messages):
    notes = []
    agent_usd, detail = _agent_part(messages, notes)
    luccy_usd, complete = 0.0, turn.unpriced_calls == 0
    for ref in turn.refs():
        usd = luccy_record_usd(user, ref)
        if usd is None:
            complete = False
        else:
            luccy_usd += usd
    if not complete:
        notes.append('Some Document Analyzer calls have no cost record; cost_usd leaves them out.')
    detail['luccy_usd'] = round(luccy_usd, 6)
    return cost_report({'luccy': (luccy_usd, complete), 'agent': (agent_usd, True)}, detail, notes)


# ---- arms ----------------------------------------------------------------------------------------


def _chat_analyzer_only(org, user, question, conversation):
    from catalog import views as chat
    if assistant.tool_for(org, user) is None:
        raise Unavailable('The eval user cannot use the Document Analyzer: it must be on and set up, and the user '
                          'an organization admin or in the Analytics group.')
    timeout = int(settings.LUCCY_STREAM_TIMEOUT)
    turn = assistant.ChatTurn(question, conversation=conversation, capped=False, tool_timeout=timeout)
    outcome = chat.run_agent_turn(org, user, question, [], analyzer_turn=turn, tool_policy=_only('analyzer'),
                                  timeout_seconds=timeout + EVAL_TURN_EXTRA_SECONDS, debug_section=False)
    last = turn.last_answer() if outcome['verbatim'] else None
    return {'answer': outcome['content'], 'judged_answer': last['answer'] if last else outcome['content'],
            'citations': turn.citations(outcome['verbatim']),
            'abstained': bool(last and last['abstained']), 'gaps': list(last['gaps']) if last else [],
            'tool_calls': outcome['tool_calls'], 'verbatim': outcome['verbatim'],
            'tool_error': assistant.TOOL_NAME in outcome['tool_calls'] and not outcome['verbatim'],
            '_cost': lambda: _chat_cost(user, turn, outcome['run_messages'])}


RUNNERS = {'chat_analyzer_only': _chat_analyzer_only}


def run_arm(arm, org, user, question, conversation=None):
    """One answer by ``arm`` in the evaluation's reply shape (module docstring); a failure
    becomes ``error``. The cost is looked up after ``latency_s`` is taken."""
    started = time.monotonic()
    result = {'answer': '', 'judged_answer': '', 'citations': [], 'abstained': False, 'gaps': [], 'tool_calls': [],
              'verbatim': False, 'tool_error': False, 'error': None,
              'cost_usd': None, 'cost_source': None, 'cost_detail': None}
    cost = None
    try:
        outcome = RUNNERS[arm](org, user, question, conversation)
        cost = outcome.pop('_cost', None)
        result.update(outcome)
    except Unavailable as exc:
        result['error'] = str(exc)[:MAX_ERROR_CHARS]
    except Exception as exc:
        logger.warning('Document Analyzer evaluation arm %s failed (%s).', arm, type(exc).__name__)
        result['error'] = f'{type(exc).__name__}: {exc}'[:MAX_ERROR_CHARS]
    result['latency_s'] = round(time.monotonic() - started, 3)
    if cost is not None:
        try:
            result.update(cost())
        except Exception as exc:
            logger.warning('Document Analyzer evaluation cost of arm %s failed (%s).', arm, type(exc).__name__)
    return result


def _bad(detail):
    return Response({'detail': detail}, status=400)


def valid_run_id(value):
    return isinstance(value, str) and bool(RUN_ID_RE.fullmatch(value)) and '..' not in value


def new_run_id():
    """``run-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>``, for a run uploaded without an id."""
    return f"run-{datetime.now(dt_timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([bounded_json(MAX_ANSWER_REQUEST_BYTES)])
def eval_answer(request):
    denied = _eval_gate(request)
    if denied:
        return denied
    data = request.data if isinstance(request.data, dict) else {}
    arm, question, conversation = data.get('arm'), data.get('question'), data.get('conversation')
    if not isinstance(arm, str) or arm not in ARMS:
        return _bad(f'Choose an arm from: {", ".join(ARMS)}.')
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        return _bad(f'Ask a question of 1 to {MAX_QUESTION_CHARS} characters.')
    if conversation in (None, ''):
        conversation = None
    elif not isinstance(conversation, str) or not CONVERSATION_RE.fullmatch(conversation):
        return _bad('The conversation id is 1 to 64 letters, digits, "-" or "_".')
    elif conversation[:len(RESERVED_CONVERSATION_PREFIX)].lower() == RESERVED_CONVERSATION_PREFIX:
        # DataGov's own chat and page threads: the harness must never reach them.
        return _bad(f'Conversation ids starting with "{RESERVED_CONVERSATION_PREFIX}" are reserved.')
    try:
        org, user = eval_identity()
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=503)
    return Response(run_arm(arm, org, user, question, conversation))


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@parser_classes([bounded_json(MAX_RUN_BYTES)])
def eval_runs(request):
    """Create or replace a comparison run. Fields left out of an update keep their value."""
    denied = _eval_gate(request)
    if denied:
        return denied
    data = request.data if isinstance(request.data, dict) else {}
    run_id = data.get('run_id')
    if run_id in (None, ''):
        run_id = None
    elif not valid_run_id(run_id):
        return _bad('The run id is 1 to 128 letters, digits, ".", "_", ":" or "-", starting with a letter or digit.')
    if data.get('status') not in RUN_STATUSES:
        return _bad(f'The status must be one of: {", ".join(RUN_STATUSES)}.')
    fields = {'status': data['status']}
    for name, kind in (('params', dict), ('summary', dict), ('rows', list)):
        if name in data:
            if not isinstance(data[name], kind):
                return _bad(f'"{name}" must be a JSON {"object" if kind is dict else "array"}.')
            fields[name] = data[name]
    org = _bound_organization()
    if org is None:
        return Response({'detail': 'LUCCY_ORGANIZATION_ID is not set to an existing organization.'}, status=503)
    try:
        with transaction.atomic():
            run = (AnalyzerComparisonRun.objects.select_for_update().filter(run_id=run_id).first()
                   if run_id else None)
            if run is not None and run.organization_id != org.pk:
                return Response({'detail': 'This run belongs to another organization.'}, status=409)
            created = run is None
            if created:
                run = AnalyzerComparisonRun(organization=org, run_id=run_id or new_run_id())
            for name, value in fields.items():
                setattr(run, name, value)
            run.save()
    except IntegrityError:
        return Response({'detail': 'This run was created at the same time by another request. Send it again.'},
                        status=409)
    return Response({'run_id': run.run_id}, status=201 if created else 200)
