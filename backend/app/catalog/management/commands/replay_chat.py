"""
Replay historical chat questions through the agent and capture per-run
diagnostics, to find *why* some queries loop / time out and to verify fixes.

READ-ONLY: this command never creates ChatSession/ChatMessage rows and never
mutates the org. It builds the agent exactly like the production worker
(``run_chat_event_sync``) but force-enables the per-org integration flags so
the PowerBI/BigQuery/dbt tools are registered (the live org flags may be off
even though history shows the tools were used when these questions ran).

Selection (default): the "worst offenders" — every triggering question whose
original assistant reply had a tool error OR >= ``--min-tool-calls`` tool calls,
deduped by question text (keeping the worst instance), capped at ``--max``.

Fidelity: "faithful session replay" — each question is replayed with its real
prior conversation context reconstructed from the stored messages, mirroring
``run_chat_event_sync``'s history formatting.

Per run we capture: wall-clock elapsed, model round-trips (= agent loop
iterations), full tool-call sequence + counts, token usage, tool errors, the
final answer (truncated), and whether it hit the hard timeout. Results stream
to ``<out>/replay_runs.jsonl`` and a human summary is written to
``<out>/replay_summary.md``.

Usage (run with the working tree mounted, prod DB for faithful context):

    python manage.py replay_chat --org-id 1 --model google:gemini-3.5-flash \
        --min-tool-calls 15 --max 50 --out /out
"""
from __future__ import annotations

import json
import time
import traceback

from django.core.management.base import BaseCommand

from catalog.models import Organization, ChatSession, ChatMessage
from catalog.views import (
    _get_chatbot_model_for_org,
    _get_dbt_enabled_for_org,
    retry_transient_llm_errors,
    _run_with_timeout,
    AGENT_TIMEOUT_SECONDS,
    deserialize_messages,
)
from catalog.services.debug_render import build_debug_payload

from pydantic_ai.messages import (
    ModelRequest, ModelResponse, UserPromptPart, TextPart,
)


# ---------------------------------------------------------------------------
# History reconstruction — identical logic to run_chat_event_sync()
# ---------------------------------------------------------------------------

def _format_history(msgs):
    formatted = []
    for msg in msgs:
        try:
            parsed = json.loads(msg.content)
            if isinstance(parsed, dict) and ('parts' in parsed or 'role' in parsed or 'kind' in parsed):
                formatted.extend(deserialize_messages([parsed]))
            else:
                raise ValueError
        except Exception:
            if msg.role == 'user':
                formatted.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
            else:
                formatted.append(ModelResponse(parts=[TextPart(content=msg.content)]))
    return formatted


def _is_synthetic(text: str) -> bool:
    """Disambiguation-flow payloads / trivial turns that won't reproduce
    faithfully standalone (live pb_live_query thread state is not replayed)."""
    t = (text or "").strip().lower()
    if not t:
        return True
    if t.startswith("selected:") or "pb_live_query_resume" in t:
        return True
    if t in {"1", "2", "3", "hey", "ok", "hi"}:
        return True
    return False


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def _select_targets(min_tool_calls: int, include_errors: bool, max_n: int,
                    skip_synthetic: bool):
    """Walk every session, find problematic assistant turns, map each back to
    its triggering user message + reconstructed prior context."""
    targets = []
    for sid in ChatSession.objects.values_list('id', flat=True):
        msgs = list(
            ChatMessage.objects.filter(session_id=sid).order_by('created_at')
        )
        for j, m in enumerate(msgs):
            if m.role == 'user':
                continue
            dm = m.debug_meta or {}
            stats = dm.get('stats') or {}
            if not stats:
                continue
            tc = stats.get('tool_call_count') or 0
            ec = stats.get('error_count') or 0
            dur = stats.get('total_duration_ms') or 0
            is_problem = (include_errors and ec > 0) or (tc >= min_tool_calls)
            if not is_problem:
                continue
            # nearest preceding user message = the trigger
            i = j - 1
            while i >= 0 and msgs[i].role != 'user':
                i -= 1
            if i < 0:
                continue
            question = msgs[i].content or ''
            if skip_synthetic and _is_synthetic(question):
                continue
            score = ec * 1_000_000_000 + tc * 1_000 + dur
            targets.append({
                'session_id': sid,
                'user_index': i,
                'question': question,
                'orig_tool_calls': tc,
                'orig_errors': ec,
                'orig_duration_ms': dur,
                'kind': 'error' if ec > 0 else 'many_tools',
                'score': score,
            })

    # dedupe by normalized question text, keep worst instance
    best = {}
    for t in targets:
        k = t['question'].strip().lower()[:300]
        if k not in best or t['score'] > best[k]['score']:
            best[k] = t
    ordered = sorted(best.values(), key=lambda d: -d['score'])
    return ordered[:max_n]


def _load_targets_from_file(path: str, max_n: int):
    """Build replay targets from a fixture JSON. Each question is replayed
    STANDALONE (session_id=None -> no prior context) so it reproduces
    regardless of current DB state."""
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    items = data.get('questions', data) if isinstance(data, dict) else data
    targets = []
    for item in (items or []):
        if isinstance(item, dict):
            q = item.get('question')
            orig = item.get('original') or {}
            kind = item.get('pattern') or 'fixture'
        else:
            q, orig, kind = str(item), {}, 'fixture'
        if not q:
            continue
        targets.append({
            'session_id': None,        # standalone replay
            'user_index': None,
            'question': q,
            'orig_tool_calls': orig.get('tool_calls', 0),
            'orig_errors': orig.get('errors', 0),
            'orig_duration_ms': orig.get('duration_ms', 0),
            'kind': kind,
            'score': 0,
        })
    return targets[:max_n]


# ---------------------------------------------------------------------------
# Agent build — faithful to build_chatbot_agent_for_org, but force-enabled
# ---------------------------------------------------------------------------

def _build_clients(org, stderr):
    """Build clients gated by the (possibly force-set) org flags, mirroring
    the production ``_get_*_client_for_org`` gating."""
    pbc = bqc = None
    if getattr(org, 'powerbi_live_tools_enabled', False):
        try:
            from catalog.powerbi_client import build_powerbi_client_for_org
            pbc = build_powerbi_client_for_org(org)
        except Exception as exc:
            stderr.write(f"  [warn] powerbi client: {exc.__class__.__name__}: {exc}")
    if (getattr(org, 'bigquery_tools_enabled', False)
            or getattr(org, 'bigquery_live_tools_enabled', False)):
        try:
            from catalog.bigquery_client import build_bigquery_client_for_org
            bqc = build_bigquery_client_for_org(org)
        except Exception as exc:
            stderr.write(f"  [warn] bigquery client: {exc.__class__.__name__}: {exc}")
    return pbc, bqc


def _scalar_usage(u):
    out = {}
    if u is None:
        return out
    for attr in ('requests', 'request_tokens', 'response_tokens', 'total_tokens',
                 'input_tokens', 'output_tokens'):
        try:
            v = getattr(u, attr)
        except Exception:
            continue
        if callable(v) or v is None:
            continue
        out[attr] = v
    return out


def _usage_dict(res):
    # pydantic-ai versions differ: usage may be a method or a property.
    u = None
    try:
        u = res.usage()
    except TypeError:
        try:
            u = res.usage
        except Exception:
            u = None
    except Exception:
        u = None
    out = _scalar_usage(u)
    if out:
        return out
    # Fallback: sum per-ModelResponse usage from the message stream.
    try:
        agg = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'requests': 0}
        found = False
        for m in res.all_messages():
            mu = getattr(m, 'usage', None)
            if mu is None:
                continue
            found = True
            agg['requests'] += 1
            for k_src in ('input_tokens', 'request_tokens'):
                agg['input_tokens'] += getattr(mu, k_src, 0) or 0
            for k_src in ('output_tokens', 'response_tokens'):
                agg['output_tokens'] += getattr(mu, k_src, 0) or 0
            agg['total_tokens'] += getattr(mu, 'total_tokens', 0) or 0
        return agg if found else out
    except Exception:
        return out


def _count_model_responses(res):
    # Count only THIS run's model responses (loops). all_messages() includes the
    # prior conversation history passed in, which would inflate the count for
    # follow-up turns, so prefer new_messages().
    try:
        msgs = res.new_messages() if hasattr(res, 'new_messages') else res.all_messages()
        return sum(1 for m in msgs if isinstance(m, ModelResponse))
    except Exception:
        return None


class Command(BaseCommand):
    help = "Replay historical chat questions through the agent and capture loop/timeout diagnostics (read-only)."

    def add_arguments(self, parser):
        parser.add_argument('--org-id', type=int, default=1)
        parser.add_argument('--model', type=str, default=None,
                            help="Override model identifier (default: org's configured model).")
        parser.add_argument('--min-tool-calls', type=int, default=15)
        parser.add_argument('--max', type=int, default=50)
        parser.add_argument('--no-errors', action='store_true',
                            help="Do NOT auto-include error turns (only tool-call threshold).")
        parser.add_argument('--include-synthetic', action='store_true',
                            help="Include disambiguation-flow / trivial turns (default: skip).")
        parser.add_argument('--force-bigquery', action='store_true',
                            help="Also force-enable BigQuery tools (default: powerbi only).")
        parser.add_argument('--force-dbt', action='store_true',
                            help="Also force-enable dbt tools (default: powerbi only).")
        parser.add_argument('--questions-file', type=str, default=None,
                            help="Replay questions from a fixture JSON (objects "
                                 "with a 'question' field under a 'questions' "
                                 "key, or a bare list) instead of mining the DB. "
                                 "Replayed STANDALONE (no prior conversation "
                                 "context) so they reproduce regardless of DB "
                                 "state. Use this for regression runs.")
        parser.add_argument('--out', type=str, default='/out')
        parser.add_argument('--timeout', type=int, default=None,
                            help="Per-run hard timeout seconds (default: org.chat_timeout_seconds or 180).")
        parser.add_argument('--dry-run', action='store_true',
                            help="Only select + print targets; do not run the agent.")

    def handle(self, *args, **opts):
        org = Organization.objects.filter(id=opts['org_id']).first()
        if not org:
            self.stderr.write(self.style.ERROR(f"Org id={opts['org_id']} not found."))
            return

        model = opts['model'] or _get_chatbot_model_for_org(org)
        if not model:
            self.stderr.write(self.style.ERROR("No model configured/overridden."))
            return

        timeout = opts['timeout'] or getattr(org, 'chat_timeout_seconds', None) or AGENT_TIMEOUT_SECONDS

        out_dir = opts['out'].rstrip('/')
        runs_path = f"{out_dir}/replay_runs.jsonl"
        targets_path = f"{out_dir}/replay_targets.json"
        summary_path = f"{out_dir}/replay_summary.md"

        self.stdout.write(self.style.SUCCESS(
            f"Replay: org={org.name!r} model={model} timeout={timeout}s "
            f"min_tool_calls={opts['min_tool_calls']} max={opts['max']}"
        ))

        if opts.get('questions_file'):
            targets = _load_targets_from_file(opts['questions_file'], opts['max'])
            self.stdout.write(f"Loaded {len(targets)} questions from "
                              f"{opts['questions_file']} (standalone replay)")
        else:
            targets = _select_targets(
                min_tool_calls=opts['min_tool_calls'],
                include_errors=not opts['no_errors'],
                max_n=opts['max'],
                skip_synthetic=not opts['include_synthetic'],
            )
        with open(targets_path, 'w', encoding='utf-8') as fh:
            json.dump(targets, fh, default=str, indent=2)
        self.stdout.write(f"Selected {len(targets)} target questions -> {targets_path}")

        if opts['dry_run']:
            for t in targets:
                self.stdout.write(
                    f"  [{t['kind']}] orig tools={t['orig_tool_calls']} "
                    f"err={t['orig_errors']} :: {t['question'][:90]!r}"
                )
            return

        # Force-enable integrations on the in-memory org (NOT saved) so tools
        # register. PowerBI is always forced on (history was PowerBI-driven);
        # BigQuery/dbt only when explicitly requested, to avoid adding tools
        # that were not present when these questions originally ran.
        org.powerbi_tools_enabled = True
        org.powerbi_live_tools_enabled = True
        org.bigquery_tools_enabled = bool(opts['force_bigquery'])
        org.bigquery_live_tools_enabled = bool(opts['force_bigquery'])
        org.dbt_tools_enabled = bool(opts['force_dbt'])
        pbc, bqc = _build_clients(org, self.stderr)
        dbt_enabled = _get_dbt_enabled_for_org(org)
        self.stdout.write(
            f"Clients: powerbi={'ok' if pbc else 'none'} "
            f"bigquery={'ok' if bqc else 'none'} dbt={dbt_enabled}"
        )

        workspace_scope = None
        if pbc is not None:
            try:
                from catalog.services.workspaces import resolve_default_workspaces_for_org
                workspace_scope = resolve_default_workspaces_for_org(None, org)
            except Exception as exc:
                self.stderr.write(f"  [warn] workspace scope: {exc}")

        from catalog.tools import get_agent

        runs = []
        with open(runs_path, 'w', encoding='utf-8') as runs_fh:
            for n, t in enumerate(targets, 1):
                rec = self._replay_one(
                    t, n, len(targets), org, model, timeout,
                    pbc, bqc, dbt_enabled, workspace_scope, get_agent,
                )
                runs.append(rec)
                runs_fh.write(json.dumps(rec, default=str) + "\n")
                runs_fh.flush()

        self._write_summary(summary_path, org, model, timeout, opts, runs)
        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {len(runs)} runs -> {runs_path}\nSummary -> {summary_path}"
        ))

    # ------------------------------------------------------------------
    def _replay_one(self, t, n, total, org, model, timeout,
                    pbc, bqc, dbt_enabled, workspace_scope, get_agent):
        session = ChatSession.objects.filter(id=t['session_id']).select_related('user').first()
        prior = []
        user_message = t['question']
        if session:
            msgs = list(ChatMessage.objects.filter(session_id=session.id).order_by('created_at'))
            ui = t['user_index']
            if 0 <= ui < len(msgs):
                user_message = msgs[ui].content
                prior = _format_history(msgs[:ui])

        debug_log: list = []
        seq: list = []

        def on_call(name, _a=(), _k=None):
            seq.append(name)

        agent = get_agent(
            powerbi_client=pbc,
            bigquery_client=bqc,
            dbt_enabled=dbt_enabled,
            powerbi_tools_enabled=getattr(org, 'powerbi_tools_enabled', True),
            bigquery_live_enabled=getattr(org, 'bigquery_live_tools_enabled', False),
            before_tool_call=on_call,
            record_call=debug_log.append,
            model=model,
            workspace_scope=workspace_scope,
            surface='web',
            org=org,
            user=session.user if session else None,
            chat_session=session,
            pb_workspace_ids=getattr(org, 'assistant_powerbi_workspace_ids', None) or None,
            bq_dataset_ids=getattr(org, 'assistant_bigquery_dataset_ids', None) or None,
        )

        from pydantic_ai import capture_run_messages
        from pydantic_ai.exceptions import UsageLimitExceeded
        from catalog.views import _agent_usage_limits, _finalize_partial_answer

        budget_hit = {'value': False}

        def _run():
            with capture_run_messages() as run_messages:
                try:
                    return retry_transient_llm_errors(
                        lambda: agent.run_sync(
                            user_message, message_history=prior,
                            usage_limits=_agent_usage_limits(),
                        )
                    )
                except UsageLimitExceeded:
                    budget_hit['value'] = True
                    return ('__BUDGET__', _finalize_partial_answer(model, run_messages))

        t0 = time.monotonic()
        timed_out = False
        error = None
        output = ''
        usage = {}
        model_responses = None
        try:
            res = _run_with_timeout(_run, timeout)
            if isinstance(res, tuple) and res and res[0] == '__BUDGET__':
                output = res[1] or ''
            elif res is not None:
                output = (res.output or '')
                usage = _usage_dict(res)
                model_responses = _count_model_responses(res)
        except TimeoutError as exc:
            timed_out = True
            error = f"timeout: {exc}"
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            traceback.print_exc()
        elapsed = time.monotonic() - t0

        payload = build_debug_payload(debug_log)
        stats = payload.get('stats', {})

        # repeated-tool signal: how concentrated is the sequence on one tool?
        from collections import Counter
        seq_counts = Counter(seq)
        top_tool, top_tool_n = (seq_counts.most_common(1)[0] if seq_counts else (None, 0))

        rec = {
            'n': n,
            'session_id': t['session_id'],
            'kind': t['kind'],
            'question': user_message[:400],
            'prior_turns': len(prior),
            'elapsed_s': round(elapsed, 1),
            'timed_out': timed_out,
            'budget_hit': budget_hit['value'],
            'error': error,
            'model_responses': model_responses,
            'tool_calls': stats.get('tool_call_count'),
            'tool_errors': stats.get('error_count'),
            'tool_duration_ms': stats.get('total_duration_ms'),
            'dax_queries': stats.get('dax_query_count'),
            'sql_queries': stats.get('sql_query_count'),
            'tool_sequence': seq,
            'top_tool': top_tool,
            'top_tool_count': top_tool_n,
            'usage': usage,
            'orig_tool_calls': t['orig_tool_calls'],
            'orig_errors': t['orig_errors'],
            'output_preview': output[:300],
        }

        flag = ('BUDGET' if budget_hit['value'] else
                ('TIMEOUT' if timed_out else ('ERR' if error else 'ok')))
        self.stdout.write(
            f"[{n}/{total}] {flag} {elapsed:5.1f}s "
            f"loops={model_responses} tools={rec['tool_calls']} "
            f"toolerr={rec['tool_errors']} (orig tools={t['orig_tool_calls']}) "
            f":: {user_message[:70]!r}"
        )
        self.stdout.flush()
        return rec

    # ------------------------------------------------------------------
    def _write_summary(self, path, org, model, timeout, opts, runs):
        def classify(r):
            if r.get('budget_hit'):
                return 'budget_capped'
            if r['timed_out']:
                return 'timeout'
            if r['error']:
                return 'error'
            tc = r['tool_calls'] or 0
            mr = r['model_responses'] or 0
            if tc >= opts['min_tool_calls'] or mr >= 12 or r['elapsed_s'] > 60:
                return 'problematic'
            if (r['tool_errors'] or 0) > 0:
                return 'problematic'
            return 'clean'

        for r in runs:
            r['category'] = classify(r)

        cats = {}
        for r in runs:
            cats[r['category']] = cats.get(r['category'], 0) + 1

        def avg(vals):
            vals = [v for v in vals if isinstance(v, (int, float))]
            return round(sum(vals) / len(vals), 1) if vals else None

        lines = []
        lines.append(f"# Chat replay diagnostics\n")
        lines.append(f"- org: **{org.name}** (id={org.id})")
        lines.append(f"- model: **{model}**")
        lines.append(f"- per-run hard timeout: {timeout}s")
        lines.append(f"- runs: **{len(runs)}**")
        lines.append("")
        lines.append("## Categories")
        for c in ('timeout', 'budget_capped', 'error', 'problematic', 'clean'):
            lines.append(f"- {c}: **{cats.get(c, 0)}**")
        lines.append("")
        lines.append("## Aggregate")
        lines.append(f"- avg loops (model round-trips): {avg([r['model_responses'] for r in runs])}")
        lines.append(f"- max loops: {max([r['model_responses'] or 0 for r in runs], default=0)}")
        lines.append(f"- avg tool calls: {avg([r['tool_calls'] for r in runs])}")
        lines.append(f"- max tool calls: {max([r['tool_calls'] or 0 for r in runs], default=0)}")
        lines.append(f"- avg elapsed: {avg([r['elapsed_s'] for r in runs])}s")
        total_tokens = avg([r['usage'].get('total_tokens') for r in runs if r.get('usage')])
        lines.append(f"- avg total tokens (when reported): {total_tokens}")
        lines.append("")

        worst = sorted(
            runs,
            key=lambda r: (r['timed_out'], r['error'] is not None,
                           r['model_responses'] or 0, r['tool_calls'] or 0),
            reverse=True,
        )[:25]
        lines.append("## Worst 25 runs (this replay)")
        lines.append("")
        lines.append("| # | cat | elapsed | loops | tools | toolerr | orig_tools | top tool (xN) | question |")
        lines.append("|---|-----|---------|-------|-------|---------|-----------|---------------|----------|")
        for r in worst:
            q = (r['question'] or '').replace('|', '\\|').replace('\n', ' ')[:70]
            lines.append(
                f"| {r['n']} | {r['category']} | {r['elapsed_s']}s | "
                f"{r['model_responses']} | {r['tool_calls']} | {r['tool_errors']} | "
                f"{r['orig_tool_calls']} | {r['top_tool']} (x{r['top_tool_count']}) | {q} |"
            )
        lines.append("")

        # Loop-pattern signal: most common repeated tool across runs
        from collections import Counter
        rep = Counter()
        for r in runs:
            if (r['top_tool_count'] or 0) >= 4:
                rep[r['top_tool']] += 1
        lines.append("## Tools most often repeated >= 4x within a single run")
        for tool, cnt in rep.most_common(10):
            lines.append(f"- `{tool}`: in {cnt} runs")
        lines.append("")

        with open(path, 'w', encoding='utf-8') as fh:
            fh.write("\n".join(lines))
