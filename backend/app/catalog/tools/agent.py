"""
Pydantic-AI agent factory for the Data Governance chatbot.

Context-first, tool-light design. For each enabled integration the factory
asks its provider module (``catalog/tools/assistant/``) for two things and
wires them in uniformly:

- ``build_context(...)`` — a front-loaded markdown dump of that
  integration's catalog (PowerBI measures+reports, dbt models+columns,
  BigQuery schema for the in-scope datasets), appended to the system
  prompt and cached per org. The agent reads it instead of searching.
- ``build_tools(...)`` — that integration's small tool set: a single
  schema/connection tool (+ run-DAX / run-SQL where applicable).

Name resolution is done by READING the front-loaded catalog listing — there is
no resolve tool. The model takes exact item names straight from the in-prompt
inventory, then each integration's item profiler
(``get_pb_item_details`` / ``get_dbt_item_details``) is the workhorse — one call
returns an item's definition, ownership, usage stats and full "Uses" / "Used by"
lineage. The one-hop ``get_lineage`` helper is the only shared tool: kept
deliberately hard to over-reach for (exact name / id only, rate-limited via
``_MAX_CALLS_OVERRIDE``), so the model uses it only for a rare direct-neighbour
check. There are NO open-ended catalog *search* tools — the catalog is already
in the prompt, which keeps the agent from looping on search→refine→search calls.
"""
import time
from typing import Callable, Optional

from pydantic_ai import Agent

from .assistant import PROVIDERS
from .lineage import get_lineage
from .prompts import (
    SYSTEM_PROMPT_BASE,
    SYSTEM_PROMPT_BIGQUERY_ADDENDUM,
    SYSTEM_PROMPT_BIGQUERY_CATALOG_ADDENDUM,
    SYSTEM_PROMPT_DBT_ADDENDUM,
    SYSTEM_PROMPT_POWERBI_ADDENDUM,
    SYSTEM_PROMPT_POWERBI_LIVE_DISABLED_NOTE,
    build_date_context_block,
    build_format_reminder_block,
)
from .safe_wrapper import make_safe_tool


DEFAULT_CHATBOT_MODEL = 'google:gemini-3.5-flash'

# Newer Anthropic models (Opus 4.7+, Fable, Mythos) no longer accept sampling
# parameters — sending `temperature` (even 0.0) returns a 400. Older Anthropic
# models, Gemini, and Claude Haiku/Sonnet still accept it, so we keep pinning
# temperature=0 there for deterministic answers and just omit it where it would
# break the request.
_NO_SAMPLING_PARAM_MARKERS = (
    'claude-opus-4-7',
    'claude-opus-4-8',
    'claude-fable',
    'claude-mythos',
)

# Per-provider system-prompt addendum (the tool-light instructions).
_PROVIDER_ADDENDA = {
    'powerbi': SYSTEM_PROMPT_POWERBI_ADDENDUM,
    'dbt': SYSTEM_PROMPT_DBT_ADDENDUM,
    'bigquery': SYSTEM_PROMPT_BIGQUERY_ADDENDUM,
}

# Live query *clients* (not just an enabled provider) pull in the date-context
# block and the live-result output-format block. The PowerBI catalog provider
# can be active for profiling/usage WITHOUT a live client, and that catalog-only
# mode must NOT trigger the live-result formatting rules.


def build_model_settings(model: Optional[str]) -> dict:
    """Return pydantic-ai ``model_settings`` appropriate for ``model``.

    Pins ``temperature=0`` for deterministic output, except on the newer
    Anthropic models that reject sampling parameters — for those it returns an
    empty dict so the request doesn't 400.
    """
    identifier = (model or DEFAULT_CHATBOT_MODEL).lower()
    if any(marker in identifier for marker in _NO_SAMPLING_PARAM_MARKERS):
        return {}
    return {'temperature': 0.0}


def _workspace_scope_block(workspace_scope: list) -> str:
    """Authoritative workspace-scope instructions for PowerBI tool calls —
    tells the agent which workspace to assume when the user hasn't named one.
    Resolved upstream (per-user default → org default)."""
    lines = ['\n\n## Workspace scope (authoritative)\n']
    for entry in workspace_scope:
        sname = entry.get('source_name') or 'PowerBI'
        wid = entry.get('workspace_id')
        wname = entry.get('workspace_name')
        count = entry.get('workspace_count', 0)
        if wid and count > 1:
            lines.append(
                f'- Source `{sname}` has {count} workspaces. Default for '
                f'this user: **{wname}** (id=`{wid}`). Pass '
                f'`workspace_id="{wid}"` to every PowerBI tool call. When '
                f'you present a result, mention the workspace by name '
                f'once (e.g. "Workspace: **{wname}**") so the user knows '
                f'which one was used.'
            )
        elif wid and count == 1:
            lines.append(
                f'- Source `{sname}` has a single workspace **{wname}** '
                f'(id=`{wid}`). Pass `workspace_id="{wid}"` to PowerBI '
                f'tools. No need to mention it unless asked.'
            )
        else:
            lines.append(
                f'- Source `{sname}` has {count} workspaces and no '
                f'default is set. BEFORE calling any PowerBI tool that '
                f'filters by workspace, ASK the user which workspace '
                f'they mean — list the available workspace names and '
                f'wait for a reply.'
            )
    lines.append('')
    lines.append(
        '**Cross-workspace queries:** Only call tools without a '
        '`workspace_id` (i.e. across all workspaces) when the user '
        'EXPLICITLY asks to compare across workspaces or says "all '
        'workspaces". Otherwise stay within the workspace specified above.'
    )
    return '\n'.join(lines) + '\n'


def get_agent(
    powerbi_client=None,
    bigquery_client=None,
    dbt_enabled: bool = False,
    before_tool_call: Optional[Callable[[str], None]] = None,
    model: Optional[str] = None,
    workspace_scope: Optional[list] = None,
    powerbi_tools_enabled: bool = False,
    bigquery_live_enabled: bool = False,
    record_call: Optional[Callable[[dict], None]] = None,
    surface: str = 'slack',
    org=None,
    user=None,
    chat_session=None,
    pb_workspace_ids: Optional[list] = None,
    bq_dataset_ids: Optional[list] = None,
) -> Agent:
    """
    Build and return the Pydantic AI agent.

    Parameters
    ----------
    powerbi_client : client | None
        The PowerBI *live* REST client. Present only when the live tier
        (``powerbi_live_tools_enabled``) is on; it adds the live
        ``powerbi_run_dax_query`` tool. The PowerBI catalog (listing + the two
        DB-only profiler/usage tools) is gated by ``powerbi_tools_enabled`` and
        needs no client.
    bigquery_client : client | None
        The BigQuery client. Present when EITHER BigQuery tier is on — the
        catalog tier needs it to fetch schema, the live tier to run SQL. The
        live SQL tool (``bigquery_execute_query``) is registered only when
        ``bigquery_live_enabled`` is True; otherwise the schema is front-loaded
        read-only with no execution tool.
    powerbi_tools_enabled : bool
        Activate the PowerBI catalog provider (listing + DB-only profiler/usage
        tools) without requiring a live client. Defaults to False here;
        ``build_chatbot_agent_for_org`` passes the org flag (default True).
    bigquery_live_enabled : bool
        Register the live ``bigquery_execute_query`` tool (and use the live
        SQL prompt addendum). When False, BigQuery is catalog/schema only.
    dbt_enabled : bool
        When True, the dbt provider is activated (local catalog — no client;
        no live tier).
    before_tool_call / record_call : callable | None
        Hooks threaded into ``make_safe_tool`` for status messages and
        debug-meta capture.
    workspace_scope : list | None
        Per-user/org PowerBI workspace default(s) for the "assume this
        workspace" prompt block (resolved upstream).
    pb_workspace_ids / bq_dataset_ids : list | None
        Org-configured scope for the front-loaded context: which PowerBI
        workspaces / BigQuery datasets to include. ``None`` / empty means
        "all" for PowerBI and "none" for BigQuery (BigQuery context requires
        an explicit dataset selection).
    org, user, surface, chat_session : optional
        Threaded through for context building / back-compat with callers.
    """
    # (key, context_client, tools_client, scope_ids) per enabled integration.
    # The two clients are split so a provider can front-load its catalog WITHOUT
    # exposing live execution:
    #   - PowerBI: catalog (profilers) needs no client; live DAX uses powerbi_client.
    #     So context_client is None and tools_client is powerbi_client (None unless
    #     the live tier is on → build_tools then omits powerbi_run_dax_query).
    #   - BigQuery: schema context needs the client; the SQL tool is registered
    #     only when the live tier is on (tools_client set only then).
    # The ``powerbi_client is not None`` clause keeps older callers that pass
    # only a client working.
    active = []
    if powerbi_tools_enabled or powerbi_client is not None:
        active.append(('powerbi', None, powerbi_client, pb_workspace_ids))
    if dbt_enabled:
        active.append(('dbt', None, None, None))
    if bigquery_client is not None:
        active.append(('bigquery', bigquery_client,
                       bigquery_client if bigquery_live_enabled else None,
                       bq_dataset_ids))

    # Live-result formatting (date context + output rules) depends on an actual
    # live *execution* tool, not just a front-loaded catalog: PowerBI live DAX
    # (a live client) or BigQuery live SQL. Catalog-only modes have no live tool.
    has_live = (powerbi_client is not None
                or (bigquery_client is not None and bigquery_live_enabled))

    system_prompt = SYSTEM_PROMPT_BASE
    if has_live:
        system_prompt += build_date_context_block()

    # Shared cross-integration tool. Name resolution is now done by READING the
    # front-loaded catalog listing (no resolve tool): the model takes exact names
    # straight from the in-prompt inventory. get_lineage stays as a rare one-hop
    # neighbour check (exact-match-only, rate-limited via _MAX_CALLS_OVERRIDE).
    # Each integration also registers its own item profiler in build_tools
    # (PowerBI -> get_pb_item_details + get_pb_usage_analytics,
    # dbt -> get_dbt_item_details), which already returns an item's full "Uses" /
    # "Used by" lineage and usage rollups.
    tools = [get_lineage]

    for key, context_client, tools_client, scope_ids in active:
        provider = PROVIDERS[key]
        try:
            context = provider.build_context(org, client=context_client, scope_ids=scope_ids)
        except Exception:
            context = ''
        if context:
            system_prompt += context
        # Pick the addendum that matches what's actually callable: a catalog-only
        # tier gets a "live disabled" variant so the prompt never tells the model
        # to run a tool that isn't registered.
        if key == 'bigquery':
            system_prompt += (SYSTEM_PROMPT_BIGQUERY_ADDENDUM if bigquery_live_enabled
                              else SYSTEM_PROMPT_BIGQUERY_CATALOG_ADDENDUM)
        elif key == 'powerbi':
            system_prompt += SYSTEM_PROMPT_POWERBI_ADDENDUM
            if powerbi_client is None:
                system_prompt += SYSTEM_PROMPT_POWERBI_LIVE_DISABLED_NOTE
        else:
            system_prompt += _PROVIDER_ADDENDA[key]
        tools.extend(provider.build_tools(org, client=tools_client))

    if workspace_scope:
        system_prompt += _workspace_scope_block(workspace_scope)

    if has_live:
        system_prompt += build_format_reminder_block()

    agent = Agent(
        model=model or DEFAULT_CHATBOT_MODEL,
        system_prompt=system_prompt,
        retries=2,
        model_settings=build_model_settings(model),
    )

    # Deterministic loop-breaker: prompt rules ("ask, don't re-call") don't bind
    # weaker models, which re-call the same profiler with name variations until
    # they hit the hard budget (or the wall-clock timeout). Cap repeats PER TOOL
    # per turn — once exceeded the wrapper skips the tool body and returns a
    # forceful directive instead, converting a runaway loop into one more answer.
    call_guard = _make_repeat_guard()

    for tool_func in tools:
        agent.tool_plain(make_safe_tool(
            tool_func, before_call=before_tool_call, record_call=record_call,
            guard=call_guard,
        ))

    return agent


# Max times any single tool may actually execute in one turn. A genuine
# multi-item question (compare A/B/C, two-period WoW) stays under this; the
# runaway loops we saw re-called one tool 8-25 times. Each profiler bundle
# (~3-5k tokens) accumulates in the conversation, so every extra call slows the
# NEXT model round-trip — capping repeats keeps round-trips fast enough that the
# run finishes (or finalises) before the hard timeout.
_MAX_CALLS_PER_TOOL = 5

# Per-tool caps tighter than the global one, for tools that are easy to
# over-reach for. get_lineage is a rare one-hop fallback (the item profilers
# already cover lineage), so it gets a hard cap of 2 — reachable, but the model
# cannot lean on it call after call the way it did when it fuzzy-searched the
# catalog for "Ops" / "Ops Reports".
_MAX_CALLS_OVERRIDE = {'get_lineage': 2}

# Wall-clock point (seconds into the turn) past which the guard stops ALL further
# tool calls and tells the model to answer now. Set well below the default hard
# timeout (AGENT_TIMEOUT_SECONDS = 180) so a run burning time on slow model
# round-trips still gets a generous window to produce an answer before it is
# hard-killed — at 100s the model has ~80s of headroom to write its reply.
_SOFT_TIME_BUDGET_S = 100


def _make_repeat_guard() -> Callable[[str, tuple, dict], Optional[str]]:
    """Return a per-turn guard closure for ``make_safe_tool``. It short-circuits
    a tool call (returning a directive string instead of running the tool) when
    either (a) the turn has run past ``_SOFT_TIME_BUDGET_S`` wall-clock seconds,
    or (b) that tool has already executed its cap (``_MAX_CALLS_OVERRIDE`` for
    tools listed there, else ``_MAX_CALLS_PER_TOOL``) times. Both turn a
    dead-end loop into one final answer."""
    counts: dict = {}
    start = time.monotonic()

    def _guard(tool_name: str, _args: tuple, _kwargs: dict) -> Optional[str]:
        elapsed = time.monotonic() - start
        if elapsed > _SOFT_TIME_BUDGET_S:
            return (
                f"[time guard] This turn has already run for {int(elapsed)}s and "
                f"is near its time limit. Do NOT call any more tools. Give the "
                f"user your best answer right now from what you have already "
                f"gathered, or ask one short clarifying question."
            )
        counts[tool_name] = counts.get(tool_name, 0) + 1
        n = counts[tool_name]
        cap = _MAX_CALLS_OVERRIDE.get(tool_name, _MAX_CALLS_PER_TOOL)
        if n <= cap:
            return None
        return (
            f"[loop guard] You have already called `{tool_name}` {n - 1} times "
            f"this turn — that is the limit. Do NOT call it again. Using only "
            f"what you have already gathered, either give the user your best "
            f"answer now, or ask ONE specific clarifying question. If a name was "
            f"ambiguous, present the candidates you already found and ask which "
            f"one they mean. Calling more tools will not help."
        )

    return _guard
