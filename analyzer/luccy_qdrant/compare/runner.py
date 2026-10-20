"""python -m luccy_qdrant.compare: ask luccy and DataGov luccy's golden set, judge every answer
with luccy's own judge, set it against luccy's own recorded score, and post the comparison to
DataGov.

The golden set is run exactly: every row of luccy's eval/gold/gold_set.jsonl (121), every kind,
warehouse included, and the rows whose expected documents DataGov does not hold too (they are
flagged per row, expected_docs_missing, so a failure can be explained). Only infrastructure
failures are left out of the scores. --ids, --kinds, --limit and --stratified make a smoke run:
its summary says complete: false. The reference is luccy's recorded run on the same set,
eval/gold/baseline.summary.json (golden.py): its score, its verdict per question, and luccy's
own manifest drift between its configuration and the one this image runs.

Environment:
  LUCCY_URL, LUCCY_API_KEY      luccy's /ask (arms A and A2)
  DATAGOV_URL                   DataGov's web origin
  LUCCY_EVAL_TOKEN              DataGov's eval answer and runs endpoints
  LUCCY_SYNC_TOKEN              DataGov's internal documents listing and digest (the corpus)
  SERVING_GEMINI_API_KEY        luccy's judge model (not needed with --dry-run)
  SERVING_JUDGE_MODEL           luccy's judge model name: it must be the recorded run's

Before anything is paid for, the free checks run: luccy's recorded run is read and the judge
must be its judge (same model, same votes per answer), then the corpus listing and its
digest, luccy's /health and a bearer probe, a readiness probe of the eval answer endpoint (an
unknown arm must be refused with 400, which proves the token) and of every DataGov arm asked
for (an empty question must be refused with 400; a 503 means the eval user or organization is
not set up), the judge's key and price, and a first post of the run (status "running") that
proves the eval token for the runs endpoint. Any of them failing exits 2 without a paid call.

Each question asks every arm one after another, in an order rotated per question from the
seed (a Latin square: over len(arms) questions every arm is asked in every position once), so
no arm always goes first; the order is kept in the posted rows. At most 2 questions run at
once: luccy answers two at a time, and arm C reaches it too.

The run keeps its state in memory and writes no file: results go to DataGov's runs endpoint
(every PROGRESS_EVERY questions and at the end) and a summary is printed to stdout. A dry run
posts nothing unless --post-dry-run (its params then say dry_run: true).
No paid call is ever retried; luccy's "busy" 503 (refused before it starts) is retried up to 3
times after its Retry-After. The run stops asking new questions (and finishes the ones in
flight) at --max-usd and on a first interrupt (SIGINT or SIGTERM); a second interrupt abandons
the questions in flight at once. It stops at once, skipping the arms the questions in flight
have not reached, when an arm answers 401/403/404/429 (a key refused, an endpoint gone, a spend
cap reached), when DataGov's eval endpoint answers 503 (not set up), and when one arm fails
MAX_CONSECUTIVE_ERRORS questions in a row: every later answer would be an error too.
Exit status: 0 done, 1 failed or stopped, 2 configuration (nothing was paid for).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import random
import re
import secrets
import signal
import sys
import threading
import traceback
from pathlib import Path

import httpx

from . import golden, payload
from .arms import ARMS, DEFAULT_ARMS, ORDER, datagov_leg, error_leg, fake_leg, luccy_leg
from .budget import Budget, cost_key
from .clients import PROBE_ARM, DataGovClient, Http, LuccyClient, error_text
from .ids import Corpus
from .judging import judge_model, make_judge, score_leg, unpriced_judge
from .metrics import make_outcome, summarize

KINDS = ("single_hop", "multi_hop", "derivation", "warehouse", "conflict", "stale", "abstain", "privacy")
SEED = 7
PROGRESS_EVERY = 10
DEFAULT_MAX_USD = 40.0
DEFAULT_TIMEOUT_S = 330.0          # luccy's server waits up to 300 s on an answer
MAX_CONCURRENCY = 2                # luccy answers two questions at once, and arm C calls it too
MAX_CONSECUTIVE_ERRORS = 5
POLL_S = 0.2
# DataGov's eval runs contract: this pattern and no "..".
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
EXIT_DONE, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2
LUCCY_ROOT = os.environ.get("LUCCY_ROOT", "/app")
ENV = {"luccy_url": "LUCCY_URL", "luccy_key": "LUCCY_API_KEY", "datagov_url": "DATAGOV_URL",
       "eval_token": "LUCCY_EVAL_TOKEN", "sync_token": "LUCCY_SYNC_TOKEN"}


# An arm answering one of these makes every later question an error while the other arms are
# still paid for, so no further question is asked: a refused key, an endpoint gone, a spend cap.
STOP_STATUSES = {401: "refused the key", 403: "refused the key", 404: "answered 404 (the endpoint is off or gone)",
                 429: "reached its spend cap"}
# DataGov's eval answer endpoint answers 503 when its eval user or organization is not set up.
EVAL_UNAVAILABLE = 503
_say_lock = threading.Lock()


class ConfigError(Exception):
    """Something to fix before a run can start; nothing was paid for."""


class Interrupts:
    """SIGINT and SIGTERM. The first: no new question is asked and the ones in flight finish.
    The second: the ones in flight are abandoned at once (handle raises KeyboardInterrupt)."""

    def __init__(self):
        self.count = 0
        self.cancel = threading.Event()

    def handle(self, signum=None, frame=None):
        self.count += 1
        if self.count >= 2:
            self.cancel.set()
            raise KeyboardInterrupt

    def raised(self) -> bool:
        """A KeyboardInterrupt reached the run (from handle, or from Python's own SIGINT handler
        where handle is not installed). True when the run must be abandoned."""
        if not self.cancel.is_set():
            self.count += 1
            if self.count >= 2:
                self.cancel.set()
        return self.cancel.is_set()


def say(out, text: str) -> None:
    with _say_lock:                                       # one whole line per question, never interleaved
        out.write(text + "\n")
        out.flush()


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- arguments
def comma_list(choices):
    def parse(value: str) -> list[str]:
        items = list(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))
        unknown = [x for x in items if x not in choices]
        if unknown:
            raise argparse.ArgumentTypeError(f"unknown {unknown}; choose from {', '.join(choices)}")
        if not items:
            raise argparse.ArgumentTypeError("empty list")
        return items
    return parse


def id_list(value: str) -> list[str]:
    items = list(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))
    if not items:
        raise argparse.ArgumentTypeError("empty list")
    return items


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def valid_run_id(value: str) -> bool:
    return bool(RUN_ID_RE.fullmatch(value)) and ".." not in value


def new_run_id() -> str:
    """run-<UTC stamp>-<6 hex>, e.g. run-20261016T142233Z-3f9a1c."""
    return f"run-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def run_id_arg(value: str) -> str:
    if not valid_run_id(value):
        raise argparse.ArgumentTypeError("a letter or digit, then letters, digits, '_', '.', ':' or '-' "
                                         "(at most 128 characters, no '..')")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m luccy_qdrant.compare", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", type=comma_list(ORDER), default=list(DEFAULT_ARMS),
                    help=f"comma-separated, from {','.join(ORDER)} (default {','.join(DEFAULT_ARMS)})")
    # Smoke runs only: any of these asks part of the golden set, and the summary says complete: false.
    ap.add_argument("--ids", type=id_list, help="comma-separated gold ids (a smoke run)")
    ap.add_argument("--kinds", type=comma_list(KINDS), help="comma-separated gold kinds (a smoke run)")
    ap.add_argument("--limit", type=positive_int, help="N questions, spread across kinds (a smoke run)")
    ap.add_argument("--stratified", type=positive_int,
                    help=f"N questions per kind (seeded sample, seed {SEED}; a smoke run)")
    ap.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD, help="spend cap for the run")
    ap.add_argument("--concurrency", type=int, default=2, help=f"questions at once, 1-{MAX_CONCURRENCY}")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds per answer call")
    ap.add_argument("--dry-run", action="store_true",
                    help="no paid call: fake arms and luccy's dry-run judge; nothing is posted")
    ap.add_argument("--post-dry-run", action="store_true",
                    help="a --dry-run that also posts its results (params.dry_run is true)")
    ap.add_argument("--run-id", type=run_id_arg, help="default run-<UTC stamp>-<6 hex>")
    args = ap.parse_args(argv)
    args.arms = [a for a in ORDER if a in args.arms]
    args.dry_run = args.dry_run or args.post_dry_run
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        ap.error(f"--concurrency must be 1-{MAX_CONCURRENCY} (luccy answers two questions at once, "
                 "and arm C reaches it too)")
    if args.max_usd <= 0:
        ap.error("--max-usd must be above 0")
    if args.timeout <= 0:
        ap.error("--timeout must be above 0")
    return args


def settings_from(env, arms: list[str]) -> dict:
    values = {key: (env.get(name) or "").strip() for key, name in ENV.items()}
    need = ["datagov_url", "eval_token", "sync_token"]
    if any(ARMS[a].system == "luccy" for a in arms):
        need += ["luccy_url", "luccy_key"]
    missing = [ENV[k] for k in need if not values[k]]
    if missing:
        raise ConfigError("set " + ", ".join(missing))
    for key in ("datagov_url", "luccy_url"):
        if key in need and not re.match(r"^https?://[^/\s]+", values[key]):
            raise ConfigError(f"{ENV[key]} must be an http(s) URL")
    return values


# --------------------------------------------------------------------------- gold + selection
def ensure_luccy_path() -> None:
    try:
        import eval.gold.judge  # noqa: F401
    except ImportError:
        if LUCCY_ROOT not in sys.path:
            sys.path.append(LUCCY_ROOT)


def load_gold() -> list[dict]:
    """luccy's gold rows, in file order."""
    ensure_luccy_path()
    try:
        from eval.gold import judge
        return list(judge.load_gold(Path(judge.GOLD_PATH)).values())
    except (ImportError, OSError) as e:
        raise ConfigError(f"luccy's gold set and judge are not importable ({type(e).__name__}: {e}); run inside "
                          f"the analyzer image or set LUCCY_ROOT (now {LUCCY_ROOT})") from e


def load_recorded() -> dict:
    """luccy's recorded run on the gold set (eval/gold/baseline.summary.json, beside it)."""
    try:
        return golden.load_baseline()
    except ValueError as e:
        raise ConfigError(str(e)) from e


def choose(gold: list[dict], args) -> list[dict]:
    """The questions to ask: every gold row, unless a smoke run's --ids, --kinds, --stratified or
    --limit narrows them (in that order)."""
    rows = gold
    if args.ids:
        known = {r["id"] for r in gold}
        unknown = [i for i in args.ids if i not in known]
        if unknown:
            raise ConfigError(f"--ids names questions luccy's gold set does not have: {', '.join(unknown)}")
        rows = [r for r in rows if r["id"] in set(args.ids)]
    if args.kinds:
        rows = [r for r in rows if r["kind"] in set(args.kinds)]
    rows = select(rows, args.limit, args.stratified)
    if not rows:
        raise ConfigError("no gold question left after --ids, --kinds, --stratified and --limit")
    return rows


def select(rows: list[dict], limit=None, stratified=None, seed=SEED) -> list[dict]:
    """--stratified N per kind (seeded), then --limit N spread across kinds like luccy's gate."""
    if stratified:
        by_kind: dict[str, list[dict]] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        rng, picked = random.Random(seed), set()
        for kind in sorted(by_kind):
            group = list(by_kind[kind])
            rng.shuffle(group)
            picked.update(r["id"] for r in group[:stratified])
        rows = [r for r in rows if r["id"] in picked]
    if limit:
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        chosen, i = [], 0
        while len(chosen) < limit and any(len(v) > i for v in by_kind.values()):
            for kind in sorted(by_kind):
                if len(by_kind[kind]) > i and len(chosen) < limit:
                    chosen.append(by_kind[kind][i])
            i += 1
        ids = {r["id"] for r in chosen}
        rows = [r for r in rows if r["id"] in ids]
    return rows


def arm_order(arms: list[str], index: int, seed: int = SEED) -> list[str]:
    """The order the arms answer question `index` in: a seeded shuffle of the arms, rotated by
    the question's index (a Latin square: over len(arms) questions each arm takes every place
    once), so no arm always answers first or last."""
    base = list(arms)
    random.Random(seed).shuffle(base)
    shift = index % len(base) if base else 0
    return base[shift:] + base[:shift]


# --------------------------------------------------------------------------- the run
class Run:
    def __init__(self, args, run_id: str, rows: list[dict], reference: golden.Reference | None, corpus: Corpus,
                 judge, datagov: DataGovClient, luccy: LuccyClient | None, params: dict, out,
                 interrupts: Interrupts | None = None):
        self.args, self.run_id, self.rows, self.reference = args, run_id, rows, reference
        self.arms = list(args.arms)
        self.corpus, self.judge, self.datagov, self.luccy = corpus, judge, datagov, luccy
        self.params, self.out = params, out
        self.interrupts = interrupts or Interrupts()
        self.posting = not args.dry_run or getattr(args, "post_dry_run", False)
        self.budget = Budget(None if args.dry_run else args.max_usd)
        self.done: list[dict] = []                         # gold rows finished, oldest first
        self.outcomes: dict[str, dict[str, dict]] = {}
        self.orders: dict[str, list[str]] = {}
        self.streak: dict[str, int] = {}                   # consecutive errors per arm
        self.lock = threading.Lock()
        self.stopped: str | None = None                    # no new question is asked
        self.halted: str | None = None                     # ... and the ones in flight skip their other arms
        self.interrupt_noted = False

    @property
    def cancelled(self) -> bool:
        return self.interrupts.cancel.is_set()

    def pause(self, seconds: float) -> bool:
        """Wait before a free retry; True when the run was abandoned meanwhile."""
        return self.interrupts.cancel.wait(seconds)

    def halt(self, reason: str) -> None:
        with self.lock:
            if self.halted is not None:
                return
            self.halted = self.stopped = reason
        say(self.out, f"stopping: {reason}")

    # ------------------------------------------------------------------ one question
    def leg(self, code: str, row: dict) -> dict:
        arm = ARMS[code]
        if self.args.dry_run:
            return fake_leg(arm, row, self.corpus)
        if arm.system == "luccy":
            return luccy_leg(self.luccy, row, self.pause)
        return datagov_leg(self.datagov, arm, row, self.corpus)

    def check_fatal(self, code: str, leg: dict) -> None:
        """Stop the run on an answer that every later question would get too."""
        status = leg["detail"].get("http_status")
        if status in STOP_STATUSES:
            self.halt(f"{code} {STOP_STATUSES[status]} ({leg['error']}); no further question was asked")
        elif status == EVAL_UNAVAILABLE and ARMS[code].system == "datagov":
            self.halt(f"{code}: DataGov's eval endpoint is not set up ({leg['error']}); no further question "
                      "was asked")

    def count_error(self, code: str, outcome: dict) -> None:
        with self.lock:
            self.streak[code] = self.streak.get(code, 0) + 1 if outcome["verdict"] == "error" else 0
            streak = self.streak[code]
        if streak >= MAX_CONSECUTIVE_ERRORS:
            self.halt(f"{code} failed {streak} questions in a row (last: {outcome['error']}); "
                      "no further question was asked")

    def ask(self, row: dict, index: int, reserved: float) -> int | None:
        """Every arm on one question, one after another in this question's order (luccy answers
        two at a time, and arms A, A2 and C all reach it). Returns how many questions are done,
        or None when the question was abandoned or never started."""
        order = arm_order(self.arms, index)
        legs, costs = {}, []
        for code in order:
            if self.cancelled or self.halted:
                break
            try:
                leg = self.leg(code, row)
            except Exception as e:                         # noqa: BLE001 - a raise is a result, never retried
                traceback.print_exc(file=sys.stderr)
                leg = error_leg(f"{type(e).__name__}: {str(e)[:300]}", 0.0)
            if self.cancelled:
                break                                      # abandoned meanwhile: nothing more is judged
            if leg["usd"] is not None:
                answer_usd, source = float(leg["usd"]), leg.get("cost_source") or "estimate"
            else:
                answer_usd, source = self.budget.estimate_arm(code), "estimate"
            self.check_fatal(code, leg)
            score, judge_error, judge_usd = score_leg(self.judge, row, leg)
            costs.append((cost_key(code), answer_usd, source == "measured" and leg["status"] == "ok"))
            if score is not None:
                costs.append(("judge", judge_usd, not self.args.dry_run))
            legs[code] = make_outcome(row, leg, score, judge_error, answer_usd, judge_usd, source)
            self.count_error(code, legs[code])
        self.budget.settle(reserved, costs)
        if self.cancelled or not legs:
            return None
        with self.lock:
            self.outcomes[row["id"]] = legs
            self.orders[row["id"]] = [code for code in order if code in legs]
            self.done.append(row)
            n = len(self.done)
        marks = " ".join(f"{code}={legs[code]['verdict']}" + ("(infra)" if legs[code]["infra_error"] else "")
                         for code in self.arms if code in legs)
        recorded = self.recorded_pass(row["id"])
        if recorded is not None:
            marks += f" recorded={'pass' if recorded else 'fail'}"
        say(self.out, f"[{n}/{len(self.rows)}] {row['id']:7s} {marks}  ${self.budget.spent:.2f}  "
                      f"{' '.join(row['question'].split())[:60]}")
        return n

    # ------------------------------------------------------------------ reporting
    def snapshot(self) -> tuple[list[dict], dict, dict]:
        with self.lock:
            return list(self.done), dict(self.outcomes), dict(self.orders)

    def recorded_pass(self, row_id: str) -> bool | None:
        """luccy's recorded verdict on the question as it is now, or None."""
        outcome = self.reference.outcomes.get(row_id) if self.reference else None
        return None if outcome is None else outcome["passed"]

    def summary_of(self, done: list[dict], outcomes: dict) -> dict:
        """metrics' numbers plus the golden set's identity, luccy's recorded run (and its figures
        on the questions finished), the configuration drift and the expected documents DataGov
        does not hold. `complete`: every gold row was selected and finished."""
        ref = self.reference
        summary = summarize(self.arms, done, outcomes, self.budget.spent, ref.outcomes if ref else None)
        on_this_run = summary.pop("recorded")
        if ref is None:
            return summary
        summary.update(
            gold=ref.gold,
            complete=len(self.rows) == ref.gold["rows"] and len(done) == len(self.rows),
            luccy_recorded=dict(ref.recorded, on_this_run=on_this_run),
            config_drift=ref.drift,
            expected_docs_missing=[{"id": r["id"], "question": r["question"], "missing": ref.missing[r["id"]]}
                                   for r in self.rows if ref.missing.get(r["id"])])
        return summary

    def summary(self) -> dict:
        done, outcomes, _ = self.snapshot()
        return self.summary_of(done, outcomes)

    def body(self, status: str) -> bytes:
        done, outcomes, orders = self.snapshot()
        summary = self.summary_of(done, outcomes)
        params = dict(self.params, progress={"done": len(done), "total": len(self.rows)},
                      stopped_reason=self.stopped)
        if status != "running":
            params["finished_at"] = utcnow()
        missing = self.reference.missing if self.reference else {}
        rows = [payload.public_row(r, outcomes[r["id"]], self.arms, orders.get(r["id"]), missing.get(r["id"]),
                                   self.recorded_pass(r["id"])) for r in done]
        return payload.build(self.run_id, status, params, summary, rows)

    def post(self, status: str) -> tuple[bool, str]:
        if not self.posting:
            return True, "not posted (a dry run; --post-dry-run posts it)"
        code, body = self.datagov.post_run(self.body(status))
        if 200 <= code < 300:
            return True, f"HTTP {code}"
        return False, f"HTTP {code}: {error_text(body)}"

    # ------------------------------------------------------------------ the loop
    def start(self, results: queue.Queue, row: dict, index: int, reserved: float) -> None:
        """One question on its own daemon thread: abandoning it never waits for it."""
        def work():
            try:
                results.put((True, self.ask(row, index, reserved)))
            except BaseException as e:                     # noqa: BLE001 - re-raised by the loop
                results.put((False, e))
        threading.Thread(target=work, name=f"compare-{row['id']}", daemon=True).start()

    def note_interrupt(self) -> None:
        if self.interrupts.count and not self.interrupt_noted:
            self.interrupt_noted = True
            self.stopped = self.stopped or "interrupted"
            say(self.out, "interrupted: finishing the questions in flight (interrupt again to abandon them)")

    def loop(self) -> None:
        results: queue.Queue = queue.Queue()
        upcoming = iter(enumerate(self.rows))
        inflight = 0
        while True:
            try:
                self.note_interrupt()
                while inflight < self.args.concurrency and self.stopped is None:
                    item = next(upcoming, None)
                    if item is None:
                        break
                    reserved = self.budget.reserve(self.arms)
                    if reserved is None:
                        self.stopped = (f"spend cap: ${self.budget.spent:.2f} spent + ${self.budget.inflight:.2f} "
                                        f"in flight + ${self.budget.estimate(self.arms):.2f} for the next question "
                                        f"> --max-usd {self.args.max_usd:g}")
                        say(self.out, f"stopping: {self.stopped}")
                        break
                    self.start(results, item[1], item[0], reserved)
                    inflight += 1
                if not inflight:
                    return
                try:
                    ok, value = results.get(timeout=POLL_S)
                except queue.Empty:
                    continue
                inflight -= 1
                if not ok:
                    raise value
                if value and value % PROGRESS_EVERY == 0:
                    sent, why = self.post("running")
                    if not sent:
                        say(sys.stderr, f"warning: progress post failed ({why}); the run goes on")
            except KeyboardInterrupt:
                if self.interrupts.raised():
                    self.stopped = "interrupted"
                    say(self.out, f"interrupted again: abandoning {inflight} question(s) in flight")
                    return


# --------------------------------------------------------------------------- stdout summary
def pct(value) -> str:
    return "-" if value is None else f"{100 * value:.1f}%"


def secs(value) -> str:
    return "-" if value is None else f"{value:.1f}s"


def report_text(summary: dict, run_id: str, status: str) -> str:
    header = (f"{'arm':4s} {'label':34s} {'pass':>9s} {'rate':>7s} {'docs':>7s} {'facts':>7s} {'abstain':>8s} "
              f"{'leaks':>5s} {'err':>4s} {'infra':>5s} {'p50':>7s} {'p90':>7s} {'cost':>8s} {'source':>8s}")
    lines = [f"run {run_id}: {status}, {summary['questions']} questions, spend ${summary['spent_usd']:.2f}",
             header]
    for code, a in summary["arms"].items():
        lines.append(f"{code:4s} {a['label']:34s} {a['pass']:>4d}/{a['n']:<4d} {pct(a['pass_rate']):>7s} "
                     f"{pct(a['expected_doc_hit_rate']):>7s} {pct(a['fact_recall']):>7s} "
                     f"{a['correct_abstentions']:>3d}/{a['abstain_rows']:<4d} {a['privacy_leaks']:>5d} "
                     f"{a['errors']:>4d} {a['infra_errors']:>5d} {secs(a['p50_latency_s']):>7s} "
                     f"{secs(a['p90_latency_s']):>7s} {'$' + format(a['est_cost_usd'], '.2f'):>8s} "
                     f"{a['cost_source'] or '-':>8s}")
    for p in summary["agreement"]:
        lines.append(f"agreement {p['a']} vs {p['b']} over {p['n']} paired: both pass {p['both_pass']}, "
                     f"only {p['a']} {p['only_a']}, only {p['b']} {p['only_b']}, both fail {p['both_fail']} "
                     f"(McNemar p {p['mcnemar_p']:.3f})")
    floor = summary["noise_floor"]
    if floor is None:
        if summary["verdicts"]:
            lines.append("noise floor: not measured (A2 did not run); a difference within ±5 points is called the same")
    else:
        lines.append(f"noise floor: luccy flipped {floor['flips']} questions against itself "
                     f"({pct(floor['disagree_rate'])}); a difference within {floor['threshold']} passes is normal")
    for v in summary["verdicts"]:
        warn = "  [disagrees with Luccy more than Luccy with itself]" if v["disagrees_more_than_luccy_itself"] else ""
        lines.append(f"{v['arm']} vs {v['vs']} over {v['n']} paired: {v['delta']:+d} -> {v['outcome']}: "
                     f"{v['text']}{warn}")
    gold = summary.get("gold")
    if gold:
        same = ("the same set luccy's recorded score used" if gold["same_as_recorded"]
                else f"NOT the set luccy's recorded score used (that one: {gold['baseline_digest']})")
        lines.append(f"golden set: {gold['selected']} of {gold['rows']} questions, digest {gold['digest']}, {same}"
                     + ("" if summary.get("complete") else "; PARTIAL: not the full golden set"))
    rec = summary.get("luccy_recorded")
    if rec:
        on = rec.get("on_this_run")
        lines.append(f"luccy recorded {rec['ts']}: {rec['n_pass']}/{rec['n']} pass ({pct(rec['pass_rate'])}), "
                     f"fact ratio {rec['fact_ratio']}, judge {rec['judge_model']} x{rec['judge_votes']}"
                     + (f"; on the {on['n']} questions finished: {on['pass']} pass" if on else ""))
    drift = summary.get("config_drift")
    if drift:
        lines.append("configuration: the same as luccy's recorded run" if drift["same"]
                     else "configuration changed since luccy's recorded run:")
        lines.extend(f"  {d['text']}" for d in drift["differences"])
        lines.append(f"  expected: {drift['documents']['text']}")
    missing = summary.get("expected_docs_missing") or []
    lines.append(f"expected documents not in DataGov (asked and scored anyway): {len(missing)} questions"
                 + (f" ({', '.join(m['id'] for m in missing)})" if missing else ""))
    infra = summary["excluded"]["infra_error"]
    lines.append(f"left out of the scores: {len(infra)} infrastructure failures")
    for e in infra[:20]:
        lines.append(f"  infra {e['id']} {e['arm']}: {e['message']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- free checks
def preflight_luccy(luccy: LuccyClient) -> dict:
    status, body = luccy.health()
    body = body if isinstance(body, dict) else {"error": body}
    if status != 200:
        raise ConfigError(f"luccy is not healthy at {luccy.base}: HTTP {status} "
                          f"{body.get('reasons') or body.get('error') or ''}".rstrip())
    ok, _, why = luccy.auth_ok()
    if not ok:
        raise ConfigError(f"luccy refused LUCCY_API_KEY ({why})")
    index = body.get("index") if isinstance(body.get("index"), dict) else {}
    return {"version": body.get("version"), "git": body.get("git"), "index_sig": index.get("sig"),
            "corpus_pages": body.get("corpus_pages")}


def preflight_digest(datagov: DataGovClient) -> dict:
    """DataGov's corpus digest (free): proves the sync token and that the backend serves the export."""
    status, body = datagov.digest()
    if status != 200 or not isinstance(body, dict):
        raise ConfigError(f"DataGov's corpus digest failed (HTTP {status}: {error_text(body)}); check DATAGOV_URL "
                          "and LUCCY_SYNC_TOKEN")
    return {"count": body.get("count"), "digest": body.get("digest")}


def preflight_eval(datagov: DataGovClient, arms: list[str]) -> dict:
    """Free readiness probes of the eval answer endpoint, for the DataGov arms asked for. Returns
    {arm: "ready"}; a ConfigError names what to set up."""
    codes = [a for a in arms if ARMS[a].system == "datagov"]
    if not codes:
        return {}
    status, body = datagov.probe(PROBE_ARM)
    if status in (401, 403):
        raise ConfigError(f"DataGov refused LUCCY_EVAL_TOKEN at the eval answer endpoint (HTTP {status})")
    if status == 404:
        raise ConfigError("DataGov's eval endpoints are off (HTTP 404): set LUCCY_EVAL_ENABLED and LUCCY_EVAL_TOKEN")
    if status == EVAL_UNAVAILABLE:
        raise ConfigError(f"DataGov's eval endpoint is not set up (HTTP 503: {error_text(body)})")
    if status != 400:
        raise ConfigError(f"DataGov's eval answer endpoint did not refuse an unknown arm (HTTP {status}: "
                          f"{error_text(body)})")
    unknown_arm = error_text(body)
    ready = {}
    for code in codes:
        name = ARMS[code].eval_arm
        status, body = datagov.probe(name)
        if status == EVAL_UNAVAILABLE:
            raise ConfigError(f"arm {code} ({name}) is not set up on DataGov (HTTP 503: {error_text(body)})")
        if status == 400 and error_text(body) == unknown_arm:
            raise ConfigError(f"DataGov does not know arm {code} ({name}): {unknown_arm}")
        if status not in (200, 400):
            raise ConfigError(f"arm {code} ({name}) failed its readiness probe (HTTP {status}: {error_text(body)})")
        ready[code] = "ready"
    return ready


# --------------------------------------------------------------------------- main
def run(args, env, out, transport=None, judge=None, interrupts: Interrupts | None = None) -> int:
    interrupts = interrupts or Interrupts()
    settings = settings_from(env, args.arms)
    gold = load_gold()
    baseline = load_recorded()
    rows = choose(gold, args)
    made_judge = judge is None
    if made_judge:
        judge = make_judge(args.dry_run)
    problem = golden.judge_problem(judge, baseline)    # free, before any call: other scores are not comparable
    if problem:
        raise ConfigError(problem)
    run_id = args.run_id or new_run_id()
    # trust_env=False: never through HTTP(S)_PROXY. DataGov's internal endpoints answer 404 to a
    # proxied request (X-Forwarded-For, X-Real-IP, Forwarded); both services are reached directly.
    with httpx.Client(transport=transport, timeout=args.timeout, trust_env=False) as client:
        http = Http(client)
        datagov = DataGovClient(http, settings["datagov_url"], settings["eval_token"], settings["sync_token"],
                                args.timeout)
        # ---- free checks, before anything is paid for
        status, documents = datagov.documents()
        if status != 200:
            raise ConfigError(f"DataGov's documents listing failed ({documents}); check DATAGOV_URL and "
                              "LUCCY_SYNC_TOKEN")
        corpus = Corpus(documents)
        if not len(corpus):
            raise ConfigError("DataGov's documents listing is empty: nothing in scope to compare on")
        corpus_digest = preflight_digest(datagov)
        luccy, luccy_info = None, None
        if any(ARMS[a].system == "luccy" for a in args.arms):
            luccy = LuccyClient(http, settings["luccy_url"], settings["luccy_key"], args.timeout)
            luccy_info = preflight_luccy(luccy)
        readiness = {} if args.dry_run else preflight_eval(datagov, args.arms)
        if made_judge and not args.dry_run:
            from serving import config
            if not config.GEMINI_API_KEY:
                raise ConfigError("SERVING_GEMINI_API_KEY is not set: luccy's judge needs it (or use --dry-run)")
            unpriced = unpriced_judge(judge)
            if unpriced:
                raise ConfigError(f"the judge model {unpriced} has no price in serving/pricing.py, "
                                  "so --max-usd cannot be enforced")
        searched = {"documents": len(corpus), "digest": corpus_digest.get("digest"),
                    "index_sig": (luccy_info or {}).get("index_sig")}    # what this run searches, for the drift
        try:
            reference = golden.reference(gold, rows, baseline, judge, corpus, searched)
        except Exception as e:                             # noqa: BLE001 - free, so a configuration error
            raise ConfigError(f"luccy's recorded run could not be set against this one ({type(e).__name__}: "
                              f"{str(e)[:300]})") from e
        params = {"arms": args.arms, "labels": {a: ARMS[a].label for a in args.arms},
                  "ids": args.ids, "kinds": args.kinds or list(KINDS), "limit": args.limit,
                  "stratified": args.stratified, "seed": SEED,
                  "arm_order": "rotated per question (seeded Latin square)", "max_usd": args.max_usd,
                  "concurrency": args.concurrency, "dry_run": args.dry_run, "judge_model": judge_model(judge),
                  "gold_digest": reference.gold["digest"], "gold_rows": len(gold), "selected": len(rows),
                  "corpus_documents": len(corpus), "corpus_digest": corpus_digest, "corpus_ids": corpus.report,
                  "readiness": readiness, "luccy": luccy_info, "started_at": utcnow()}
        r = Run(args, run_id, rows, reference, corpus, judge, datagov, luccy, params, out, interrupts)
        if interrupts.count:
            raise KeyboardInterrupt                       # interrupted during the checks: nothing was asked
        ok, why = r.post("running")                       # proves LUCCY_EVAL_TOKEN before any paid call
        if not ok:
            raise ConfigError(f"DataGov's eval runs endpoint refused the run ({why}); check DATAGOV_URL and "
                              "LUCCY_EVAL_TOKEN")
        same = reference.gold["same_as_recorded"]
        recorded = reference.recorded
        say(out, f"run {run_id}: {len(rows)} of {len(gold)} gold questions x {len(args.arms)} arms "
                 f"({', '.join(args.arms)}), golden set {reference.gold['digest']} "
                 f"({'the same as' if same else 'NOT the same as'} luccy's recorded run: "
                 f"{recorded['n_pass']}/{recorded['n']}), corpus {len(corpus)} documents, "
                 f"{sum(1 for m in reference.missing.values() if m)} questions with expected documents not in it"
                 + ("" if len(rows) == len(gold) else " [PARTIAL: not the full golden set]")
                 + (" [DRY RUN]" if args.dry_run else "") + ("" if r.posting else " [not posted]"))
        # ---- the paid part
        try:
            r.loop()
        except KeyboardInterrupt:
            interrupts.cancel.set()                       # abandon whatever is in flight
            r.stopped = "interrupted"
        except Exception as e:                             # noqa: BLE001 - reported to DataGov, then exit 1
            traceback.print_exc(file=sys.stderr)
            interrupts.cancel.set()
            r.stopped = f"crashed: {type(e).__name__}: {str(e)[:300]}"
        status = "failed" if r.stopped else "done"
        ok, why = r.post(status)
        summary = r.summary()
        say(out, report_text(summary, run_id, status))
        say(out, "SUMMARY_JSON " + json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        if not ok:
            say(sys.stderr, f"error: the final results post failed ({why}); the summary above is all that is left")
            return EXIT_FAILED
        return EXIT_FAILED if r.stopped else EXIT_DONE


def main(argv=None, env=None, out=None, transport=None, judge=None) -> int:
    out = out or sys.stdout
    args = parse_args(argv)                               # argparse exits 2 on a bad flag
    interrupts = Interrupts()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):       # `docker stop` stops cleanly too
            previous[sig] = signal.signal(sig, interrupts.handle)
    try:
        return run(args, os.environ if env is None else env, out, transport, judge, interrupts)
    except ConfigError as e:
        say(sys.stderr, f"config error: {e}")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        say(sys.stderr, "interrupted")
        return EXIT_FAILED
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
