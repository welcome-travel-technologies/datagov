import csv
import io
import json
import logging
import re
import traceback
import uuid

logger = logging.getLogger(__name__)

from django.http import JsonResponse, StreamingHttpResponse
from django.core.cache import cache
from django.db.models import Q, Count

from .decorators import api_login_required

from rest_framework import viewsets, filters
from rest_framework.response import Response
from rest_framework.decorators import api_view, action, parser_classes, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated, AllowAny

from django_filters.rest_framework import DjangoFilterBackend
from django_q.tasks import async_task

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai import capture_run_messages
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded

from .tools import get_agent
from .models import (
    Summary, Item, ItemGroup, NetworkNode,
    NetworkEdge, Department, DataPerson, Category, ChatSession, ChatMessage,
    IntegrationSource, SourceRunLog, SourceSchedule,
    IntegrationDestination, DestinationRunLog, DestinationSchedule,
    IntegrationHook, Organization, OrganizationMembership,
    WorkflowRun, WorkflowSchedule, WorkflowRawExport,
    PowerBIReportUsage, GovernanceTask, MetricsMap,
)
from .services.workspaces import get_workspaces_for_source
from .serializers import (
    SummarySerializer, ItemSerializer, ItemGroupSerializer,
    DepartmentSerializer, DataPersonSerializer, CategorySerializer,
    GovernanceTaskSerializer, MetricsMapSerializer, PublicMetricsMapSerializer,
)

def _enrich_with_item_metadata(nodes):
    """Augment ``nodes`` (list of dicts) with workspace and parent info from
    the Item table so the frontend can filter by workspace and prefix column
    labels with their parent table/dataset.

    ``NetworkNode.node_id`` is "{TYPE}::{hash}" and ``Item.item_id`` is the
    hash, so we strip the prefix before the lookup. Items not in the catalog
    (e.g. PAGE / VISUAL / FIELD nodes synthesized only in the graph) are left
    as-is.

    Module-level so both ``get_network`` and ``find_network_path`` can call it.
    """
    hashes = []
    hash_to_node = {}
    for n in nodes:
        nid = n.get('id') or ''
        if '::' in nid:
            h = nid.split('::', 1)[1]
            hashes.append(h)
            hash_to_node[h] = n
    if not hashes:
        return nodes
    chunk = 900
    for i in range(0, len(hashes), chunk):
        rows = Item.objects.filter(item_id__in=hashes[i:i + chunk]).values(
            'item_id', 'workspace_id', 'workspace_name',
            'table_name', 'dataset_name', 'database_name', 'schema_name', 'datatype', 'tags',
        )
        for r in rows:
            node = hash_to_node.get(r['item_id'])
            if not node:
                continue
            if r['workspace_id']:
                node['workspace_id'] = r['workspace_id']
            if r['workspace_name']:
                node['workspace_name'] = r['workspace_name']
            if r['datatype']:
                node['datatype'] = r['datatype']
            if r['tags']:
                node['tags'] = r['tags']
            # Parent label: prefer table for columns, dataset otherwise.
            group = (node.get('group') or '').upper()
            if 'COLUMN' in group:
                parent = r['table_name'] or r['dataset_name']
            else:
                parent = r['dataset_name']
                # Grouping keys for the lineage tree sidebar: dbt assets group by
                # database/schema, PowerBI tables by workspace/dataset.
                if r['database_name']:
                    node['database'] = r['database_name']
                if r['schema_name']:
                    node['schema'] = r['schema_name']
                if r['dataset_name']:
                    node['dataset'] = r['dataset_name']
            if parent:
                node['parent'] = parent
    return nodes


# ── Lineage edge classification ────────────────────────────────────────────────
# The classification rules now live in one place: catalog.services.network_classify.
# Each edge persists its `kind` and `level` (computed there at load time and
# backfilled for legacy rows), so reads filter on indexed columns instead of
# re-deriving from node-id prefixes. These helpers stay as the fallback used
# when serializing an edge whose stored `kind` is NULL.
from .services.network_classify import (
    CONTAINER_TYPES as _CONTAINER_TYPES,
    MEMBER_TYPES as _MEMBER_TYPES,
    classify_node_ids as _classify_node_ids,
    _node_type,
)


def _edge_kind(source, target):
    """Fallback edge classifier from node-id prefixes (see network_classify).

    Returns one of ``contains`` / ``column`` / ``model``. Matches the persisted
    ``NetworkEdge.kind`` exactly; used only for legacy rows where it is NULL.
    """
    return _classify_node_ids(source, target)[0]


def _serialize_node(node):
    """NetworkNode -> graph payload dict ({id, label, group})."""
    return {
        "id": node.node_id,
        "label": node.name or node.node_id,
        "group": node.group or "UNKNOWN",
    }


def serialize_message(msg):
    """Convert a Pydantic AI message to JSON."""
    return json.loads(ModelMessagesTypeAdapter.dump_json([msg]))[0]

def deserialize_messages(data):
    """Convert JSON back to Pydantic AI messages."""
    return ModelMessagesTypeAdapter.validate_python(data)

def _get_org_for_session(session):
    """Return the session user's organization, or None when unavailable."""
    membership = (
        OrganizationMembership.objects
        .filter(user=session.user)
        .select_related('organization')
        .first()
    )
    return membership.organization if membership else None


def _get_powerbi_client_for_org(org):
    """Build the PowerBI *live* REST client — gated on the live tier
    (``powerbi_live_tools_enabled``). The catalog tier needs no client."""
    try:
        if not org or not getattr(org, 'powerbi_live_tools_enabled', False):
            return None
        from .powerbi_client import build_powerbi_client_for_org
        return build_powerbi_client_for_org(org)
    except Exception:
        return None


def _get_bigquery_client_for_org(org):
    """Build the BigQuery client when EITHER BigQuery tier is on — the catalog
    tier needs it to fetch schema, the live tier to run SQL."""
    try:
        if not org or not (getattr(org, 'bigquery_tools_enabled', False)
                           or getattr(org, 'bigquery_live_tools_enabled', False)):
            return None
        from .bigquery_client import build_bigquery_client_for_org
        return build_bigquery_client_for_org(org)
    except Exception:
        return None


def _get_dbt_enabled_for_org(org) -> bool:
    return bool(org and getattr(org, 'dbt_tools_enabled', False))


def _get_powerbi_tools_enabled_for_org(org) -> bool:
    """PowerBI *catalog* tier (front-loaded listing + DB-only profiler/usage
    tools). Defaults to True via ``getattr`` so the assistant works before the
    migration is applied; distinct from the live-DAX ``powerbi_live_tools_enabled``
    gate."""
    return bool(org and getattr(org, 'powerbi_tools_enabled', True))


def _get_bigquery_live_enabled_for_org(org) -> bool:
    """BigQuery *live* tier — whether the read-only SQL tool is registered."""
    return bool(org and getattr(org, 'bigquery_live_tools_enabled', False))


def _get_chatbot_model_for_org(org):
    try:
        if org and org.chatbot_model and org.chatbot_model.is_active:
            return org.chatbot_model.identifier
    except Exception:
        pass
    return None


def build_chatbot_agent_for_org(
    org, user=None, before_tool_call=None, record_call=None, surface='slack',
    chat_session=None,
):
    """Build a Pydantic AI agent scoped to ``org``.

    ``user`` lets per-source workspace defaults override the org-level default;
    Slack callers pass ``user=None`` because the Slack author is not mapped to
    a Django user, so workspace resolution falls back to the org default.

    ``record_call`` is invoked after every tool call with a structured entry;
    callers use it to build the ``debug_meta`` payload persisted with each
    chat message.

    ``surface`` is 'web' or 'slack' (default); it is threaded through for
    back-compat but no longer changes tool behaviour.

    ``chat_session`` is accepted for backwards-compatible callers and unused.

    The front-loaded catalog context is scoped to the org's selected PowerBI
    workspaces / BigQuery datasets (``assistant_powerbi_workspace_ids`` /
    ``assistant_bigquery_dataset_ids``); ``getattr`` defaults keep this working
    before the settings migration is applied.
    """
    powerbi_client = _get_powerbi_client_for_org(org)        # live DAX only
    bigquery_client = _get_bigquery_client_for_org(org)      # catalog or live
    dbt_enabled = _get_dbt_enabled_for_org(org)
    powerbi_tools_enabled = _get_powerbi_tools_enabled_for_org(org)
    bigquery_live_enabled = _get_bigquery_live_enabled_for_org(org)
    chatbot_model = _get_chatbot_model_for_org(org)
    workspace_scope = None
    # Workspace defaults help the profiler too, so resolve them whenever the
    # PowerBI catalog assistant is on — not only when a live client is present.
    if powerbi_tools_enabled and org is not None:
        from .services.workspaces import resolve_default_workspaces_for_org
        workspace_scope = resolve_default_workspaces_for_org(user, org)
    return get_agent(
        powerbi_client=powerbi_client,
        bigquery_client=bigquery_client,
        dbt_enabled=dbt_enabled,
        powerbi_tools_enabled=powerbi_tools_enabled,
        bigquery_live_enabled=bigquery_live_enabled,
        before_tool_call=before_tool_call,
        model=chatbot_model,
        workspace_scope=workspace_scope,
        record_call=record_call,
        surface=surface,
        org=org,
        user=user,
        chat_session=chat_session,
        pb_workspace_ids=getattr(org, 'assistant_powerbi_workspace_ids', None) or None,
        bq_dataset_ids=getattr(org, 'assistant_bigquery_dataset_ids', None) or None,
    )


# Default max time (seconds) the agent may run before we give up and return an
# error. Used as the fallback when an org has no ``chat_timeout_seconds`` set
# (and clamped to [30, 600] when configured per-org in Org Settings). This
# prevents the Django Q worker from hanging indefinitely on a stuck LLM call.
AGENT_TIMEOUT_SECONDS = 180
CHAT_SESSION_LOCK_TIMEOUT_SECONDS = 360

# Hard caps on a SINGLE agent run, enforced by pydantic-ai's UsageLimits. These
# stop pathological tool-call loops (e.g. walking the lineage graph hop-by-hop,
# re-calling an ambiguous schema lookup, or blindly retrying a failing DAX
# query) long before they reach the wall-clock timeout. When a cap is hit we do
# NOT error out — we synthesise a best-effort answer from whatever the agent
# already gathered (see ``_finalize_partial_answer``). Legitimate questions use
# far fewer calls than these limits, so they are never affected.
AGENT_TOOL_CALLS_LIMIT = 20
AGENT_REQUEST_LIMIT = 30

# Transient LLM-provider failures (Gemini 5xx "Bad Gateway"/overload, 429 rate
# limits) are retried with a short backoff before the error reaches the user.
LLM_RETRY_BACKOFF_SECONDS = (10, 20)  # sleep before attempt 2, attempt 3

# Human-readable status messages shown in the chat bubble while a tool is executing.
# Keyed by the raw tool function name (before make_safe_tool renames it to safe_*).
TOOL_STATUS_MESSAGES = {
    'get_pb_item_details':        'Profiling the PowerBI item...',
    'get_pb_usage_analytics':     'Building the measure ↔ report usage map...',
    'get_dbt_item_details':       'Profiling the dbt model...',
    'search_pb_columns':          'Looking up PowerBI columns and schemas...',
    'get_pb_measure_dependencies': 'Fetching measure dependencies and DAX...',
    'get_pb_measure_schema': 'Building measure schema bundle (tables, columns, joins)...',
    'verify_pb_measure_dimension_link': 'Verifying measure↔dimension relationship path...',
    'preview_pb_dbt_bridge':      'Previewing PowerBI ↔ dbt bridge candidates...',
    'search_dbt_models':          'Searching dbt models...',
    'search_dbt_sources':         'Searching dbt sources...',
    'search_dbt_tests':           'Searching dbt tests...',
    'get_dbt_sql':                'Fetching dbt SQL...',
    'get_dbt_bigquery_lineage':   'Tracing dbt ↔ BigQuery lineage...',
    'get_dbt_upstream_tree':      'Walking dbt upstream lineage tree...',
    'get_lineage':                'Tracing data lineage...',
    'bigquery_execute_query':     'Running read-only BigQuery query...',
    'bigquery_get_table_schema':  'Fetching BigQuery table schema...',
    'bigquery_list_datasets':     'Listing BigQuery datasets...',
    'bigquery_list_tables':       'Listing BigQuery tables...',
    'powerbi_run_dax_query':     'Running DAX query on PowerBI...',
    'powerbi_list_workspaces':   'Listing PowerBI workspaces...',
    'powerbi_list_datasets':     'Listing PowerBI datasets...',
    'powerbi_get_dataset_schema':'Validating PowerBI dataset schema...',
    'powerbi_get_dataset_tables':'Fetching dataset tables...',
}


def _chat_session_lock_key(session_id):
    return f'chat_session_busy_{session_id}'


def _extract_parts_text(raw):
    """Joined text of a JSON ``{"parts": [...]}`` message string, else ``None``.

    Concatenates each part's ``content`` (falling back to ``text``). Returns
    ``None`` when ``raw`` is not a JSON object, fails to parse, or has no
    ``parts`` key, so each caller can apply its own fallback for that case.
    """
    if not (isinstance(raw, str) and raw.strip().startswith('{') and raw.strip().endswith('}')):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if 'parts' not in parsed:
        return None
    return "".join(str(p.get('content', '') or p.get('text', '')) for p in parsed['parts'])


def _run_with_timeout(fn, timeout_seconds=AGENT_TIMEOUT_SECONDS):
    """Run ``fn`` in a helper thread and stop waiting after ``timeout_seconds``.

    Python cannot force-kill a running thread, but avoiding the
    ThreadPoolExecutor context manager keeps the Django-Q worker from blocking
    on executor shutdown after the timeout path has already fired.
    """
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    timed_out = False
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        timed_out = True
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(
            f"The AI assistant took too long to respond (>{timeout_seconds}s). "
            "Please try a simpler question or try again later."
        ) from exc
    finally:
        if not timed_out:
            executor.shutdown(wait=True)


def retry_transient_llm_errors(fn):
    """Call ``fn``, retrying when the LLM provider fails transiently.

    Retries ``ModelHTTPError`` with status 429 or 5xx (e.g. Gemini returning
    502 Bad Gateway under load) up to ``len(LLM_RETRY_BACKOFF_SECONDS)`` extra
    times, sleeping between attempts. Any other error propagates immediately.
    """
    import time
    from pydantic_ai.exceptions import ModelHTTPError

    for attempt, backoff in enumerate(LLM_RETRY_BACKOFF_SECONDS + (None,)):
        try:
            return fn()
        except ModelHTTPError as exc:
            retryable = exc.status_code == 429 or exc.status_code >= 500
            if not retryable or backoff is None:
                raise
            logger.warning(
                "Transient LLM error (status %s) on attempt %d, retrying in %ss",
                exc.status_code, attempt + 1, backoff,
            )
            time.sleep(backoff)


def _agent_usage_limits():
    """Per-run cap that bounds tool-call loops without affecting normal use."""
    return UsageLimits(
        request_limit=AGENT_REQUEST_LIMIT,
        tool_calls_limit=AGENT_TOOL_CALLS_LIMIT,
    )


def _finalize_partial_answer(model_identifier, partial_messages):
    """Synthesise a best-effort answer from the messages already gathered when a
    run hits its tool-call budget. Uses a tool-LESS agent on the same model so it
    can only produce text (never call more tools), turning a dead-end loop into a
    useful partial answer.

    Returns the answer text, or '' if even the finalisation fails.
    """
    if not model_identifier:
        return ''
    try:
        from pydantic_ai import Agent
        from pydantic_ai.messages import (
            UserPromptPart, TextPart, ToolReturnPart,
        )
        from .tools.agent import build_model_settings

        # Flatten the captured run to PLAIN TEXT: the user's question, every bit
        # the tools already returned, and anything the model already said. We
        # deliberately DROP all tool-CALL parts — feeding them back as message
        # history makes a weak finalize model mimic the pattern and emit a fresh
        # tool call, which trips tool_calls_limit=0 and aborts finalisation
        # (leaving the user with nothing). Passing the gathered facts as a single
        # text prompt removes that failure mode entirely.
        question = ''
        gathered: list = []
        for msg in partial_messages:
            for part in getattr(msg, 'parts', []) or []:
                if isinstance(part, UserPromptPart):
                    c = part.content
                    if isinstance(c, str) and c.strip():
                        question = c.strip()
                elif isinstance(part, ToolReturnPart):
                    c = part.content
                    text = str(c.get('data', c)) if isinstance(c, dict) else str(c)
                    if text and text.strip():
                        gathered.append(text.strip())
                elif isinstance(part, TextPart):
                    if part.content and part.content.strip():
                        gathered.append(part.content.strip())
        if not question and not gathered:
            return ''

        context_blob = '\n\n---\n\n'.join(gathered[-12:])[:12000]
        prompt = (
            f"User's question: {question or '(see gathered notes below)'}\n\n"
            f"Information already gathered before the tool budget ran out:\n"
            f"{context_blob or '(little was gathered)'}\n\n"
            f"Answer the question now using ONLY the information above. State "
            f"clearly what is known; if it is incomplete, say what single thing "
            f"to narrow down. Do not ask to run more tools."
        )

        finalize_agent = Agent(
            model=model_identifier,
            system_prompt=(
                'You are wrapping up a data-catalog answer that reached its '
                'tool-call budget before finishing. Give the best partial answer '
                'you can from the information provided. Be concise and honest '
                'about what is uncertain. Do not ask to run more tools.'
            ),
            model_settings=build_model_settings(model_identifier),
        )
        res = finalize_agent.run_sync(
            prompt,
            usage_limits=UsageLimits(request_limit=2, tool_calls_limit=0),
        )
        return res.output or ''
    except Exception:
        logger.warning('Partial-answer finalisation failed', exc_info=True)
        return ''


def run_chat_event_sync(session_id, user_message):
    """
    Module-level function required by Django Q to run background tasks.
    Runs the agent synchronously since Django Q2 executes in a sync thread environment.

    A hard timeout (AGENT_TIMEOUT_SECONDS) is enforced via ThreadPoolExecutor so
    the worker can never be stuck indefinitely by an unresponsive LLM or
    PowerBI API call.

    Real-time status messages are written to Django's cache under the key
    ``chat_status_<session_id>`` before each tool call so the polling endpoint
    can relay them to the frontend.
    """
    STATUS_CACHE_KEY = f'chat_status_{session_id}'

    def _on_tool_call(tool_name: str, _args: tuple, kwargs: dict) -> None:
        """Write the human-readable status for the tool currently executing."""
        msg = TOOL_STATUS_MESSAGES.get(tool_name, f'Running {tool_name.replace("_", " ")}...')
        if kwargs.get('query'):
            msg += f" (query={kwargs['query']!r})"
        cache.set(STATUS_CACHE_KEY, msg, timeout=300)

    try:
        # Announce immediately so the bubble shows something before any tool fires
        cache.set(STATUS_CACHE_KEY, 'Thinking...', timeout=300)

        session = ChatSession.objects.filter(id=session_id).select_related('user').first()
        if not session:
            return

        db_messages = list(session.messages.all().order_by('created_at'))
            
        formatted_history = []
        for i, msg in enumerate(db_messages):
            if i == len(db_messages) - 1:
                continue # Skip last user message for history
            try:
                parsed = json.loads(msg.content)
                if isinstance(parsed, dict) and ('parts' in parsed or 'role' in parsed or 'kind' in parsed):
                    formatted_history.extend(deserialize_messages([parsed]))
                else:
                    raise ValueError
            except Exception:
                if msg.role == 'user':
                    formatted_history.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
                else:
                    formatted_history.append(ModelResponse(parts=[TextPart(content=msg.content)]))

        org = _get_org_for_session(session)
        chatbot_model_id = _get_chatbot_model_for_org(org)

        debug_log: list = []

        from .services.debug_render import build_debug_payload, render_debug_section

        # ── Agent (context-first: front-loaded catalog + per-integration
        # schema/run tools; PowerBI live values via
        # get_pb_measure_schema → powerbi_run_dax_query) ────────────
        agent = build_chatbot_agent_for_org(
            org,
            user=session.user,
            before_tool_call=_on_tool_call,
            record_call=debug_log.append,
            surface='web',
            chat_session=session,
        )

        # Run agent in a separate thread so we can enforce a hard timeout. A
        # per-run UsageLimits cap bounds pathological tool-call loops; if it
        # fires, we capture what was gathered and synthesise a partial answer
        # rather than surfacing an error or running to the wall-clock timeout.
        budget_hit = {'value': False, 'text': ''}
        # Share the captured run messages out of the worker thread so a
        # wall-clock timeout (which abandons that thread mid-run) can still
        # synthesise a partial answer from whatever was gathered.
        captured = {'messages': None}

        def _run_agent():
            with capture_run_messages() as run_messages:
                captured['messages'] = run_messages
                try:
                    return retry_transient_llm_errors(
                        lambda: agent.run_sync(
                            user_message,
                            message_history=formatted_history,
                            usage_limits=_agent_usage_limits(),
                        )
                    )
                except UsageLimitExceeded:
                    budget_hit['value'] = True
                    budget_hit['text'] = _finalize_partial_answer(
                        chatbot_model_id, run_messages,
                    )
                    return None

        timeout_seconds = getattr(org, 'chat_timeout_seconds', None) or AGENT_TIMEOUT_SECONDS
        try:
            res = _run_with_timeout(_run_agent, timeout_seconds)
        except TimeoutError:
            # Hard wall-clock timeout. Rather than surfacing a bare "took too
            # long" error, fall back to a best-effort answer built from the
            # messages captured up to the last completed step (same path as the
            # tool-budget cap). The abandoned worker thread is left to wind down.
            snapshot = list(captured['messages'] or [])
            logger.warning(
                'Agent run hit the %ss wall-clock timeout; finalising a partial '
                'answer from %d captured messages.', timeout_seconds, len(snapshot),
            )
            budget_hit['value'] = True
            budget_hit['text'] = _finalize_partial_answer(chatbot_model_id, snapshot)
            res = None

        debug_payload = build_debug_payload(debug_log)

        if budget_hit['value']:
            output = budget_hit['text'] or (
                "I gathered a lot but couldn't fully finish this question within "
                "its time and tool budget. Please narrow it — e.g. name a "
                "specific report, measure, or workspace — and I'll answer "
                "precisely."
            )
        else:
            output = (res.output if res else '') or ''
        if org and org.debug_responses_enabled:
            output += render_debug_section(debug_payload)

        # Candidate disambiguation is stateless: the agent/tool never persists
        # flow_state. If the user clicks a candidate, the selected-context
        # pre-empt above starts a fresh graph run with that selected measure.

        ChatMessage.objects.create(
            session=session,
            role="assistant",
            content=output,
            debug_meta=debug_payload,
        )

        return output

    except Exception as e:
        traceback.print_exc()
        ChatMessage.objects.create(
            session_id=session_id,
            role="assistant",
            content=f"Sorry, I encountered an error: {str(e)}"
        )
        return str(e)

    finally:
        # Clear the status key so stale messages don't appear on the next request
        cache.delete(STATUS_CACHE_KEY)
        cache.delete(_chat_session_lock_key(session_id))


@api_view(['POST'])
def chat_api_view(request):
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)

    lock_acquired = False
    session = None
    try:
        user_message = request.data.get('message')
        session_id = request.data.get('session_id')
        
        if not user_message:
            return JsonResponse({'error': 'Message is required'}, status=400)
            
        if session_id:
            session = ChatSession.objects.filter(id=session_id, user=user).first()
            if not session:
                return JsonResponse({'error': 'Session not found'}, status=404)
        else:
            title = user_message[:40] + "..." if len(user_message) > 40 else user_message
            session = ChatSession.objects.create(user=user, title=title)

        # Per-org chat timeout (default 180). The Django-Q hard kill and the
        # session lock are sized just above it so the friendly Python-level
        # timeout in run_chat_event_sync fires first, while Django Q still
        # guarantees a stuck worker can't hang indefinitely.
        org = _get_org_for_session(session)
        chat_timeout = getattr(org, 'chat_timeout_seconds', None) or AGENT_TIMEOUT_SECONDS
        hard_timeout = chat_timeout + 30
        lock_timeout = max(CHAT_SESSION_LOCK_TIMEOUT_SECONDS, hard_timeout + 60)

        lock_key = _chat_session_lock_key(session.id)
        lock_acquired = cache.add(
            lock_key, True, timeout=lock_timeout,
        )
        if not lock_acquired:
            return JsonResponse({
                'error': 'A response is already being generated for this chat. '
                         'Please wait for it to finish before sending another message.'
            }, status=409)
            
        # The user message should be plain text, but if it comes wrapped in JSON 
        # from the frontend history building, extract the text part.
        extracted = _extract_parts_text(user_message)
        clean_user_message = extracted if extracted is not None else user_message

        ChatMessage.objects.create(session=session, role='user', content=clean_user_message)
        
        # Enqueue the background task via Django Q2.
        # ``hard_timeout`` is a Django-Q-level kill sized just above the
        # per-org chat timeout (which fires first inside run_chat_event_sync),
        # so Django Q never leaves a worker hanging past the configured limit.
        task_id = async_task(
            'catalog.views.run_chat_event_sync',
            session.id,
            clean_user_message,
            timeout=hard_timeout,
        )

        # Store task→session mapping so the status endpoint can look up the cache key
        cache.set(f'chat_task_session_{task_id}', session.id, timeout=600)
        
        return JsonResponse({
            'session_id': session.id,
            'task_id': task_id
        })

    except Exception as e:
        traceback.print_exc()
        if lock_acquired and session is not None:
            cache.delete(_chat_session_lock_key(session.id))
        return JsonResponse({'error': str(e)}, status=500)


@api_login_required
@api_view(['GET'])
def get_chat_task_status(request, task_id):
    """
    Check the status of a Django Q background task.

    Also returns the real-time ``current_status`` string being written by the
    agent's tool hooks so the frontend bubble can show what is happening right now.
    """
    try:
        from django_q.models import Task, OrmQ

        # Look up which session this task belongs to so we can read its cache key
        session_id = cache.get(f'chat_task_session_{task_id}')
        if not session_id:
            return JsonResponse({'status': 'not_found', 'error': 'Task not found'}, status=404)

        owns_session = ChatSession.objects.filter(
            id=session_id,
            user=request.user,
        ).exists()
        if not owns_session:
            return JsonResponse({'status': 'forbidden', 'error': 'Forbidden'}, status=403)

        current_status = (
            cache.get(f'chat_status_{session_id}', 'Thinking...')
        )
        
        # Check if the task exists in the Task table (completed or failed)
        task = Task.objects.filter(id=task_id).first()
        if task:
            if task.success:
                return JsonResponse({'status': 'completed', 'result': task.result})
            else:
                # Even on Django-Q failure the worker wrote an error message to the DB
                return JsonResponse({'status': 'failed', 'error': 'Task failed'})
                
        # Check if it's still in the queue (waiting to be picked up)
        queued = OrmQ.objects.filter(key=task_id).exists()
        if queued:
            return JsonResponse({'status': 'processing', 'current_status': current_status})
            
        # Not in OrmQ and not in Task yet — worker is actively processing it
        return JsonResponse({'status': 'processing', 'current_status': current_status})
        
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)


@api_login_required
@api_view(['GET'])
def get_chat_sessions(request):
    sessions = ChatSession.objects.filter(user=request.user).order_by('-updated_at')
    data = [{'id': s.id, 'title': s.title, 'updated_at': s.updated_at.isoformat()} for s in sessions]
    return Response(data)

@api_login_required
@api_view(['GET'])
def get_chat_messages(request, session_id):
    session = ChatSession.objects.filter(id=session_id, user=request.user).first()
    if not session:
        return Response({'error': 'Session not found'}, status=404)
    
    messages = session.messages.all().order_by('created_at')
    data = []
    
    for m in messages:
        content = m.content
        # Backward compatibility: extract text if content is a legacy JSON string.
        # Only override when the join is non-empty (else keep the stored content).
        extracted = _extract_parts_text(content)
        if extracted:
            content = extracted
        
        # Standardize role naming for the frontend
        role = 'model' if m.role == 'assistant' else 'user'
        
        data.append({'role': role, 'content': content})
        
    return Response(data)

@api_login_required
@api_view(['DELETE'])
def delete_chat_session(request, session_id):
    session = ChatSession.objects.filter(id=session_id, user=request.user).first()
    if session:
        session.delete()
        return Response({'status': 'deleted'})
    return Response({'error': 'Session not found'}, status=404)

def _get_user_organization(user):
    """Return the Organization for the given user via OrganizationMembership, or None."""
    membership = OrganizationMembership.objects.filter(user=user).select_related('organization').first()
    return membership.organization if membership else None


def _deleted_filter(org):
    """``{'deleted': False}`` unless the org has opted in to show soft-deleted
    items via Organization.show_deleted_items, in which case ``{}``."""
    if org and org.show_deleted_items:
        return {}
    return {'deleted': False}

class DepartmentViewSet(viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer

    def get_queryset(self):
        org = _get_user_organization(self.request.user)
        if org:
            return Department.objects.filter(organization=org)
        return Department.objects.all()

class DataPersonViewSet(viewsets.ModelViewSet):
    serializer_class = DataPersonSerializer
    filter_backends = [DjangoFilterBackend]
    # Note: 'department' is the legacy single-FK filter name kept for
    # frontend compatibility. It maps to membership in the new M2M field
    # via the get_queryset hook below.
    filterset_fields = ['is_owner', 'is_steward', 'is_other']

    def get_queryset(self):
        org = _get_user_organization(self.request.user)
        qs = DataPerson.objects.filter(organization=org) if org else DataPerson.objects.all()
        qs = qs.prefetch_related('departments')
        # Convenience query param: ?role=owner / ?role=steward / ?role=other
        # returns only people flagged for that role. The dictionary UI uses
        # this to populate the Owner and Steward dropdowns separately;
        # "other" is a person-level tag with no governance-slot dropdown.
        role = self.request.query_params.get('role')
        if role == 'owner':
            qs = qs.filter(is_owner=True)
        elif role == 'steward':
            qs = qs.filter(is_steward=True)
        elif role == 'other':
            qs = qs.filter(is_other=True)
        # Department filter (M2M membership). Accept either name to keep
        # older callers working.
        dept_id = self.request.query_params.get('department') or self.request.query_params.get('departments')
        if dept_id:
            qs = qs.filter(departments__id=dept_id).distinct()
        return qs

class CategoryViewSet(viewsets.ModelViewSet):
    serializer_class = CategorySerializer

    def get_queryset(self):
        org = _get_user_organization(self.request.user)
        if org:
            return Category.objects.filter(organization=org)
        return Category.objects.all()


class MetricsMapViewSet(viewsets.ModelViewSet):
    """CRUD for org-scoped metrics maps (the React "Metrics Map" scratchpad).

    Maps are private to the requesting user's organization: every read is
    filtered to that org and ``organization``/``created_by`` are stamped
    server-side on write so a payload can't reach another tenant's data.
    """
    serializer_class = MetricsMapSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['kind']
    search_fields = ['name', 'description']
    ordering_fields = ['updated_at', 'created_at', 'name']

    def get_queryset(self):
        org = _get_user_organization(self.request.user)
        if not org:
            return MetricsMap.objects.none()
        return MetricsMap.objects.filter(organization=org).select_related('created_by')

    def perform_create(self, serializer):
        serializer.save(
            organization=_get_user_organization(self.request.user),
            created_by=self.request.user,
        )

    def perform_update(self, serializer):
        # Keep the map pinned to its org; never let an update move it.
        serializer.save(organization=_get_user_organization(self.request.user))

    @action(detail=True, methods=['post', 'delete'], url_path='share')
    def share(self, request, pk=None):
        """Enable / update / disable the public share link for a map.

        ``get_object`` runs through the org-scoped ``get_queryset``, so only the
        owning org can (un)share a given map — a foreign map 404s here.

          POST   -> enable or update. Mints a ``public_token`` (uuid4) the first
                    time, or when the body asks to ``rotate`` it; applies the
                    optional ``can_drag`` viewer setting. Returns the token + flag.
          DELETE -> stop sharing. Clears ``public_token`` (the link goes dead).
        """
        m = self.get_object()
        if request.method == 'DELETE':
            m.public_token = None
            m.save(update_fields=['public_token', 'updated_at'])
            return Response(status=204)

        data = request.data if isinstance(request.data, dict) else {}
        if m.public_token is None or data.get('rotate'):
            m.public_token = uuid.uuid4()
        if 'can_drag' in data:
            m.public_can_drag = bool(data.get('can_drag'))
        m.save(update_fields=['public_token', 'public_can_drag', 'updated_at'])
        return Response({
            'public_token': str(m.public_token),
            'public_can_drag': m.public_can_drag,
        })


@api_view(['GET'])
@permission_classes([AllowAny])
def metrics_map_public(request, token):
    """Anonymous, read-only fetch of a shared metrics map by its share token.

    No authentication and no org scoping — knowledge of the unguessable uuid4
    link is the only gate (mirrors "anyone with the link can view"). Returns the
    narrow public projection (see ``PublicMetricsMapSerializer``); a missing or
    revoked token is a flat 404 so disabled links read as "no longer available".
    """
    m = MetricsMap.objects.filter(public_token=token).first()
    if not m:
        return Response({'detail': 'Not found.'}, status=404)
    return Response(PublicMetricsMapSerializer(m).data)


class ActionPermissionMixin:
    """Wraps Item updates with Slack notifications and the auto-DELETED-on-delete
    rule. The name is historical — the read-only/read-write permission split was
    removed; any authenticated org member can write. Page visibility is still
    gated by group membership."""

    def _apply_and_notify(self, request, super_call):
        """Run the Item update, then fire Slack alerts if status or deleted
        changed. Status lives on the item's ItemGroup now, so marking an item
        deleted auto-DEPRECATEs its group (not a per-item column)."""
        instance = self.get_object()
        grp = instance.item_group
        old_status = grp.status if grp else instance.status
        old_deleted = instance.deleted

        # An item-level `status` write is captured here and routed to the item's
        # ItemGroup below — the group is the single source of truth, and the
        # change is cascaded back down to every item. So acting on one item
        # updates the whole group and fires exactly one alert/task.
        requested_status = None
        if hasattr(request, 'data') and 'status' in request.data:
            requested_status = request.data.get('status')

        response = super_call(request)
        instance.refresh_from_db()
        grp = instance.item_group

        # Route a valid requested status to the group.
        if requested_status in dict(Item.STATUS_CHOICES) and grp and grp.status != requested_status:
            grp.status = requested_status
            grp.save(update_fields=['status'])

        # Stamp / clear the deletion timestamp so we retain when each item was
        # marked for deletion (and clear it if the item is restored).
        if instance.deleted != old_deleted:
            from django.utils import timezone
            instance.deleted_at = timezone.now() if instance.deleted else None
            instance.save(update_fields=['deleted_at'])
        # Marking an item deleted auto-DEPRECATEs its group.
        if instance.deleted and not old_deleted and grp and grp.status != 'DELETED':
            grp.status = 'DELETED'
            grp.save(update_fields=['status'])

        new_status = grp.status if grp else instance.status
        # Mirror the group's (possibly new) status onto every item so the
        # denormalized column stays consistent across the whole group.
        if grp and new_status != old_status:
            from .services.group_cascade import cascade_status_to_items
            cascade_status_to_items(grp)
            instance.refresh_from_db(fields=['status'])
        try:
            from etl.hooks.slack.slack_alerts import send_slack_item_alert
            if new_status != old_status:
                send_slack_item_alert(instance, request.user, 'status', old_status, new_status)
            if instance.deleted and not old_deleted:
                send_slack_item_alert(instance, request.user, 'deleted', old_deleted, instance.deleted)
        except Exception as e:
            print(f'[Slack alert] notify failed: {e}')
        if new_status != old_status and grp is not None:
            from .governance_tasks import sync_status_task
            from .status_history import log_status_change, sync_group_deleted_at
            log_status_change(grp, old_status, new_status, request.user)
            sync_group_deleted_at(grp, new_status)
            sync_status_task(grp, new_status, request.user)
        return response

    def update(self, request, *args, **kwargs):
        parent = super()
        return self._apply_and_notify(request, lambda r: parent.update(r, *args, **kwargs))

class CatalogPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'limit'
    max_page_size = 100000

class ItemViewSet(ActionPermissionMixin, viewsets.ModelViewSet):
    # Governance now lives on item_group — select_related it (and its FKs) so
    # the serializer's group-sourced fields don't N+1.
    # NOTE: the deleted filter is applied in get_queryset so it can honour
    # Organization.show_deleted_items per request.
    queryset = Item.objects.all().select_related(
        'organization', 'item_group',
        'item_group__ownership_department', 'item_group__ownership_person',
        'item_group__steward', 'item_group__category', 'item_group__primary_item',
    )
    serializer_class = ItemSerializer
    pagination_class = CatalogPagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = [
        'item_name', 'item_type', 'service', 'is_unused', 'workspace_name',
        'dataset_name', 'table_name', 'database_name',
        'status', 'item_group__status', 'item_group__ownership_department',
        'item_group__ownership_person', 'item_group__category',
        'integration_source',
    ]
    search_fields = ['item_name']
    ordering_fields = [
        'item_name', 'connected_reports', 'connected_report_pages',
        'connected_visuals', 'connected_measures', 'connected_columns',
        'connected_tables',
    ]

    def get_queryset(self):
        org = _get_user_organization(self.request.user)
        # `?include_deleted=true` keeps soft-deleted items in the result,
        # regardless of the org's show_deleted_items toggle. The PowerBI Cleanup
        # "Deprecated" tab uses it (with status=DELETED) so the marked-to-
        # delete groups still appear there and can be undone. Every other view
        # hides soft-deleted items as usual.
        if self.request.query_params.get('include_deleted') == 'true':
            qs = self.queryset
        else:
            qs = self.queryset.filter(**_deleted_filter(org))
        if org:
            qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))
        return qs

    @action(detail=True, methods=['post'])
    def set_primary(self, request, pk=None):
        """Pin this item as its ItemGroup's primary instance.

        The group's ``primary_item`` is the single source of truth for the
        default workspace / dataset / DAX (and the Dashboard's measure
        attribution). One FK on the group — no sibling bookkeeping needed.
        """
        item = self.get_object()
        grp = item.item_group
        if grp is None:
            return Response({'detail': 'This item has no group.'}, status=400)
        if grp.primary_item_id != item.pk:
            grp.primary_item = item
            grp.save(update_fields=['primary_item'])
        return Response({
            'status': 'ok',
            'group': grp.pk,
            'group_key': grp.group_key,
            'primary_item_id': item.pk,
        })


class ItemGroupViewSet(viewsets.ModelViewSet):
    """Governance is curated here. The Data Dictionary PATCHes a group to set
    owner / steward / status / category / annotation for the whole group
    (every measure instance, or a single-item singleton)."""
    queryset = ItemGroup.objects.select_related(
        'ownership_department', 'ownership_person', 'steward',
        'category', 'organization', 'primary_item',
    )
    serializer_class = ItemGroupSerializer

    def get_queryset(self):
        qs = self.queryset
        org = _get_user_organization(self.request.user)
        if org:
            qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))
        return qs

    def update(self, request, *args, **kwargs):
        """Curate a group's governance, then cascade the result to its items.

        Two write paths funnel through here:
          * a plain status edit (Data Dictionary dropdown) — mirrored onto every
            item's ``status`` column;
          * a mark-to-delete (``deleted`` flips True) — forces the group to
            DELETED and soft-deletes every item in the group.

        Either way we fire exactly one Slack alert / governance task per group,
        anchored to the group's primary (or first) item, and log the audit row.
        """
        instance = self.get_object()
        old_status = instance.status
        old_deleted = instance.deleted

        response = super().update(request, *args, **kwargs)
        instance.refresh_from_db()

        just_deleted = instance.deleted and not old_deleted
        just_restored = old_deleted and not instance.deleted

        # Marking the group deleted forces DELETED (decision: delete couples
        # to deprecation) before we cascade status down.
        if just_deleted and instance.status != 'DELETED':
            instance.status = 'DELETED'
            instance.save(update_fields=['status'])

        new_status = instance.status
        status_changed = new_status != old_status

        from .services.group_cascade import cascade_status_to_items, cascade_delete_to_items
        if status_changed or just_deleted:
            cascade_status_to_items(instance)
        if just_deleted:
            cascade_delete_to_items(instance, deleted=True)
        elif just_restored:
            cascade_delete_to_items(instance, deleted=False)

        if status_changed or just_deleted:
            rep = instance.primary_item or instance.items.first()
            if rep is not None:
                try:
                    from etl.hooks.slack.slack_alerts import send_slack_item_alert
                    if status_changed:
                        send_slack_item_alert(rep, request.user, 'status', old_status, new_status)
                    if just_deleted:
                        send_slack_item_alert(rep, request.user, 'deleted', False, True)
                except Exception as e:
                    print(f'[Slack alert] notify failed: {e}')
            from .governance_tasks import sync_status_task
            from .status_history import log_status_change, sync_group_deleted_at
            if status_changed:
                log_status_change(instance, old_status, new_status, request.user)
            sync_group_deleted_at(instance, new_status)
            sync_status_task(instance, new_status, request.user)
        return response


class GovernanceTaskViewSet(viewsets.ModelViewSet):
    """Task Manager feed. Tasks are created by the backend on status changes;
    the client lists them (default: open, newest first) and marks them done."""
    queryset = GovernanceTask.objects.select_related(
        'assignee', 'item_group', 'item_group__primary_item', 'organization',
    )
    serializer_class = GovernanceTaskSerializer
    pagination_class = CatalogPagination
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['assignee']
    ordering_fields = ['created_at', 'completed_at']
    ordering = ['-created_at']

    def get_queryset(self):
        qs = self.queryset
        org = _get_user_organization(self.request.user)
        if org:
            qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))
        # `state`: 'open' (default) | 'done' | 'all'. Handled here rather than
        # via the filterset so 'all' means "no state filter" instead of an
        # exact match on the literal string.
        state = self.request.query_params.get('state', GovernanceTask.STATE_OPEN)
        if state in (GovernanceTask.STATE_OPEN, GovernanceTask.STATE_DONE):
            qs = qs.filter(state=state)
        return qs

    @action(detail=True, methods=['post'])
    def done(self, request, pk=None):
        """Mark the task done (soft) — hidden from the default feed, kept for audit."""
        from django.utils import timezone
        task = self.get_object()
        if task.state != GovernanceTask.STATE_DONE:
            task.state = GovernanceTask.STATE_DONE
            task.completed_at = timezone.now()
            task.completed_by = request.user if request.user.is_authenticated else None
            task.save(update_fields=['state', 'completed_at', 'completed_by', 'updated_at'])
        return Response({'status': 'ok', 'id': task.pk, 'state': task.state})


@api_view(['GET'])
def get_summary(request):
    summary = Summary.objects.first()
    if summary:
        serializer = SummarySerializer(summary)
        return Response(serializer.data)
    return Response({})


@api_view(['GET'])
def get_dashboard(request):
    """Precomputed Dashboard payload — one request instead of the SPA fetching
    every measure/report/page + usage and aggregating in the browser. Mirrors
    the legacy ``dashboard_home`` server-side compute: one entry per measure
    group (attributed to its primary instance) + per-workspace report/page/view
    stats + the governance filter lists."""
    from django.db.models import Sum, Max
    from collections import defaultdict

    org = _get_user_organization(request.user)
    del_kw = _deleted_filter(org)
    org_q = (Q(organization=org) | Q(organization__isnull=True)) if org else None
    external_re = re.compile(r'external\s*measure')

    def measure_is_external(rec):
        expr = (rec.get('expression') or '').strip()
        if not expr:
            return True
        hay = (expr + ' ' + (rec.get('description') or '')).lower()
        return bool(external_re.search(hay))

    def ws_priority(name):
        ws = (name or '').lower()
        if 'finance' in ws:
            return 0
        if 'commercial' in ws:
            return 1
        if 'ops' in ws or 'operation' in ws:
            return 2
        if 'marketing' in ws:
            return 3
        return 4

    def pick_primary(instances, primary_id=None):
        if primary_id:
            for i in instances:
                if i.get('item_id') == primary_id:
                    return i
        return sorted(instances, key=lambda i: (
            1 if measure_is_external(i) else 0,
            ws_priority(i.get('workspace_name')),
            i.get('dataset_name') or '',
            i.get('item_id') or '',
        ))[0]

    # ---- Measures, de-duplicated by ItemGroup ------------------------------
    m_qs = (Item.objects.filter(item_type='PB_MEASURE', **del_kw)
            .exclude(workspace_name__isnull=True).exclude(workspace_name=''))
    if org_q:
        m_qs = m_qs.filter(org_q)
    measure_rows = list(m_qs.values(
        'item_id', 'item_group_id', 'workspace_name', 'dataset_name',
        'is_unused', 'description', 'expression',
    ))

    grp_qs = ItemGroup.objects.filter(kind='measure_name')
    if org_q:
        grp_qs = grp_qs.filter(org_q)
    gov = {g['id']: g for g in grp_qs.values(
        'id', 'primary_item_id', 'custom_description',
        'ownership_person__name', 'steward__name', 'ownership_department__name',
        'ownership_department_id', 'ownership_person_id', 'steward_id',
        'status', 'category_id', 'category__name',
    )}

    groups = defaultdict(list)
    for r in measure_rows:
        groups[r['item_group_id'] or ('id::' + (r['item_id'] or ''))].append(r)

    status_labels = dict(Item.STATUS_CHOICES)
    measure_groups = []
    distinct_measures = 0
    unused_measures_total = 0
    for gkey, insts in groups.items():
        g = gov.get(gkey, {})
        primary = pick_primary(insts, g.get('primary_item_id'))
        has_desc = bool((primary.get('description') or '').strip()
                        or (g.get('custom_description') or '').strip())
        is_unused = bool(primary.get('is_unused'))
        distinct_measures += 1
        if is_unused:
            unused_measures_total += 1
        measure_groups.append({
            'w': primary['workspace_name'],
            'di': g.get('ownership_department_id'),
            'dn': g.get('ownership_department__name') or 'No Department',
            'oi': g.get('ownership_person_id'),
            'on': g.get('ownership_person__name') or 'No Owner',
            'si': g.get('steward_id'),
            'sn': g.get('steward__name') or 'No Steward',
            'st': g.get('status') or 'UNVERIFIED',
            'stn': status_labels.get(g.get('status'), 'Unverified'),
            'ci': g.get('category_id'),
            'cn': g.get('category__name') or 'No Category',
            'h': 1 if has_desc else 0,
            'u': 1 if is_unused else 0,
        })

    # ---- Per-workspace report / page / view stats --------------------------
    ws_stats = {}

    def ws_stat(ws):
        return ws_stats.setdefault(ws, {'r': 0, 'p': 0, 'v30': 0, 'vt': 0})

    rep_qs = (Item.objects.filter(item_type='PB_REPORT', **del_kw)
              .exclude(workspace_name__isnull=True).exclude(workspace_name=''))
    pg_qs = (Item.objects.filter(item_type='PB_PAGE', **del_kw)
             .exclude(workspace_name__isnull=True).exclude(workspace_name=''))
    if org_q:
        rep_qs = rep_qs.filter(org_q)
        pg_qs = pg_qs.filter(org_q)
    for r in rep_qs.values('workspace_name').annotate(c=Count('item_id')):
        ws_stat(r['workspace_name'])['r'] = r['c']
    for p in pg_qs.values('workspace_name').annotate(c=Count('item_id')):
        ws_stat(p['workspace_name'])['p'] = p['c']

    usage_qs = PowerBIReportUsage.objects.all()
    if org_q:
        usage_qs = usage_qs.filter(org_q)
    recent_month = usage_qs.aggregate(m=Max('month'))['m']
    views_total_all = usage_qs.aggregate(s=Sum('view_count'))['s'] or 0
    views_recent_all = 0
    if recent_month:
        views_recent_all = (usage_qs.filter(month=recent_month)
                            .aggregate(s=Sum('view_count'))['s'] or 0)
        for u in usage_qs.values('workspace_name').annotate(
                vt=Sum('view_count'),
                v30=Sum('view_count', filter=Q(month=recent_month))):
            ws = u['workspace_name']
            if not ws:
                continue
            st = ws_stat(ws)
            st['vt'] = u['vt'] or 0
            st['v30'] = u['v30'] or 0

    # ---- Governance filter lists (with department membership) --------------
    dept_qs = Department.objects.order_by('name')
    owner_qs = DataPerson.objects.filter(is_owner=True).order_by('name')
    steward_qs = DataPerson.objects.filter(is_steward=True).order_by('name')
    cat_qs = Category.objects.order_by('name')
    if org_q:
        dept_qs = dept_qs.filter(org_q)
        owner_qs = owner_qs.filter(org_q)
        steward_qs = steward_qs.filter(org_q)
        cat_qs = cat_qs.filter(org_q)

    def person_list(qs):
        return [{'id': p.id, 'name': p.name,
                 'departments': [d.id for d in p.departments.all()]}
                for p in qs.prefetch_related('departments')]

    summary = Summary.objects.first()
    total_reports = (summary.total_reports if summary
                     else sum(s['r'] for s in ws_stats.values()))

    return Response({
        'measure_groups': measure_groups,
        'ws_stats': ws_stats,
        'departments': [{'id': d.id, 'name': d.name} for d in dept_qs],
        'owners': person_list(owner_qs),
        'stewards': person_list(steward_qs),
        'categories': [{'id': c.id, 'name': c.name} for c in cat_qs],
        'summary': {
            'total_reports': total_reports,
            'distinct_measures': distinct_measures,
            'unused_measures_total': unused_measures_total,
            'views_total_all': views_total_all,
            'views_recent_all': views_recent_all,
            'recent_month': recent_month.isoformat() if recent_month else None,
            'recent_month_label': recent_month.strftime('%b %Y') if recent_month else '',
        },
    })

@api_view(['GET'])
def get_filters(request):
    # Cascading/dependent filters: an optional ``workspace_name`` narrows the
    # returned datasets to that workspace, and ``workspace_name`` +
    # ``dataset_name`` narrow the returned tables. With no params this returns
    # the full global lists (backwards compatible).
    #
    # IMPORTANT: ``.order_by()`` clears Item.Meta.ordering. Without it, Django
    # injects ``item_name`` into the SELECT alongside the values list, which
    # turns ``DISTINCT`` into "distinct (item_name, X)" pairs — i.e. tens of
    # thousands of "duplicate" rows for the same workspace/dataset/table name.
    del_kw = _deleted_filter(_get_user_organization(request.user))
    workspace = (request.query_params.get('workspace_name') or '').strip()
    dataset = (request.query_params.get('dataset_name') or '').strip()

    workspace_names = sorted(
        Item.objects.filter(**del_kw)
        .exclude(workspace_name__isnull=True).exclude(workspace_name='')
        .order_by()
        .values_list('workspace_name', flat=True).distinct()
    )

    dataset_qs = Item.objects.filter(
        item_type__in=['PB_TABLE', 'PB_COLUMN', 'PB_MEASURE', 'DBT_COLUMN'], **del_kw
    )
    if workspace:
        dataset_qs = dataset_qs.filter(workspace_name=workspace)
    dataset_names = sorted(
        dataset_qs
        .exclude(dataset_name__isnull=True).exclude(dataset_name='')
        .order_by()
        .values_list('dataset_name', flat=True).distinct()
    )

    table_qs = Item.objects.filter(**del_kw)
    if workspace:
        table_qs = table_qs.filter(workspace_name=workspace)
    if dataset:
        table_qs = table_qs.filter(dataset_name=dataset)
    table_names = sorted(
        table_qs
        .exclude(table_name__isnull=True).exclude(table_name='')
        .order_by()
        .values_list('table_name', flat=True).distinct()
    )

    return Response({
        'workspaces': workspace_names,
        'datasets': dataset_names,
        'tables': table_names,
    })

@api_view(['GET'])
def pb_cleanup_counts(request):
    """Filter-aware counts for the PowerBI Cleanup page's KPI cards and tab
    badges. Org-scoped exactly like ItemViewSet (the table source) and honours
    the same workspace_name / dataset_name filters, so the numbers track the
    filtered view instead of being frozen at the global totals on page load."""
    from .frontend_views import compute_pb_cleanup_counts
    org = _get_user_organization(request.user)
    pb_qs = Item.objects.filter(service='powerbi', **_deleted_filter(org))
    if org:
        pb_qs = pb_qs.filter(Q(organization=org) | Q(organization__isnull=True))
    counts = compute_pb_cleanup_counts(
        pb_qs,
        workspace_name=request.query_params.get('workspace_name') or None,
        dataset_name=request.query_params.get('dataset_name') or None,
    )
    return Response(counts)

@api_view(['GET'])
def dbt_insights(request):
    """Aggregate stats about the dbt transformation layer.

    All counts are derived at query time from ``Item`` and ``NetworkEdge``
    rows, so the endpoint stays current without a dedicated stats table.
    Heavier metrics (downstream-report counts) live on Item.connected_reports
    via the workflow final step's backfill.

    Query params:
        section — 'cleanup', 'top', or 'all' (default). Each page only needs
                  its half of the payload, so gating skips the wasted aggregate
                  / Python work for the half it never reads.
    """
    from django.db.models.functions import Substr

    section = (request.query_params.get('section') or 'all').lower()
    if section not in ('all', 'cleanup', 'top'):
        section = 'all'
    want_cleanup = section in ('all', 'cleanup')
    want_top = section in ('all', 'top')

    org = _get_user_organization(request.user)
    item_qs = Item.objects.filter(service='dbt', **_deleted_filter(org))
    edge_qs = NetworkEdge.objects.all()
    if org:
        # Match the same scoping rule ItemViewSet uses: org's own rows plus
        # legacy unscoped rows. NetworkEdge has no NULL-org legacy data, so
        # we filter strictly there.
        item_qs = item_qs.filter(Q(organization=org) | Q(organization__isnull=True))
        edge_qs = edge_qs.filter(organization=org)

    LIMIT = 15
    # `item_group`/`status`/`deleted`/`item_type` ride along so the Cleanup page
    # can show a status badge and the group-level Mark-to-Delete / Undo actions
    # (same as PowerBI Cleanup) without a second round-trip per row.
    LIST_FIELDS = ('item_id', 'item_name', 'database_name', 'schema_name',
                   'item_type', 'status', 'deleted', 'item_group')
    # Cleanup is a finite hygiene worklist — the user expects every row, so
    # we send them all and let the frontend paginate. `top` stays bounded.

    type_counts = dict(item_qs.values('item_type').annotate(c=Count('item_id'))
                       .values_list('item_type', 'c'))

    payload = {
        'totals': {
            'models': type_counts.get('DBT_MODEL', 0),
            'seeds': type_counts.get('DBT_SEED', 0),
            'sources': type_counts.get('DBT_SOURCE', 0),
            'tests': type_counts.get('DBT_TEST', 0),
            'columns': type_counts.get('DBT_COLUMN', 0),
        },
    }

    if want_cleanup:
        # Models without a description (governance hygiene).
        undocumented_qs = item_qs.filter(item_type='DBT_MODEL').filter(
            Q(description__isnull=True) | Q(description='')
        )

        # Models without a single DBT_TEST consumer. Push the exclusion to the
        # DB via a Substr() subquery so we never materialise the full tested-id
        # set in Python and the result is stably ordered by item_name.
        tested_model_ids = (
            edge_qs.filter(source__startswith='DBT_MODEL::',
                           target__startswith='DBT_TEST::')
            .annotate(model_item_id=Substr('source', len('DBT_MODEL::') + 1))
            .values('model_item_id')
        )
        untested_qs = (
            item_qs.filter(item_type='DBT_MODEL')
            .exclude(item_id__in=tested_model_ids)
        )

        unused_models_qs = item_qs.filter(item_type='DBT_MODEL', is_unused=True)
        unused_seeds_qs = item_qs.filter(item_type='DBT_SEED', is_unused=True)
        unused_sources_qs = item_qs.filter(item_type='DBT_SOURCE', is_unused=True)

        # DELETED count must include soft-deleted (marked-to-delete) rows so
        # the Deprecated tab badge matches its include_deleted listing. item_qs
        # already excludes deleted, so use a fresh, org-scoped queryset here.
        deprecated_qs = Item.objects.filter(service='dbt', status='DELETED')
        if org:
            deprecated_qs = deprecated_qs.filter(Q(organization=org) | Q(organization__isnull=True))

        payload['totals'].update({
            'unused_models': unused_models_qs.count(),
            'unused_seeds': unused_seeds_qs.count(),
            'unused_sources': unused_sources_qs.count(),
            'undocumented_models': undocumented_qs.count(),
            'untested_models': untested_qs.count(),
            'attention': item_qs.filter(status='ATTENTION').count(),
            'deprecated': deprecated_qs.count(),
        })
        payload['unused_models'] = list(
            unused_models_qs.order_by('item_name').values(*LIST_FIELDS)
        )
        payload['unused_seeds'] = list(
            unused_seeds_qs.order_by('item_name').values(*LIST_FIELDS)
        )
        payload['unused_sources'] = list(
            unused_sources_qs.order_by('item_name').values(*LIST_FIELDS)
        )
        payload['untested_models'] = list(
            untested_qs.order_by('item_name').values(*LIST_FIELDS)
        )
        payload['undocumented_models'] = list(
            undocumented_qs.order_by('item_name').values(*LIST_FIELDS)
        )

    if want_top:
        # Top dbt assets by downstream PB_REPORT count — the impact metric.
        # Includes models, seeds, and sources (the frontend renders a type badge).
        payload['top_by_reports'] = list(
            item_qs.filter(item_type__in=['DBT_MODEL', 'DBT_SEED', 'DBT_SOURCE'],
                           connected_reports__gt=0)
            .order_by('-connected_reports', 'item_name')
            .values('item_id', 'item_name', 'item_type', 'connected_reports',
                    'database_name', 'schema_name')[:LIMIT]
        )

        # Top models by raw downstream fan-out (distinct consumer nodes).
        # Captures load-bearing nodes that don't yet reach a report.
        fanout_rows = list(
            edge_qs.filter(source__startswith='DBT_MODEL::')
            .exclude(target__startswith='DBT_TEST::')
            .exclude(target__startswith='DBT_COLUMN::')
            .values('source')
            .annotate(c=Count('target', distinct=True))
            .order_by('-c')[:LIMIT]
        )
        fanout_item_ids = [row['source'].split('::', 1)[1] for row in fanout_rows]
        fanout_name_map = dict(
            item_qs.filter(item_type='DBT_MODEL', item_id__in=fanout_item_ids)
            .values_list('item_id', 'item_name')
        )
        payload['top_by_fanout'] = [
            {
                'item_id': item_id,
                'item_name': fanout_name_map.get(item_id, item_id),
                'consumers': row['c'],
            }
            for row, item_id in zip(fanout_rows, fanout_item_ids)
        ]

    return Response(payload)


# Whitelist of dimensions that can appear in the `group_by` param of the
# /api/powerbi-usage/ endpoint. Anything outside this set is dropped silently
# so the API never lets a caller order by / project an arbitrary column.
USAGE_GROUP_DIMENSIONS = {
    'month', 'workspace_id', 'workspace_name',
    'report_id', 'report_name',
    'user_email', 'user_display_name',
    'platform', 'distribution_method', 'report_page',
}
DEFAULT_USAGE_GROUP_BY = ['month', 'workspace_id', 'workspace_name', 'report_id', 'report_name']


@api_view(['GET'])
def powerbi_usage(request):
    """Aggregated Power BI report usage rows for the Reports Usage tab.

    Default behavior aggregates the per (workspace × report × user × month ×
    platform × distribution × page) rows up to (workspace × report × month)
    — the grain the legacy UI table renders.

    Pivot mode: pass `group_by` as a CSV of allowed dimensions
    (USAGE_GROUP_DIMENSIONS) to project / aggregate at any other grain. The
    response always carries the chosen dimensions plus `view_count` (Sum) and
    `unique_users` (distinct user_email count).

    Filters (optional): workspace_name, month (YYYY-MM-01). Org-scoped to
    the requesting user (mirrors ItemViewSet).
    """
    from django.db.models import Sum

    org = _get_user_organization(request.user)
    qs = PowerBIReportUsage.objects.all()
    if org:
        qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))

    workspace_name = request.query_params.get('workspace_name') or ''
    month = request.query_params.get('month') or ''
    if workspace_name:
        qs = qs.filter(workspace_name=workspace_name)
    if month:
        qs = qs.filter(month=month)

    group_by_param = request.query_params.get('group_by') or ''
    if group_by_param:
        # Preserve caller order, drop unknown dims, dedupe.
        seen = set()
        group_by = []
        for raw in group_by_param.split(','):
            dim = raw.strip()
            if dim in USAGE_GROUP_DIMENSIONS and dim not in seen:
                group_by.append(dim)
                seen.add(dim)
        if not group_by:
            group_by = list(DEFAULT_USAGE_GROUP_BY)
    else:
        group_by = list(DEFAULT_USAGE_GROUP_BY)

    try:
        limit = int(request.query_params.get('limit') or 0)
    except (TypeError, ValueError):
        limit = 0
    limit = max(0, min(limit, 10000))

    rows_qs = (
        qs.values(*group_by)
          .annotate(
              view_count=Sum('view_count'),
              unique_users=Count('user_email', distinct=True),
          )
    )
    # Default ordering preserves the legacy contract for the no-pivot path;
    # in pivot mode month may not even be a dim, so we just sort by views.
    if 'month' in group_by:
        rows_qs = rows_qs.order_by('-month', '-view_count')
    else:
        rows_qs = rows_qs.order_by('-view_count')

    if limit:
        rows_qs = rows_qs[:limit]

    data = []
    for r in rows_qs:
        item = {}
        for dim in group_by:
            v = r[dim]
            if dim == 'month' and v is not None:
                v = v.isoformat()
            item[dim] = v
        item['view_count'] = r['view_count'] or 0
        item['unique_users'] = r['unique_users'] or 0
        data.append(item)

    months = sorted(
        {r['month'].isoformat() for r in qs.values('month').distinct() if r['month']},
        reverse=True,
    )
    return Response({'results': data, 'months': months, 'group_by': group_by})


def _column_ego(center_id, depth, direction, unified=False, full=False):
    """Build a column-level ego graph around ``center_id``.

    Traverses only ``column`` edges (column<->column, measure->column, and the
    cross-tool bridge), then attaches each touched column's parent container via
    its ``contains`` edge so the frontend can render models/tables as boxes with
    their columns nested inside.

    If ``center_id`` is itself a container (model/table), we seed from its member
    columns so "show column lineage for this model" works directly.

    When ``full`` is True the column BFS ignores ``depth`` and runs until the
    frontier is dry, i.e. it follows column edges to their transitive ends (the
    "show full column lineage" action). Otherwise it is bounded to ``depth`` hops.

    When ``unified`` is True we additionally pull the downstream PowerBI report
    hierarchy (column/measure -> visual -> page -> report, all ``model``-kind
    usage edges) so reports render as downstream consumer cards. Reports are
    inherently downstream, so this only runs for 'both'/'downstream'.
    """
    from django.db.models import Q
    chunk_size = 900

    # Edges persist `kind`; `_edge_is` falls back to the prefix classifier only
    # for legacy rows whose `kind` was never backfilled (so the result is exact).
    def _edge_is(edge, kind):
        return (edge.kind or _edge_kind(edge.source, edge.target)) == kind

    # Lineage edges are kind='column' (incl. the cross-tool bridge); the column
    # box a member sits in is kind='contains'. NULL is kept so un-backfilled rows
    # still get inspected by the Python fallback above.
    _LINEAGE_Q = Q(kind='column') | Q(kind__isnull=True)
    _CONTAINS_Q = Q(kind='contains') | Q(kind__isnull=True)

    # 1. Seed frontier: the center column(s).
    seed = set()
    if _node_type(center_id) in _CONTAINER_TYPES:
        for e in NetworkEdge.objects.filter(_CONTAINS_Q, source=center_id):
            if _edge_is(e, 'contains'):
                seed.add(e.target)
    if not seed:
        seed.add(center_id)

    # 2. BFS over column edges only (direction-aware), same semantics as the
    #    asset ego graph: downstream follows source->target, upstream the reverse.
    nodes_set = set(seed)
    column_edges = set()
    edge_meta = {}  # (source, target) -> (lineage_type, is_bridge)
    frontier = set(seed)
    # `full` walks column edges to their transitive ends (loop until the frontier
    # is dry); otherwise we bound to `depth` hops. The graph is finite and we only
    # ever enqueue not-yet-seen nodes, so full traversal always halts — the large
    # step cap below is just a backstop against pathological/cyclic data.
    _FULL_STEP_CAP = 1000
    for _ in range(_FULL_STEP_CAP if full else depth):
        if not frontier:
            break
        next_frontier = set()
        frontier_list = list(frontier)
        for i in range(0, len(frontier_list), chunk_size):
            chunk = frontier_list[i:i + chunk_size]
            if direction == 'downstream':
                layer = NetworkEdge.objects.filter(_LINEAGE_Q, source__in=chunk)
            elif direction == 'upstream':
                layer = NetworkEdge.objects.filter(_LINEAGE_Q, target__in=chunk)
            else:
                layer = (NetworkEdge.objects.filter(_LINEAGE_Q, source__in=chunk)
                         | NetworkEdge.objects.filter(_LINEAGE_Q, target__in=chunk))
            for e in layer:
                if e.source == e.target or not _edge_is(e, 'column'):
                    continue
                column_edges.add((e.source, e.target))
                edge_meta[(e.source, e.target)] = (e.lineage_type, bool(e.bridge_reason))
                if direction == 'downstream':
                    if e.target not in nodes_set:
                        next_frontier.add(e.target)
                elif direction == 'upstream':
                    if e.source not in nodes_set:
                        next_frontier.add(e.source)
                else:
                    if e.source not in nodes_set:
                        next_frontier.add(e.source)
                    if e.target not in nodes_set:
                        next_frontier.add(e.target)
        nodes_set |= next_frontier
        frontier = next_frontier

    # 3. Attach the parent container of every column we collected.
    contains_edges = set()
    member_ids = [n for n in nodes_set if _node_type(n) in _MEMBER_TYPES]
    for i in range(0, len(member_ids), chunk_size):
        chunk = member_ids[i:i + chunk_size]
        for e in NetworkEdge.objects.filter(_CONTAINS_Q, target__in=chunk):
            if _edge_is(e, 'contains'):
                contains_edges.add((e.source, e.target))
                nodes_set.add(e.source)

    # 3b. Structural relationship edges (FK→PK 'join' / 'filter') incident to any
    # collected column. We pull the joined column in (one hop, no transitive
    # expansion → bounded) and attach its container so the relationship is
    # visible from the current view without ballooning the graph. Skipped on a
    # depth-0 focus load, which must show only the focused element + its card.
    structural_edges = []
    if depth > 0 or full:
        struct_seen = set()
        member_set = set(member_ids)
        _STRUCT_Q = Q(kind='join') | Q(kind='filter')
        for i in range(0, len(member_ids), chunk_size):
            chunk = member_ids[i:i + chunk_size]
            incident = (
                NetworkEdge.objects.filter(_STRUCT_Q, source__in=chunk)
                | NetworkEdge.objects.filter(_STRUCT_Q, target__in=chunk)
            )
            for e in incident:
                if e.kind not in ('join', 'filter'):
                    continue
                key = (e.source, e.target, e.kind)
                if key in struct_seen:
                    continue
                struct_seen.add(key)
                structural_edges.append((e.source, e.target, e.kind, bool(e.bridge_reason)))
                nodes_set.add(e.source)
                nodes_set.add(e.target)

        # Attach containers for any join-partner columns we just pulled in.
        extra_members = [n for n in nodes_set
                         if _node_type(n) in _MEMBER_TYPES and n not in member_set]
        for i in range(0, len(extra_members), chunk_size):
            chunk = extra_members[i:i + chunk_size]
            for e in NetworkEdge.objects.filter(_CONTAINS_Q, target__in=chunk):
                if _edge_is(e, 'contains'):
                    contains_edges.add((e.source, e.target))
                    nodes_set.add(e.source)

    # 3d. Downstream report/usage hierarchy (unified mode only): from every
    # member collected so far, follow model-level usage edges
    # (column/measure -> visual -> page -> report) so PowerBI reports surface as
    # downstream consumer cards. Bounded BFS (members -> visual -> page -> report,
    # +1 slack hop) keeps it cheap. Skipped on a depth-0 focus load (it would pull
    # the whole report hierarchy for a heavily-used measure — the slow path).
    usage_edges = []
    if unified and direction in ('both', 'downstream') and (depth > 0 or full):
        _STRUCT_DOWN = ('PB_VISUAL', 'PB_PAGE', 'PB_REPORT')
        usage_seen = set()
        frontier = {n for n in nodes_set if _node_type(n) in _MEMBER_TYPES}
        for _ in range(4):
            if not frontier:
                break
            next_frontier = set()
            frontier_list = list(frontier)
            for i in range(0, len(frontier_list), chunk_size):
                chunk = frontier_list[i:i + chunk_size]
                for e in NetworkEdge.objects.filter(source__in=chunk):
                    if e.source == e.target:
                        continue
                    if _node_type(e.target) not in _STRUCT_DOWN:
                        continue
                    if (e.kind or _edge_kind(e.source, e.target)) != 'model':
                        continue
                    key = (e.source, e.target)
                    if key in usage_seen:
                        continue
                    usage_seen.add(key)
                    usage_edges.append(key)
                    if e.target not in nodes_set:
                        nodes_set.add(e.target)
                        next_frontier.add(e.target)
            frontier = next_frontier

    # Per-column lineage attributes for the node payload: a column that is the
    # target of a column edge carries that edge's derivation type; any column in
    # a column edge participates in lineage.
    cols_with_lineage = set()
    lineage_by_target = {}
    for (s, t), (lt, _br) in edge_meta.items():
        cols_with_lineage.add(s)
        cols_with_lineage.add(t)
        if lt:
            lineage_by_target[t] = lt

    # 4. Serialize nodes (synthesize placeholders for any id missing a row).
    nodes_data = []
    seen_ids = set()
    nodes_list = list(nodes_set)
    for i in range(0, len(nodes_list), chunk_size):
        for n in NetworkNode.objects.filter(node_id__in=nodes_list[i:i + chunk_size]):
            if n.node_id in seen_ids:
                continue
            seen_ids.add(n.node_id)
            nodes_data.append(_serialize_node(n))
    for nid in nodes_set:
        if nid not in seen_ids:
            seen_ids.add(nid)
            nodes_data.append({"id": nid, "label": nid, "group": _node_type(nid) or "UNKNOWN"})
    _enrich_with_item_metadata(nodes_data)
    for n in nodes_data:
        nid = n.get('id')
        if nid in cols_with_lineage:
            n['hasLineage'] = True
        if nid in lineage_by_target:
            n['lineageType'] = lineage_by_target[nid]

    links = []
    for s, t in column_edges:
        lt, br = edge_meta.get((s, t), (None, False))
        link = {"source": s, "target": t, "kind": "column"}
        if lt:
            link["lineage_type"] = lt
        if br:
            link["bridge"] = True
        links.append(link)
    links += [{"source": s, "target": t, "kind": "contains"} for s, t in contains_edges]
    for s, t, kind, br in structural_edges:
        link = {"source": s, "target": t, "kind": kind}
        if br:
            link["bridge"] = True
        links.append(link)
    # Downstream report-hierarchy usage edges (unified mode only).
    links += [{"source": s, "target": t, "kind": "model"} for s, t in usage_edges]
    return Response({"nodes": nodes_data, "links": links, "mode": "unified" if unified else "column"})


@api_view(['GET'])
def get_network(request):
    """
    Returns graph nodes / edges.

    Node payload shape:
        {"id": "<TYPE::hash>", "label": "<display name>", "group": "<TYPE>"}
    where `id` is the composite, globally-unique identifier stored in
    `NetworkNode.node_id` (mirrors `Item.item_id` for catalog-resident types)
    and `label` is the human-readable name for the UI.

    Query params:
        node_id (str)      — composite id of the center node, or 'ALL' for the
                             whole graph, or omitted for just the dropdown list.
        depth (int)        — BFS radius (clamped to 0..5; 0 = just the focused
                             element + its container, no neighbour expansion).
        direction (str)    — 'both' (default), 'downstream' (follow source→target),
                             or 'upstream' (follow target→source).
        full (bool)        — column/unified modes only: traverse column edges to
                             their transitive ends, ignoring the depth clamp.
    """
    node_id = request.query_params.get('node_id', None)
    try:
        depth = int(request.query_params.get('depth', 1))
    except (TypeError, ValueError):
        depth = 1
    # depth 0 is a valid "focus only" request (just the element + its container),
    # used by the lineage canvas when you first open a node; expansion is then on
    # demand. Clamping to a 1 minimum would silently load a whole 1-hop
    # neighbourhood (incl. the report hierarchy) — slow, and it shoves the focused
    # card far downstream — so allow 0.
    depth = max(0, min(depth, 5))
    direction = request.query_params.get('direction', 'both')
    if direction not in ('both', 'upstream', 'downstream'):
        direction = 'both'
    full = request.query_params.get('full', '').strip().lower() in ('1', 'true', 'yes')

    # 'asset' (default) = model/table-level graph; 'column' = column-level lineage
    # with models rendered as containers (see _column_ego); 'unified' = column
    # lineage plus the downstream PowerBI report hierarchy as consumer cards.
    mode = request.query_params.get('mode', 'asset')
    if mode not in ('asset', 'column', 'unified'):
        mode = 'asset'

    _serialize = _serialize_node

    # Optional search query for the lazy-loading asset dropdown.
    # Returns the top 50 nodes whose name contains the query string (case-insensitive).
    q = request.query_params.get('q', '').strip()

    if not node_id or node_id == 'ALL':
        # Full asset directory for the sidebar tree: every model/table-level node
        # (no columns, no edges), enriched with grouping metadata. Lets the
        # browser show the whole project to browse/search even before any ego
        # graph is loaded. `list=assets`.
        if request.query_params.get('list', '').strip() == 'assets':
            asset_groups = ['DBT_MODEL', 'DBT_SOURCE', 'DBT_SEED', 'PB_TABLE']
            rows = NetworkNode.objects.filter(group__in=asset_groups).values(
                'node_id', 'name', 'group',
            )
            nodes_data = [
                {"id": r["node_id"], "label": r["name"] or r["node_id"], "group": r["group"] or "UNKNOWN"}
                for r in rows
            ]
            _enrich_with_item_metadata(nodes_data)
            return Response({"nodes": nodes_data, "links": []})

        # Lazy-load one container's members (columns / measures / fields) for the
        # sidebar directory tree, so a model/table can expand to its columns
        # without shipping all ~30k members up front. `list=members&parent=<id>`.
        if request.query_params.get('list', '').strip() == 'members':
            # Local import: get_network imports Q later in its body, which makes the
            # name function-local, so we must bind it here before use.
            from django.db.models import Q
            parent_id = request.query_params.get('parent', '').strip()
            member_groups = ['DBT_COLUMN', 'PB_COLUMN', 'PB_MEASURE', 'PB_FIELD']
            child_ids = list(
                NetworkEdge.objects.filter(
                    Q(kind='contains') | Q(kind__isnull=True), source=parent_id,
                ).values_list('target', flat=True)
            )
            rows = NetworkNode.objects.filter(
                node_id__in=child_ids, group__in=member_groups,
            ).values('node_id', 'name', 'group')
            nodes_data = [
                {"id": r["node_id"], "label": r["name"] or r["node_id"], "group": r["group"] or "UNKNOWN"}
                for r in rows
            ]
            _enrich_with_item_metadata(nodes_data)
            nodes_data.sort(key=lambda n: (n.get("label") or "").lower())
            return Response({"nodes": nodes_data, "links": []})

        # Search members (columns / measures / fields) by name and resolve each
        # to its container so the sidebar can nest the hits under the right
        # model/table leaf. `list=member_search&q=<text>`. Empty `q` → no rows.
        if request.query_params.get('list', '').strip() == 'member_search':
            from django.db.models import Q
            q_text = request.query_params.get('q', '').strip()
            if not q_text:
                return Response({"nodes": [], "links": []})
            member_groups = ['DBT_COLUMN', 'PB_COLUMN', 'PB_MEASURE', 'PB_FIELD']
            container_groups = ['DBT_MODEL', 'DBT_SOURCE', 'DBT_SEED', 'PB_TABLE']
            rows = list(
                NetworkNode.objects.filter(
                    name__icontains=q_text, group__in=member_groups,
                ).values('node_id', 'name', 'group')[:200]
            )
            member_ids = [r['node_id'] for r in rows]
            # Resolve each member's container via 'contains' edges (parent→child).
            # Legacy NULL-kind edges are included to match the lazy-members loader,
            # but we keep only sources that are real container nodes so a stray
            # column→column edge can't masquerade as a parent.
            parent_by_child = {}
            if member_ids:
                edges = list(
                    NetworkEdge.objects.filter(
                        Q(kind='contains') | Q(kind__isnull=True),
                        target__in=member_ids,
                    ).values('source', 'target')
                )
                source_ids = {e['source'] for e in edges}
                container_ids = set(
                    NetworkNode.objects.filter(
                        node_id__in=source_ids, group__in=container_groups,
                    ).values_list('node_id', flat=True)
                )
                for e in edges:
                    if e['source'] in container_ids:
                        parent_by_child.setdefault(e['target'], e['source'])
            nodes_data = [
                {
                    "id": r["node_id"],
                    "label": r["name"] or r["node_id"],
                    "group": r["group"] or "UNKNOWN",
                    "container": parent_by_child[r["node_id"]],
                }
                for r in rows
                if r["node_id"] in parent_by_child
            ]
            _enrich_with_item_metadata(nodes_data)
            nodes_data.sort(key=lambda n: (n.get("label") or "").lower())
            return Response({"nodes": nodes_data, "links": []})

        if node_id == 'ALL':
            # Load the entire graph
            nodes_data = [_serialize(n) for n in NetworkNode.objects.all()]
            _enrich_with_item_metadata(nodes_data)
            edges_data = [{"source": e.source, "target": e.target,
                           "kind": _edge_kind(e.source, e.target)}
                          for e in NetworkEdge.objects.all()]
            return Response({"nodes": nodes_data, "links": edges_data})

        # Lazy search: return only nodes matching `q` (for the Select2 ajax dropdown).
        # If `q` is empty, return an empty list — the user must type to search.
        # Optional `group` param scopes results to a specific node type (e.g. MEASURE).
        group_filter = request.query_params.get('group', '').strip().upper()
        if q:
            qs = NetworkNode.objects.filter(name__icontains=q)
            if group_filter:
                qs = qs.filter(group=group_filter)
            qs = qs.values("node_id", "name", "group")[:50]
        else:
            qs = NetworkNode.objects.none()
        nodes_data = [
            {"id": n["node_id"], "label": n["name"] or n["node_id"], "group": n["group"] or "UNKNOWN"}
            for n in qs
        ]
        _enrich_with_item_metadata(nodes_data)
        return Response({"nodes": nodes_data, "links": []})

    if mode == 'column':
        return _column_ego(node_id, depth, direction, full=full)
    if mode == 'unified':
        return _column_ego(node_id, depth, direction, unified=True, full=full)

    # Ego graph with specified depth
    # direction controls which edges are traversed at each BFS step:
    #   'both'       — follow edges in either direction (default)
    #   'downstream' — follow only edges where current node is source (producer→consumer)
    #   'upstream'   — follow only edges where current node is target (consumer→producer)
    nodes_set = {node_id}
    edges_set = set()
    current_layer_nodes = {node_id}

    for _ in range(depth):
        if not current_layer_nodes:
            break

        next_layer_nodes = set()
        chunk_size = 900
        current_list = list(current_layer_nodes)

        for i in range(0, len(current_list), chunk_size):
            chunk = current_list[i:i + chunk_size]

            # Asset view traverses only asset-level edges (model↔model, source→model,
            # report hierarchy, table-level bridges). Column/contains edges are
            # excluded here so a single model hop isn't consumed by its columns —
            # those live in the column view (mode=column). Legacy rows whose
            # `level` was never backfilled are still included so nothing vanishes.
            from django.db.models import Q
            # 'both' edges (measure boxes, field↔measure) are hinges shown in
            # both views; legacy NULL rows are kept so nothing vanishes pre-backfill.
            asset_q = Q(level='asset') | Q(level='both') | Q(level__isnull=True)
            if direction == 'downstream':
                # Only follow edges where current nodes are sources (move toward consumers)
                layer_edges = NetworkEdge.objects.filter(asset_q, source__in=chunk)
            elif direction == 'upstream':
                # Only follow edges where current nodes are targets (move toward producers)
                layer_edges = NetworkEdge.objects.filter(asset_q, target__in=chunk)
            else:
                # Both directions
                layer_edges = (
                    NetworkEdge.objects.filter(asset_q, source__in=chunk)
                    | NetworkEdge.objects.filter(asset_q, target__in=chunk)
                )

            for edge in layer_edges:
                if edge.source == edge.target:
                    continue  # skip self-loops
                edges_set.add((edge.source, edge.target))
                if direction == 'downstream':
                    # We started from source, so the new node is the target
                    if edge.target not in nodes_set:
                        next_layer_nodes.add(edge.target)
                elif direction == 'upstream':
                    # We started from target, so the new node is the source
                    if edge.source not in nodes_set:
                        next_layer_nodes.add(edge.source)
                else:
                    if edge.source not in nodes_set:
                        next_layer_nodes.add(edge.source)
                    if edge.target not in nodes_set:
                        next_layer_nodes.add(edge.target)

        nodes_set.update(next_layer_nodes)
        current_layer_nodes = next_layer_nodes

    # Fetch full NetworkNode rows for every id we collected. Because `node_id`
    # is unique, this yields at most one row per id — no dedup dance required.
    nodes_data = []
    seen_ids = set()
    chunk_size = 900
    nodes_list = list(nodes_set)
    for i in range(0, len(nodes_list), chunk_size):
        chunk = nodes_list[i:i + chunk_size]
        for n in NetworkNode.objects.filter(node_id__in=chunk):
            if n.node_id in seen_ids:
                continue
            seen_ids.add(n.node_id)
            nodes_data.append(_serialize(n))

    # If an edge referenced an id that somehow isn't in NetworkNode (data drift),
    # synthesize a placeholder so the frontend still has a valid node entry.
    for nid in nodes_set:
        if nid not in seen_ids:
            seen_ids.add(nid)
            # Best-effort: parse the TYPE prefix for a reasonable group label.
            group = nid.split("::", 1)[0] if "::" in nid else "UNKNOWN"
            nodes_data.append({"id": nid, "label": nid, "group": group})

    _enrich_with_item_metadata(nodes_data)
    edges_data = [{"source": s, "target": t, "kind": _edge_kind(s, t)} for s, t in edges_set]

    return Response({
        "nodes": nodes_data,
        "links": edges_data,
    })


@api_view(['GET'])
def find_network_path(request):
    """
    Returns the shortest path between two nodes in the lineage graph.

    Delegates BFS to ``catalog.services.network_path.find_shortest_path`` so the
    same logic is reused by the chatbot tool.

    Query params:
        from (str)        — composite node_id of the start node (required)
        to (str)          — composite node_id of the end node (required)
        max_depth (int)   — maximum BFS hops (clamped to 1..30, default 6)
        direction (str)   — 'both' (default), 'downstream', or 'upstream'
        algorithm (str)   — 'all_shortest' (default) returns every distinct
                            shortest path; 'shortest' returns just one.
    """
    from .services.network_path import find_shortest_path

    src = (request.query_params.get('from') or '').strip()
    dst = (request.query_params.get('to') or '').strip()
    if not src or not dst:
        return Response(
            {"found": False, "message": "Both 'from' and 'to' node ids are required.",
             "nodes": [], "links": [], "distance": 0},
            status=400,
        )

    try:
        max_depth = int(request.query_params.get('max_depth', 6))
    except (TypeError, ValueError):
        max_depth = 6
    direction = request.query_params.get('direction', 'both')
    algorithm = request.query_params.get('algorithm', 'all_shortest')
    workspace_id = (request.query_params.get('workspace_id') or '').strip()

    result = find_shortest_path(
        src, dst,
        max_depth=max_depth,
        direction=direction,
        algorithm=algorithm,
        workspace_id=workspace_id,
    )

    nodes_payload = [{"id": n.id, "label": n.label, "group": n.group} for n in result.nodes]
    _enrich_with_item_metadata(nodes_payload)
    return Response({
        "found": result.found,
        "distance": result.distance,
        # Union of all shortest paths — render this as a DAG.
        "nodes": nodes_payload,
        "links": [{"source": s, "target": t} for s, t in result.edges],
        # Each individual shortest path as an ordered list of node_ids
        # (for the per-path summary list in the UI).
        "paths": result.paths,
        "message": result.message,
    })


@api_view(['GET'])
def get_network_reachable(request):
    """
    Returns every node reachable from a given End node in a single direction.

    Used to populate the Path tab's "Start" dropdown once the user has picked
    an "End" — guarantees that whatever Start the user picks has a real path
    back to End, so no path search ever returns "no path".

    Query params:
        from (str)        — composite node_id of the End node (required)
        direction (str)   — 'upstream' (default) or 'downstream'
        workspace_id (str)— optional PowerBI workspace constraint
    """
    from .services.network_path import find_reachable_nodes

    end_id = (request.query_params.get('from') or '').strip()
    if not end_id:
        return Response(
            {"nodes": [], "truncated": False, "message": "'from' node id is required."},
            status=400,
        )
    direction = request.query_params.get('direction', 'upstream')
    workspace_id = (request.query_params.get('workspace_id') or '').strip()

    result = find_reachable_nodes(end_id, direction=direction, workspace_id=workspace_id)

    nodes_payload = [
        {"id": n.id, "label": n.label, "group": n.group, "distance": n.distance}
        for n in result.nodes
    ]
    _enrich_with_item_metadata(nodes_payload)
    return Response({
        "nodes": nodes_payload,
        "truncated": result.truncated,
        "message": result.message,
    })


# ==========================================
# INTEGRATIONS API VIEWS
# ==========================================

def _get_user_org(request):
    """Org for the current user IF they are an org admin, else None.

    Routes through the unified access predicate (catalog/access.py) so these API
    gates and the page-visibility flags can never drift apart. Superusers always
    pass and fall back to the first org.
    """
    from .access import resolve_org, is_org_admin
    org = resolve_org(request.user)
    return org if (org and is_org_admin(request.user, org)) else None


@api_login_required
@api_view(['GET'])
def integrations_get_all(request):
    """Return all integration data for the user's organization."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    sources = IntegrationSource.objects.filter(organization=org).prefetch_related('run_logs')
    sources_data = []
    for s in sources:
        last_log = s.run_logs.first()
        schedule = None
        try:
            sch = s.schedule
            schedule = {
                'frequency': sch.frequency,
                'cron_expression': sch.cron_expression,
                'is_enabled': sch.is_enabled,
                'last_run_at': sch.last_run_at.isoformat() if sch.last_run_at else None,
                'next_run_at': sch.next_run_at.isoformat() if sch.next_run_at else None,
            }
        except Exception:
            pass

        source_entry = {
            'id': s.id,
            'name': s.name,
            'source_type': s.source_type,
            'is_active': s.is_active,
            'tenant_id': s.tenant_id or '',
            'client_id': s.client_id or '',
            'client_secret_set': bool(s.client_secret),
            'workspace_ids': s.workspace_ids or [],
            'default_workspace_id': s.default_workspace_id or '',
            'available_workspaces': get_workspaces_for_source(s),
            # dbt / GitHub fields
            'github_repo_url': s.github_repo_url or '',
            'github_token_set': bool(s.github_token),
            'github_branch': s.github_branch or 'main',
            'dbt_manifest_path': s.dbt_manifest_path or 'target/manifest.json',
            'schedule': schedule,
            'last_run': {
                'status': last_log.status,
                'started_at': last_log.started_at.isoformat(),
                'finished_at': last_log.finished_at.isoformat() if last_log.finished_at else None,
                'triggered_by': last_log.triggered_by,
            } if last_log else None,
        }
        sources_data.append(source_entry)

    destinations = IntegrationDestination.objects.filter(organization=org).prefetch_related('run_logs')
    destinations_data = []
    for d in destinations:
        last_log = d.run_logs.first()
        schedule = None
        try:
            sch = d.schedule
            schedule = {
                'frequency': sch.frequency,
                'cron_expression': sch.cron_expression,
                'is_enabled': sch.is_enabled,
                'last_run_at': sch.last_run_at.isoformat() if sch.last_run_at else None,
                'next_run_at': sch.next_run_at.isoformat() if sch.next_run_at else None,
            }
        except Exception:
            pass

        destinations_data.append({
            'id': d.id,
            'name': d.name,
            'destination_type': d.destination_type,
            'is_active': d.is_active,
            'bq_project_id': d.bq_project_id or '',
            'bq_dataset_id': d.bq_dataset_id or '',
            'bq_service_account_set': bool(d.bq_service_account_json),
            'schedule': schedule,
            'last_run': {
                'status': last_log.status,
                'started_at': last_log.started_at.isoformat(),
                'finished_at': last_log.finished_at.isoformat() if last_log.finished_at else None,
                'triggered_by': last_log.triggered_by,
            } if last_log else None,
        })

    hooks = IntegrationHook.objects.filter(organization=org)
    hooks_data = [{
        'id': h.id,
        'name': h.name,
        'hook_type': h.hook_type,
        'is_active': h.is_active,
        'slack_bot_token_set': bool(h.slack_bot_token),
        'slack_channel': h.slack_channel or '',
        'slack_alerts_channel': h.slack_alerts_channel or '',
    } for h in hooks]

    return Response({
        'sources': sources_data,
        'destinations': destinations_data,
        'hooks': hooks_data,
    })


@api_login_required
@api_view(['POST'])
def integrations_save_source(request):
    """Create or update an IntegrationSource."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    source_id = data.get('id')

    if source_id:
        source = IntegrationSource.objects.filter(id=source_id, organization=org).first()
        if not source:
            return Response({'error': 'Source not found'}, status=404)
    else:
        source_type = data.get('source_type', 'powerbi_fabric')
        source = IntegrationSource(
            organization=org,
            source_type=source_type,
            category=IntegrationSource.DEFAULT_CATEGORY_BY_TYPE.get(
                source_type, IntegrationSource.CATEGORY_VISUALIZATION,
            ),
        )

    source.name = data.get('name', source.name)
    source.is_active = data.get('is_active', source.is_active)

    if source.source_type == 'dbt':
        # dbt / GitHub fields
        repo_url = data.get('github_repo_url', '')
        if repo_url:
            source.github_repo_url = str(repo_url).strip()
        new_token = data.get('github_token', '')
        if new_token:
            source.github_token = str(new_token).strip()
        branch = data.get('github_branch', '')
        if branch:
            source.github_branch = str(branch).strip()
        manifest = data.get('dbt_manifest_path', '')
        if manifest:
            source.dbt_manifest_path = str(manifest).strip()
    else:
        # PowerBI / Fabric fields
        t_id = data.get('tenant_id', '')
        if t_id:
            source.tenant_id = str(t_id).strip().strip("'").strip('"')

        c_id = data.get('client_id', '')
        if c_id:
            source.client_id = str(c_id).strip().strip("'").strip('"')

        # Only update secret if a new one is provided
        new_secret = data.get('client_secret', '')
        if new_secret:
            source.client_secret = str(new_secret).strip().strip("'").strip('"')

        # workspace_ids: accept comma-separated string or list
        ws_ids = data.get('workspace_ids', source.workspace_ids)
        if isinstance(ws_ids, str):
            ws_ids = [w.strip().strip("'").strip('"') for w in ws_ids.split(',') if w.strip()]
        elif isinstance(ws_ids, list):
            ws_ids = [str(w).strip().strip("'").strip('"') for w in ws_ids]
        source.workspace_ids = ws_ids

        if 'default_workspace_id' in data:
            source.default_workspace_id = (data.get('default_workspace_id') or '').strip() or None

    source.save()

    # Upsert schedule
    frequency = data.get('schedule_frequency', 'manual')
    cron_expr = data.get('schedule_cron', '')
    schedule_enabled = data.get('schedule_enabled', False)

    schedule, _ = SourceSchedule.objects.get_or_create(source=source)
    schedule.frequency = frequency
    schedule.cron_expression = cron_expr if frequency == 'custom' else _frequency_to_cron(frequency, data)
    schedule.is_enabled = schedule_enabled
    schedule.save()

    # Update Django-Q schedule
    _update_django_q_schedule(source, schedule)

    return Response({'status': 'saved', 'id': source.id})


def _frequency_to_cron(frequency, data):
    """Convert friendly frequency to cron expression."""
    if frequency == 'daily':
        hour = data.get('schedule_hour', '2')
        return f'0 {hour} * * *'
    elif frequency == 'weekly':
        hour = data.get('schedule_hour', '2')
        day = data.get('schedule_day', '1')  # 1=Monday
        return f'0 {hour} * * {day}'
    return None


def _update_django_q_schedule(source, schedule):
    """Create/update/delete a Django-Q scheduled task for this source."""
    try:
        from django_q.models import Schedule

        task_name = f'source_run_{source.id}'
        Schedule.objects.filter(name=task_name).delete()

        if schedule.is_enabled and schedule.cron_expression:
            Schedule.objects.create(
                name=task_name,
                func='catalog.integration_tasks.run_source_task',
                args=f'{source.id}',
                kwargs='{"triggered_by": "scheduler"}',
                schedule_type=Schedule.CRON,
                cron=schedule.cron_expression,
            )
    except Exception:
        # A failure here (e.g. missing croniter, invalid cron) used to be silently
        # swallowed, leaving the source looking scheduled while no Django-Q schedule
        # existed. Log it so registration failures are visible.
        logger.exception('Failed to register Django-Q schedule for source %s', source.id)


@api_login_required
@api_view(['POST'])
def integrations_run_source_now(request, source_id):
    """Trigger an immediate run of a source via Django-Q background task."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    source = IntegrationSource.objects.filter(id=source_id, organization=org).first()
    if not source:
        return Response({'error': 'Source not found'}, status=404)

    # Check if there is already a running task
    running_log = SourceRunLog.objects.filter(source=source, status='running').first()
    if running_log:
        return Response({
            'error': 'A run is already in progress. Please wait for it to complete.'
        }, status=400)

    # Create a 'running' log entry upfront so the task can find and update it
    run_log = SourceRunLog.objects.create(
        source=source,
        status='running',
        triggered_by='manual',
    )

    # Enqueue the background task — catalog.integration_tasks.run_source_task
    # will handle extract → transform → load_data → BigQuery push automatically
    task_id = async_task(
        'catalog.integration_tasks.run_source_task',
        source.id,
        'manual',
    )

    return Response({'status': 'queued', 'run_log_id': run_log.id, 'task_id': task_id})


@api_login_required
@api_view(['POST'])
def integrations_test_source(request, source_id):
    """
    Lightweight connectivity test — no data is downloaded or stored.
    - powerbi_fabric: OAuth token + workspace listing
    - dbt:           git ls-remote to verify repo / branch access
    Returns {status: 'ok'|'fail', lines: [...]}
    """
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    source = IntegrationSource.objects.filter(id=source_id, organization=org).first()
    if not source:
        return Response({'error': 'Source not found'}, status=404)

    from etl.sources.registry import get_source
    src = get_source(source)
    result = src.test()
    return Response(result)


@api_login_required
@api_view(['POST'])
def integrations_test_destination(request, dest_id):
    """
    Lightweight connectivity test for a BigQuery destination.
    Validates credentials and checks project/dataset accessibility without writing any data.
    Returns {status: 'ok'|'fail', lines: [...]}
    """
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    dest = IntegrationDestination.objects.filter(id=dest_id, organization=org).first()
    if not dest:
        return Response({'error': 'Destination not found'}, status=404)

    from etl.destinations.registry import get_destination
    dst = get_destination(dest)
    result = dst.test()
    return Response(result)


@api_login_required
@api_view(['GET'])
def integrations_get_run_logs(request, source_id):
    """Get run logs for a source."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    source = IntegrationSource.objects.filter(id=source_id, organization=org).first()
    if not source:
        return Response({'error': 'Source not found'}, status=404)

    logs = SourceRunLog.objects.filter(source=source).order_by('-started_at')[:20]
    data = [{
        'id': log.id,
        'status': log.status,
        'started_at': log.started_at.isoformat(),
        'finished_at': log.finished_at.isoformat() if log.finished_at else None,
        'triggered_by': log.triggered_by,
        'duration_seconds': int((log.finished_at - log.started_at).total_seconds()) if log.finished_at else None,
    } for log in logs]
    return Response(data)


@api_login_required
@api_view(['GET'])
def integrations_get_run_log_detail(request, log_id):
    """Get full log output for a specific run."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    log = SourceRunLog.objects.filter(id=log_id, source__organization=org).first()
    if not log:
        return Response({'error': 'Log not found'}, status=404)

    return Response({
        'id': log.id,
        'status': log.status,
        'started_at': log.started_at.isoformat(),
        'finished_at': log.finished_at.isoformat() if log.finished_at else None,
        'triggered_by': log.triggered_by,
        'log_output': log.log_output or '',
    })


@api_login_required
@api_view(['POST'])
def integrations_save_destination(request):
    """Create or update an IntegrationDestination."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    dest_id = data.get('id')

    if dest_id:
        dest = IntegrationDestination.objects.filter(id=dest_id, organization=org).first()
        if not dest:
            return Response({'error': 'Destination not found'}, status=404)
    else:
        dest = IntegrationDestination(organization=org, destination_type='bigquery')

    dest.name = data.get('name', dest.name)
    dest.is_active = data.get('is_active', dest.is_active)
    dest.bq_dataset_id = data.get('bq_dataset_id', dest.bq_dataset_id)

    # Parse service account JSON and extract project_id automatically
    sa_json_str = data.get('bq_service_account_json', '').strip()
    if sa_json_str:
        try:
            sa_info = json.loads(sa_json_str)
            dest.bq_project_id = sa_info.get('project_id', dest.bq_project_id)
            dest.bq_service_account_json = sa_json_str
        except json.JSONDecodeError:
            return Response({'error': 'Invalid service account JSON'}, status=400)

    dest.save()

    # Upsert schedule
    frequency = data.get('schedule_frequency', 'manual')
    cron_expr = data.get('schedule_cron', '')
    schedule_enabled = data.get('schedule_enabled', False)

    schedule, _ = DestinationSchedule.objects.get_or_create(destination=dest)
    schedule.frequency = frequency
    schedule.cron_expression = cron_expr if frequency == 'custom' else _frequency_to_cron(frequency, data)
    schedule.is_enabled = schedule_enabled
    schedule.save()

    # Update Django-Q schedule for destination
    try:
        from django_q.models import Schedule
        task_name = f'dest_run_{dest.id}'
        Schedule.objects.filter(name=task_name).delete()
        if schedule.is_enabled and schedule.cron_expression:
            Schedule.objects.create(
                name=task_name,
                func='catalog.integration_tasks.run_destination_task',
                args=f'{dest.id}',
                kwargs='{"triggered_by": "scheduler"}',
                schedule_type=Schedule.CRON,
                cron=schedule.cron_expression,
            )
    except Exception:
        logger.exception('Failed to register Django-Q schedule for destination %s', dest.id)

    return Response({'status': 'saved', 'id': dest.id, 'bq_project_id': dest.bq_project_id})


@api_login_required
@api_view(['POST'])
def integrations_run_destination_now(request, dest_id):
    """Trigger an immediate run of a destination via Django-Q background task."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    dest = IntegrationDestination.objects.filter(id=dest_id, organization=org).first()
    if not dest:
        return Response({'error': 'Destination not found'}, status=404)

    running_log = DestinationRunLog.objects.filter(destination=dest, status='running').first()
    if running_log:
        return Response({
            'error': 'A run is already in progress. Please wait for it to complete.'
        }, status=400)

    run_log = DestinationRunLog.objects.create(
        destination=dest,
        status='running',
        triggered_by='manual',
    )

    task_id = async_task(
        'catalog.integration_tasks.run_destination_task',
        dest.id,
        'manual',
    )

    return Response({'status': 'queued', 'run_log_id': run_log.id, 'task_id': task_id})


@api_login_required
@api_view(['GET'])
def integrations_get_dest_logs(request, dest_id):
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    dest = IntegrationDestination.objects.filter(id=dest_id, organization=org).first()
    if not dest:
        return Response({'error': 'Destination not found'}, status=404)

    logs = DestinationRunLog.objects.filter(destination=dest).order_by('-started_at')[:20]
    data = [{
        'id': log.id,
        'status': log.status,
        'started_at': log.started_at.isoformat(),
        'finished_at': log.finished_at.isoformat() if log.finished_at else None,
        'triggered_by': log.triggered_by,
        'duration_seconds': int((log.finished_at - log.started_at).total_seconds()) if log.finished_at else None,
    } for log in logs]
    return Response(data)


@api_login_required
@api_view(['GET'])
def integrations_get_dest_log_detail(request, log_id):
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    log = DestinationRunLog.objects.filter(id=log_id, destination__organization=org).first()
    if not log:
        return Response({'error': 'Log not found'}, status=404)

    return Response({
        'id': log.id,
        'status': log.status,
        'started_at': log.started_at.isoformat(),
        'finished_at': log.finished_at.isoformat() if log.finished_at else None,
        'triggered_by': log.triggered_by,
        'log_output': log.log_output or '',
    })


@api_login_required
@api_view(['POST'])
def integrations_kill_dest_run(request, log_id):
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    log = DestinationRunLog.objects.filter(id=log_id, destination__organization=org).first()
    if not log:
        return Response({'error': 'Log not found'}, status=404)

    if log.status in ['running', 'queued']:
        # Cooperative cancel: ask the worker to stop at its next checkpoint so it
        # doesn't overwrite this 'failed' status with its own result later.
        from .integration_tasks import request_destination_cancel
        request_destination_cancel(log.destination_id)
        log.status = 'failed'
        log.log_output = (log.log_output or '') + "\n\n[System] Run killed by user."
        log.save(update_fields=['status', 'log_output'])
        return Response({'status': 'killed'})
    return Response({'error': 'Run is not active'}, status=400)


@api_login_required
@api_view(['DELETE'])
def integrations_delete_dest_run(request, log_id):
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    log = DestinationRunLog.objects.filter(id=log_id, destination__organization=org).first()
    if not log:
        return Response({'error': 'Log not found'}, status=404)

    log.delete()
    return Response({'status': 'deleted'})


@api_login_required
@api_view(['POST'])
def integrations_kill_run(request, log_id):
    """Mark a run log as failed (killed by user)."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    log = SourceRunLog.objects.filter(id=log_id, source__organization=org).first()
    if not log:
        return Response({'error': 'Log not found'}, status=404)

    if log.status in ['running', 'queued']:
        # Cooperative cancel: ask the worker to stop at its next checkpoint so it
        # doesn't overwrite this 'failed' status with its own result later.
        from .integration_tasks import request_source_cancel
        request_source_cancel(log.source_id)
        log.status = 'failed'
        log.log_output = (log.log_output or '') + "\n\n[System] Run killed by user."
        log.save(update_fields=['status', 'log_output'])
        return Response({'status': 'killed'})
    return Response({'error': 'Run is not active'}, status=400)


@api_login_required
@api_view(['DELETE'])
def integrations_delete_run(request, log_id):
    """Delete a run log."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    log = SourceRunLog.objects.filter(id=log_id, source__organization=org).first()
    if not log:
        return Response({'error': 'Log not found'}, status=404)

    log.delete()
    return Response({'status': 'deleted'})


@api_login_required
@api_view(['POST'])
def integrations_save_hook(request):
    """Create or update an IntegrationHook."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    hook_id = data.get('id')

    if hook_id:
        hook = IntegrationHook.objects.filter(id=hook_id, organization=org).first()
        if not hook:
            return Response({'error': 'Hook not found'}, status=404)
    else:
        hook = IntegrationHook(organization=org, hook_type='slack')

    hook.name = data.get('name', hook.name)
    hook.is_active = data.get('is_active', hook.is_active)

    # Update channel fields
    if 'slack_channel' in data:
        hook.slack_channel = data['slack_channel']
    if 'slack_alerts_channel' in data:
        hook.slack_alerts_channel = data['slack_alerts_channel']

    new_token = data.get('slack_bot_token', '').strip()
    if new_token:
        hook.slack_bot_token = new_token
        # Keep Organization.slack_bot_token in sync for bot hook
        if hook.hook_type == 'slack':
            org.slack_bot_token = new_token
            org.save(update_fields=['slack_bot_token'])
    elif data.get('disconnect'):
        hook.slack_bot_token = ''
        hook.is_active = False
        if hook.hook_type == 'slack':
            org.slack_bot_token = ''
            org.save(update_fields=['slack_bot_token'])
    elif hook.hook_type == 'slack_alerts' and not hook.slack_bot_token:
        # For slack_alerts hook, inherit the token from the slack bot hook if not set
        slack_hook = IntegrationHook.objects.filter(organization=org, hook_type='slack').first()
        if slack_hook and slack_hook.slack_bot_token:
            hook.slack_bot_token = slack_hook.slack_bot_token

    hook.save()
    return Response({'status': 'saved', 'id': hook.id})


# ==========================================
# WORKFLOW API VIEWS
# ==========================================

@api_login_required
@api_view(['GET'])
def workflow_get_status(request):
    """Return workflow data: schedule, last run, recent runs, source/dest summary."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    # Schedule
    schedule_data = None
    try:
        sched = WorkflowSchedule.objects.get(organization=org)
        schedule_data = {
            'frequency': sched.frequency,
            'cron_expression': sched.cron_expression,
            'is_enabled': sched.is_enabled,
            'last_run_at': sched.last_run_at.isoformat() if sched.last_run_at else None,
            'next_run_at': sched.next_run_at.isoformat() if sched.next_run_at else None,
        }
    except WorkflowSchedule.DoesNotExist:
        pass

    # Recent runs
    runs = WorkflowRun.objects.filter(organization=org).order_by('-started_at')[:10]
    runs_data = [{
        'id': r.id,
        'status': r.status,
        'current_stage': r.current_stage,
        'triggered_by': r.triggered_by,
        'started_at': r.started_at.isoformat(),
        'finished_at': r.finished_at.isoformat() if r.finished_at else None,
        'duration_seconds': int((r.finished_at - r.started_at).total_seconds()) if r.finished_at else None,
    } for r in runs]

    # Source/destination summaries for the pipeline visual. Sort by
    # (category, name) so the DAG renders transformation sources before
    # visualization sources — same ordering the workflow runner uses.
    _CATEGORY_ORDER = {
        IntegrationSource.CATEGORY_TRANSFORMATION: 0,
        IntegrationSource.CATEGORY_VISUALIZATION: 1,
    }
    sources = sorted(
        IntegrationSource.objects.filter(organization=org),
        key=lambda s: (_CATEGORY_ORDER.get(s.category, 99), s.name),
    )
    sources_summary = [{
        'id': s.id,
        'name': s.name,
        'source_type': s.source_type,
        'category': s.category,
        'is_active': s.is_active,
        'last_status': s.run_logs.first().status if s.run_logs.exists() else None,
    } for s in sources]

    destinations = IntegrationDestination.objects.filter(organization=org)
    dests_summary = [{
        'id': d.id,
        'name': d.name,
        'destination_type': d.destination_type,
        'is_active': d.is_active,
        'last_status': d.run_logs.first().status if d.run_logs.exists() else None,
    } for d in destinations]

    # Raw export (Advanced Settings)
    raw_export_data = {
        'is_active': False,
        'gcs_bucket_name': '',
        'gcs_service_account_set': False,
    }
    re_obj = WorkflowRawExport.objects.filter(organization=org).first()
    if re_obj:
        raw_export_data = {
            'is_active': re_obj.is_active,
            'gcs_bucket_name': re_obj.gcs_bucket_name or '',
            'gcs_service_account_set': bool(re_obj.gcs_service_account_json),
        }

    return Response({
        'schedule': schedule_data,
        'runs': runs_data,
        'sources': sources_summary,
        'destinations': dests_summary,
        'raw_export': raw_export_data,
    })


@api_login_required
@api_view(['POST'])
def workflow_save_raw_export(request):
    """Save the workflow raw-export (GCS) advanced settings."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    re_obj, _ = WorkflowRawExport.objects.get_or_create(organization=org)
    re_obj.is_active = bool(data.get('is_active', False))
    re_obj.gcs_bucket_name = (data.get('gcs_bucket_name') or '').strip() or None

    # Only overwrite the saved JSON when the user actually pasted a new value.
    new_sa = data.get('gcs_service_account_json')
    if new_sa is not None and new_sa != '':
        re_obj.gcs_service_account_json = new_sa
    re_obj.save()

    return Response({
        'status': 'saved',
        'is_active': re_obj.is_active,
        'gcs_bucket_name': re_obj.gcs_bucket_name or '',
        'gcs_service_account_set': bool(re_obj.gcs_service_account_json),
    })


@api_login_required
@api_view(['POST'])
def workflow_test_raw_export(request):
    """Connectivity test for the configured GCS bucket. Uses the saved JSON
    unless the request body provides a fresh one (so the user can test before
    saving)."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    re_obj = WorkflowRawExport.objects.filter(organization=org).first()
    bucket = (data.get('gcs_bucket_name') or (re_obj.gcs_bucket_name if re_obj else '') or '').strip()
    sa_json = data.get('gcs_service_account_json') or (re_obj.gcs_service_account_json if re_obj else '') or ''

    from etl.destinations.gcs.raw_export import test_gcs_connection
    result = test_gcs_connection(bucket, sa_json)
    return Response(result)


@api_login_required
@api_view(['POST'])
def workflow_run_now(request):
    """Trigger a full workflow pipeline via Django-Q."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    # Check for already running workflow
    running = WorkflowRun.objects.filter(organization=org, status='running').first()
    if running:
        return Response({
            'error': 'A workflow is already running. Please wait for it to complete.'
        }, status=400)

    wf = WorkflowRun.objects.create(
        organization=org,
        status='pending',
        current_stage='pending',
        triggered_by='manual',
    )

    task_id = async_task(
        'catalog.integration_tasks.run_workflow_task',
        wf.id,
        'manual',
    )
    cache.set(f'workflow_task_{wf.id}', task_id, timeout=86400)

    return Response({'status': 'queued', 'workflow_run_id': wf.id, 'task_id': task_id})


@api_login_required
@api_view(['GET'])
def workflow_get_run_detail(request, run_id):
    """Get full details of a workflow run (for polling and log viewing)."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    wf = WorkflowRun.objects.filter(id=run_id, organization=org).first()
    if not wf:
        return Response({'error': 'Workflow run not found'}, status=404)

    return Response({
        'id': wf.id,
        'status': wf.status,
        'current_stage': wf.current_stage,
        'triggered_by': wf.triggered_by,
        'started_at': wf.started_at.isoformat(),
        'finished_at': wf.finished_at.isoformat() if wf.finished_at else None,
        'log_output': wf.log_output or '',
    })


@api_login_required
@api_view(['POST'])
def workflow_save_schedule(request):
    """Save the workflow schedule."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    frequency = data.get('frequency', 'manual')
    schedule_enabled = data.get('schedule_enabled', False)

    sched, _ = WorkflowSchedule.objects.get_or_create(organization=org)
    sched.frequency = frequency
    sched.cron_expression = (
        data.get('cron_expression', '')
        if frequency == 'custom'
        else _frequency_to_cron(frequency, data)
    )
    sched.is_enabled = schedule_enabled
    sched.save()

    # Update Django-Q schedule
    from django_q.models import Schedule
    task_name = f'workflow_run_{org.id}'
    try:
        Schedule.objects.filter(name=task_name).delete()
        if sched.is_enabled and sched.cron_expression:
            Schedule.objects.create(
                name=task_name,
                func='catalog.integration_tasks.run_workflow_scheduled',
                args=f'{org.id}',
                kwargs=json.dumps({'triggered_by': 'scheduler'}),
                schedule_type=Schedule.CRON,
                cron=sched.cron_expression,
            )
    except Exception:
        logger.exception('Failed to register Django-Q workflow schedule for org %s', org.id)
        return Response(
            {'status': 'error', 'detail': 'Schedule could not be registered; see server logs.'},
            status=500,
        )

    return Response({'status': 'saved'})


@api_login_required
@api_view(['POST'])
def workflow_kill_run(request, run_id):
    """Request cancellation of a running workflow and stop queued work if possible."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    wf = WorkflowRun.objects.filter(id=run_id, organization=org).first()
    if not wf:
        return Response({'error': 'Workflow run not found'}, status=404)

    if wf.status in ['running', 'pending']:
        from django.utils import timezone as tz
        from django_q.models import OrmQ

        # Cooperative stop flag read by catalog.integration_tasks.run_workflow_task.
        cache.set(f'workflow_cancel_{wf.id}', True, timeout=86400)

        # If Django-Q has not picked the task up yet, remove it from the queue.
        task_id = cache.get(f'workflow_task_{wf.id}')
        queued_deleted = 0
        if task_id:
            queued_deleted, _ = OrmQ.objects.filter(key=task_id).delete()

        # Mark the workflow and any active child logs as failed immediately so the
        # UI reflects the stop request even if the worker is currently inside a
        # long-running API call and reaches the next cancellation checkpoint later.
        wf.status = 'failed'
        wf.current_stage = 'done'
        wf.log_output = (wf.log_output or '') + '\n\n[System] Workflow killed by user.'
        if queued_deleted:
            wf.log_output += f'\n[System] Removed queued Django-Q task {task_id} before it started.'
        else:
            wf.log_output += '\n[System] Stop signal sent. Any active step will stop at the next cancellation checkpoint.'
        wf.finished_at = tz.now()
        wf.save(update_fields=['status', 'current_stage', 'log_output', 'finished_at'])

        SourceRunLog.objects.filter(
            source__organization=org,
            status='running',
            triggered_by__startswith='workflow:',
        ).update(
            status='failed',
            finished_at=tz.now(),
            log_output='[System] Run killed by user via workflow stop.',
        )
        DestinationRunLog.objects.filter(
            destination__organization=org,
            status='running',
            triggered_by__startswith='workflow:',
        ).update(
            status='failed',
            finished_at=tz.now(),
            log_output='[System] Run killed by user via workflow stop.',
        )

        return Response({'status': 'killed', 'queued_deleted': queued_deleted})
    return Response({'error': 'Workflow is not active'}, status=400)


@api_login_required
@api_view(['DELETE'])
def workflow_delete_run(request, run_id):
    """Delete a workflow run log."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    wf = WorkflowRun.objects.filter(id=run_id, organization=org).first()
    if not wf:
        return Response({'error': 'Workflow run not found'}, status=404)

    wf.delete()
    return Response({'status': 'deleted'})


@api_login_required
@api_view(['POST'])
def workflow_toggle_step(request):
    """Toggle is_active for a source or destination from the Workflow UI."""
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    data = request.data
    step_type = data.get('type')   # 'source' or 'destination'
    step_id = data.get('id')
    is_active = data.get('is_active', True)

    if step_type == 'source':
        obj = IntegrationSource.objects.filter(id=step_id, organization=org).first()
    elif step_type == 'destination':
        obj = IntegrationDestination.objects.filter(id=step_id, organization=org).first()
    else:
        return Response({'error': 'Invalid type'}, status=400)

    if not obj:
        return Response({'error': 'Not found'}, status=404)

    obj.is_active = is_active
    obj.save(update_fields=['is_active'])
    return Response({'status': 'toggled', 'is_active': obj.is_active})


@api_login_required
@api_view(['POST'])
def integrations_clean_logs(request):
    """
    Delete ALL run logs for this organisation (source logs, destination logs,
    and workflow run records).  Only org admins can call this.
    """
    org = _get_user_org(request)
    if not org:
        return Response({'error': 'Not an organization admin'}, status=403)

    from catalog.models import SourceRunLog, DestinationRunLog, WorkflowRun

    src_count, _  = SourceRunLog.objects.filter(source__organization=org).delete()
    dest_count, _ = DestinationRunLog.objects.filter(destination__organization=org).delete()
    wf_count, _   = WorkflowRun.objects.filter(organization=org).delete()

    return Response({
        'status': 'ok',
        'deleted': {
            'source_logs': src_count,
            'destination_logs': dest_count,
            'workflow_runs': wf_count,
        }
    })


# ---------------------------------------------------------------------------
# Governance CSV round-trip (Data Dictionary)
# ---------------------------------------------------------------------------
# Governance lives on ItemGroup and is shared by every Item in the group, so
# the CSV is one row PER GROUP (not per item) and is matched back by the
# group. Download and upload share the exact same columns so a file
# round-trips. group_pk is the stable numeric key; group_id (group_key) is
# the human-facing fallback the user edits by.

_GOV_CSV_COLUMNS = [
    'group_pk', 'group_id', 'kind', 'name', 'service', 'item_type',
    'status', 'owner', 'steward', 'department', 'category',
    'custom_description',
]
_GOV_STATUS_VALUES = {c[0] for c in Item.STATUS_CHOICES}


def _gov_csv_groups(user):
    """Org-scoped ItemGroup queryset annotated with a representative item's
    name/service/type for the read-only context columns."""
    from django.db.models import OuterRef, Subquery
    org = _get_user_organization(user)
    qs = ItemGroup.objects.select_related(
        'ownership_person', 'steward', 'ownership_department', 'category',
        'primary_item',
    )
    if org is not None:
        qs = qs.filter(Q(organization=org) | Q(organization__isnull=True))
    any_item = Item.objects.filter(item_group_id=OuterRef('pk')).order_by('item_name')
    qs = qs.annotate(
        _any_name=Subquery(any_item.values('item_name')[:1]),
        _any_service=Subquery(any_item.values('service')[:1]),
        _any_type=Subquery(any_item.values('item_type')[:1]),
    ).order_by('kind', 'group_key')
    return org, qs


class _CsvEcho:
    """A file-like object that just returns what it's given (for streaming)."""
    def write(self, value):
        return value


@api_view(['GET'])
def governance_export_csv(request):
    """Download every ItemGroup's governance as CSV (one row per group)."""
    if not request.user.is_authenticated:
        return Response({'error': 'Unauthorized'}, status=401)

    _org, groups = _gov_csv_groups(request.user)
    writer = csv.writer(_CsvEcho())

    def _stream():
        yield '﻿'  # BOM so Excel opens UTF-8 correctly
        # Excel honours a leading "sep=" line and uses it as the delimiter
        # regardless of the OS locale's list separator, so an EU-locale user
        # gets correctly split columns on a plain double-click. The importer
        # strips this line on round-trip.
        yield 'sep=,\r\n'
        yield writer.writerow(_GOV_CSV_COLUMNS)
        for g in groups.iterator(chunk_size=1000):
            pi = g.primary_item
            yield writer.writerow([
                g.id,
                g.group_key,
                g.kind,
                (pi.item_name if pi else g._any_name) or '',
                (pi.service if pi else g._any_service) or '',
                (pi.item_type if pi else g._any_type) or '',
                g.status or '',
                g.ownership_person.name if g.ownership_person else '',
                g.steward.name if g.steward else '',
                g.ownership_department.name if g.ownership_department else '',
                g.category.name if g.category else '',
                (g.custom_description or '').replace('\r\n', ' ').replace('\n', ' ').strip(),
            ])

    from django.utils import timezone
    resp = StreamingHttpResponse(_stream(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = (
        f'attachment; filename="governance_{timezone.now():%Y-%m-%d}.csv"'
    )
    return resp


def _resolve_named(model, name, org):
    """Case-insensitive, org-scoped name lookup against EXISTING records only.
    Returns (obj, 'ok' | 'missing' | 'ambiguous')."""
    qs = model.objects.filter(name__iexact=name.strip())
    scoped = list((qs.filter(organization=org) if org is not None else qs)[:2])
    if not scoped and org is not None:
        scoped = list(qs.filter(organization__isnull=True)[:2])
    if not scoped:
        return None, 'missing'
    if len(scoped) > 1:
        return None, 'ambiguous'
    return scoped[0], 'ok'


@api_view(['POST'])
@parser_classes([MultiPartParser, FormParser])
def governance_import_csv(request):
    """Upload a governance CSV (same columns as the export). Each row is
    matched to an ItemGroup by `group_pk`, falling back to `group_id`
    (group_key). EMPTY cells leave the existing value unchanged
    (non-destructive). Owner / steward / department / category are matched by
    name against existing records only — unknown names are skipped and
    reported, never created."""
    if not request.user.is_authenticated:
        return Response({'error': 'Unauthorized'}, status=401)

    upload = request.FILES.get('file')
    if not upload:
        return Response(
            {'error': 'No file uploaded (expected multipart form field "file").'},
            status=400,
        )
    # Excel re-saves CSVs in the OS locale's encoding (Western EU -> cp1252,
    # Greek -> cp1253), dropping the UTF-8 BOM. cp1252 and cp1253 cannot be
    # told apart by decode-success alone (cp1253 Greek bytes still "decode"
    # as cp1252 mojibake), so use charset-normalizer's script-coherence
    # detection. Fall back to a static chain if the library is unavailable.
    raw = upload.read()
    text = None
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(
            raw, cp_isolation=['utf_8', 'cp1252', 'cp1253', 'cp1250',
                               'iso8859_1'],
        ).best()
        if best is not None:
            text = str(best)
    except Exception:
        text = None
    if text is None:
        for enc in ('utf-8-sig', 'cp1252', 'cp1253', 'latin-1'):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    if text is None:  # latin-1 never fails, but keep the guard for safety
        return Response({'error': 'File must be a UTF-8 encoded CSV.'}, status=400)
    if text[:1] == '﻿':  # strip any BOM the detector left in place
        text = text[1:]

    # Our export prepends an Excel "sep=," hint line. Excel (and LibreOffice)
    # may keep it as a literal first row on re-save, so drop a leading
    # "sep=<char>" line before parsing.
    sep_match = re.match(r'sep=(.)\r?\n', text, re.IGNORECASE)
    if sep_match:
        text = text[sep_match.end():]

    # Excel also rewrites with the locale's list separator (';' or tab in many
    # EU locales) instead of ','. Sniff it from the header rather than assume.
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t|').delimiter
    except csv.Error:
        first_line = next(iter(sample.splitlines()), '')
        delimiter = max(',;\t|', key=first_line.count)

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    cols = reader.fieldnames or []
    if 'group_id' not in cols and 'group_pk' not in cols:
        return Response(
            {'error': 'CSV must contain a "group_id" (or "group_pk") column. '
                      'Use a file produced by Download CSV.'},
            status=400,
        )

    org = _get_user_organization(request.user)
    base = ItemGroup.objects.all()
    if org is not None:
        base = base.filter(Q(organization=org) | Q(organization__isnull=True))

    report = {
        'total_rows': 0, 'updated': 0,
        'skipped_no_match': [], 'unmatched_values': [],
        'invalid_status': [], 'ambiguous': [],
    }

    for idx, row in enumerate(reader, start=2):  # row 1 is the header
        report['total_rows'] += 1
        key = (row.get('group_id') or '').strip()
        pk = (row.get('group_pk') or '').strip()

        grp = None
        if pk.isdigit():
            grp = base.filter(pk=int(pk)).first()
        if grp is None and key:
            grp = base.filter(group_key=key).first()
        if grp is None:
            report['skipped_no_match'].append({'row': idx, 'group_id': key or pk})
            continue

        changed = False

        status = (row.get('status') or '').strip()
        if status:
            su = status.upper()
            if su in _GOV_STATUS_VALUES:
                if grp.status != su:
                    grp.status = su
                    changed = True
            else:
                report['invalid_status'].append(
                    {'row': idx, 'group_id': key, 'value': status})

        for field, attr, model in (
            ('owner', 'ownership_person', DataPerson),
            ('steward', 'steward', DataPerson),
            ('department', 'ownership_department', Department),
            ('category', 'category', Category),
        ):
            val = (row.get(field) or '').strip()
            if not val:
                continue
            obj, st = _resolve_named(model, val, org)
            if st == 'ok':
                if getattr(grp, f'{attr}_id') != obj.id:
                    setattr(grp, attr, obj)
                    changed = True
            elif st == 'ambiguous':
                report['ambiguous'].append({'row': idx, 'field': field, 'value': val})
            else:
                report['unmatched_values'].append(
                    {'row': idx, 'field': field, 'value': val})

        cd = row.get('custom_description')
        if cd is not None and cd.strip() and (grp.custom_description or '') != cd:
            grp.custom_description = cd
            changed = True

        if changed:
            grp.save()
            report['updated'] += 1

    report['message'] = (
        f"{report['updated']} group(s) updated out of {report['total_rows']} "
        f"row(s). {len(report['skipped_no_match'])} unmatched group_id, "
        f"{len(report['unmatched_values'])} unknown name(s) skipped, "
        f"{len(report['invalid_status'])} invalid status, "
        f"{len(report['ambiguous'])} ambiguous name(s)."
    )
    return Response(report)
