import { describe, expect, it } from "vitest";
import type { ComparisonRow, ComparisonSummary } from "@/lib/document-analyzer-api";
import {
  answerStyle,
  armLabel,
  armShortLabel,
  chanceText,
  ciText,
  configDrift,
  differs,
  disagreementSentence,
  disagreesTooMuch,
  docsMissing,
  excludedQuestions,
  filterArms,
  filterRows,
  findVerdict,
  flipsText,
  fmtArmCost,
  fmtCount,
  fmtDay,
  fmtOf,
  fmtPct,
  fmtPoints,
  fmtQuestionDelta,
  fmtQuestions,
  fmtSeconds,
  fmtSpent,
  fmtUsd,
  goldenSetLine,
  infraErrors,
  isDryRun,
  isRunActive,
  kindCell,
  kindLabel,
  NOISE_NOT_MEASURED,
  noiseSentence,
  orderArms,
  paramText,
  partialRunText,
  QUESTION_FILTERS,
  RECORDED,
  RECORDED_NOISE,
  recordedArm,
  recordedLabel,
  recordedScoreSentence,
  recordedSentence,
  rowDocsMissing,
  rowVerdict,
  runClass,
  runLabel,
  runStatusClass,
  runStatusLabel,
  STALE_RUN_LABEL,
  TONE_STYLES,
  verdictText,
  verdictTone,
  withRecorded,
} from "./comparison-format";

describe("number formatting", () => {
  it("shows rates, which are fractions, as percentages, with a dash for anything missing", () => {
    expect(fmtPct(0.725)).toBe("72.5%");
    expect(fmtPct(0.72)).toBe("72%");
    expect(fmtPct(1)).toBe("100%");
    expect(fmtPct(0)).toBe("0%");
    expect(fmtPct(0.33333)).toBe("33.3%");
    expect(fmtPct(1.5)).toBe("150%");                         // no "already a percentage" guessing
    for (const missing of [null, undefined, NaN, Infinity, "0.5", {}]) expect(fmtPct(missing)).toBe("—");
  });

  it("counts questions, and signs differences in questions with a real minus and no sign on zero", () => {
    expect(fmtQuestions(1)).toBe("1 question");
    expect(fmtQuestions(6)).toBe("6 questions");
    expect(fmtQuestions(-2)).toBe("2 questions");
    expect(fmtQuestions(null)).toBe("—");
    expect(fmtQuestionDelta(3)).toBe("+3 questions");
    expect(fmtQuestionDelta(-1)).toBe("−1 question");
    expect(fmtQuestionDelta(-2)).toBe("−2 questions");
    expect(fmtQuestionDelta(0)).toBe("0 questions");
    expect(fmtQuestionDelta(undefined)).toBe("—");
  });

  it("says durations as people do", () => {
    expect(fmtSeconds(0.84)).toBe("0.8 s");
    expect(fmtSeconds(9.96)).toBe("10 s");
    expect(fmtSeconds(12.4)).toBe("12 s");
    expect(fmtSeconds(60)).toBe("1 min");
    expect(fmtSeconds(125.4)).toBe("2 min 5 s");
    expect(fmtSeconds(-1)).toBe("—");
    expect(fmtSeconds(undefined)).toBe("—");
  });

  it("formats dollars, counts and parts of a whole", () => {
    expect(fmtUsd(0)).toBe("$0");
    expect(fmtUsd(0.0042)).toBe("$0.0042");
    expect(fmtUsd(0.42)).toBe("$0.42");
    expect(fmtUsd(1234.5)).toBe("$1,234.50");
    expect(fmtUsd(null)).toBe("—");
    expect(fmtCount(1200)).toBe("1,200");
    expect(fmtCount(undefined)).toBe("—");
    expect(fmtOf(4, 5)).toBe("4 of 5");
    expect(fmtOf(4, null)).toBe("—");
  });
});

const SUMMARY: ComparisonSummary = {
  arms: { A: { label: "Luccy" }, X9: { label: "Experimental" }, C: {}, A2: { label: "  " } },
  // delta and threshold are whole question counts, as is the noise floor.
  verdicts: [{ arm: "C", vs: "A", delta: 1, threshold: 6, within_noise: true, text: "Same as Luccy (within its own run-to-run variation)" }],
  noise_floor: { flips: 9, threshold: 6 },
};

describe("arms", () => {
  it("prefers the run's own label, then the known names, then the key", () => {
    expect(armLabel("A", SUMMARY)).toBe("Luccy");
    expect(armLabel("C", SUMMARY)).toBe("DataGov chat · Luccy only");
    expect(armLabel("A2", SUMMARY)).toBe("Luccy (2nd run)");
    expect(armLabel("X9", SUMMARY)).toBe("Experimental");
    expect(armLabel("Z", null)).toBe("Z");
    expect(armShortLabel("A2", SUMMARY)).toBe("Luccy 2nd");
    expect(armShortLabel("C", SUMMARY)).toBe("Chat");
    expect(armShortLabel("X9", SUMMARY)).toBe("Experimental");
  });

  it("orders known arms first and keeps unknown ones, from summary and rows", () => {
    const rows: ComparisonRow[] = [{ id: "q1", arms: { Z: {}, A2: {} } }];
    expect(orderArms(SUMMARY, rows)).toEqual(["A", "A2", "C", "X9", "Z"]);
    expect(orderArms(null, null)).toEqual([]);
  });
});

describe("verdicts", () => {
  it("picks the tone from the noise floor first, then from the question counts", () => {
    expect(verdictTone({ delta: -12, threshold: 6, within_noise: true })).toBe("same");
    expect(verdictTone({ delta: 7, threshold: 6, within_noise: false })).toBe("better");
    expect(verdictTone({ delta: -7, threshold: 6, within_noise: false })).toBe("worse");
    expect(verdictTone({ delta: -7, within_noise: false })).toBe("worse");
    expect(verdictTone({ delta: 6, threshold: 6 })).toBe("same");             // up to ±threshold is normal
    expect(verdictTone({ delta: -6, threshold: 6 })).toBe("same");
    expect(verdictTone({ delta: 7, threshold: 6 })).toBe("better");
    expect(verdictTone({ delta: 7 })).toBe("unknown");
    expect(verdictTone({ within_noise: false })).toBe("unknown");
    expect(verdictTone(null)).toBe("unknown");
    expect(TONE_STYLES.worse.text).toBe("text-err");
    expect(TONE_STYLES.same.badge).toBe("Same");
  });

  it("uses the run's sentence, or builds one in plain words", () => {
    const v = findVerdict(SUMMARY, "C", "A");
    expect(verdictText(v, SUMMARY)).toBe("Same as Luccy (within its own run-to-run variation)");
    expect(findVerdict(SUMMARY, "X9", "A")).toBeNull();
    expect(verdictText({ arm: "C", vs: "A", delta: -9, threshold: 6, within_noise: false }, SUMMARY)).toBe("Worse than Luccy by 9 questions");
    expect(verdictText({ arm: "C", vs: "A", delta: 1, within_noise: false, text: " " }, SUMMARY)).toBe("Better than Luccy by 1 question");
    expect(verdictText({ arm: "C", vs: "A", delta: 2, threshold: 6 }, SUMMARY)).toBe("Same as Luccy (within its own run-to-run variation)");
    expect(verdictText(null, SUMMARY)).toBe("Not enough answers to compare yet.");
    expect(verdictText({ arm: "C", vs: "A" }, SUMMARY)).toBe("Not enough answers to compare yet.");
  });

  it("explains the noise floor in one sentence, in questions, or says it was not measured", () => {
    expect(noiseSentence(SUMMARY)).toBe("Luccy answered 9 questions differently when asked twice; differences up to ±6 questions are normal.");
    expect(noiseSentence({ noise_floor: { flips: 1, threshold: 1 } })).toBe("Luccy answered 1 question differently when asked twice; differences up to ±1 question are normal.");
    expect(noiseSentence({ noise_floor: { flips: 0, threshold: 2 } })).toBe("Luccy answered 0 questions differently when asked twice; differences up to ±2 questions are normal.");
    expect(noiseSentence({ noise_floor: { flips: 4, threshold: 4, disagree_rate: 0.08 } })).toBe("Luccy answered 4 questions (8%) differently when asked twice; differences up to ±4 questions are normal.");
    expect(noiseSentence({ noise_floor: {} })).toBe("Luccy answered — questions differently when asked twice; differences up to ±— questions are normal.");
    // No noise floor: luccy was not run twice.
    expect(noiseSentence({ noise_floor: null })).toBe("Luccy's run-to-run variation was not measured in this run.");
    expect(noiseSentence({})).toBe(NOISE_NOT_MEASURED);
    expect(noiseSentence(null)).toBe(NOISE_NOT_MEASURED);
  });

  it("takes the tone from the runner's outcome, whatever the numbers say", () => {
    expect(verdictTone({ outcome: "same", delta: -12, threshold: 6, within_noise: false })).toBe("same");
    expect(verdictTone({ outcome: "worse", delta: 0, threshold: 6, within_noise: true })).toBe("worse");
    expect(verdictTone({ outcome: "better", delta: -3 })).toBe("better");
    expect(verdictTone({ outcome: "inconclusive", delta: 9, threshold: 2, within_noise: false })).toBe("inconclusive");
    expect(verdictTone({ outcome: " Worse " })).toBe("worse");
    expect(verdictTone({ outcome: "error", delta: 9, threshold: 2 })).toBe("unknown");      // an outcome the page doesn't know
    expect(TONE_STYLES.inconclusive.badge).toBe("Inconclusive");
    expect(verdictText({ arm: "C", vs: "A", outcome: "inconclusive" }, SUMMARY)).toBe("Not enough evidence to tell it apart from Luccy");
    expect(verdictText({ arm: "C", vs: "A", outcome: "inconclusive", text: "Can't tell yet: too few questions" }, SUMMARY)).toBe("Can't tell yet: too few questions");
  });

  it("says how often each side disagreed, with 'not measured' where the run has no number", () => {
    const v = { arm: "C", vs: "A", disagree_rate: 0.125, luccy_disagree_rate: 0.08 };
    expect(disagreementSentence(v, SUMMARY)).toBe("DataGov chat disagreed with Luccy on 12.5% of questions; Luccy disagreed with itself on 8%.");
    expect(disagreementSentence({ ...v, luccy_disagree_rate: null }, SUMMARY))
      .toBe("DataGov chat disagreed with Luccy on 12.5% of questions; how often Luccy disagrees with itself was not measured.");
    expect(disagreementSentence({ arm: "C", vs: "A", disagree_rate: null, luccy_disagree_rate: 0.1 }, SUMMARY))
      .toBe("How often DataGov chat disagreed with Luccy was not measured; Luccy disagreed with itself on 10%.");
    expect(disagreementSentence({ arm: "X9", vs: "A", disagree_rate: 0.3 }, SUMMARY)).toMatch(/^Experimental disagreed with Luccy on 30% of questions;/);
    expect(disagreesTooMuch({ disagrees_more_than_luccy_itself: true })).toBe(true);
    expect(disagreesTooMuch({ disagrees_more_than_luccy_itself: false })).toBe(false);
    expect(disagreesTooMuch({ disagrees_more_than_luccy_itself: null })).toBe(false);
    expect(disagreesTooMuch(null)).toBe(false);
  });

  it("shows the 95% confidence interval in percentage points", () => {
    expect(ciText({ ci_low: -0.12, ci_high: 0.04 })).toBe("95% CI −12 to +4 points");
    expect(ciText({ ci_low: 0.025, ci_high: 0.2 })).toBe("95% CI +2.5 to +20 points");
    expect(ciText({ ci_low: 0, ci_high: 0.0004 })).toBe("95% CI 0 to 0 points");
    expect(ciText({ ci_low: -0.1, ci_high: null })).toBe("");
    expect(ciText(null)).toBe("");
    expect(fmtPoints(-0.05)).toBe("−5");
    expect(fmtPoints(0.051)).toBe("+5.1");
    expect(fmtPoints(undefined)).toBe("—");
  });

  it("says whether a pair's difference could be chance", () => {
    expect(chanceText(0.48)).toBe("The difference could be chance (p = 0.48).");
    expect(chanceText(0.004)).toBe("The difference is unlikely to be chance (p = 0.004).");
    expect(chanceText(0.0001)).toBe("The difference is unlikely to be chance (p < 0.001).");
    expect(chanceText(null)).toBe("");
  });
});

describe("questions", () => {
  const rows: ComparisonRow[] = [
    { id: "q1", arms: { A: { verdict: "pass" }, A2: { verdict: "pass" }, C: { verdict: "fail" } } },
    { id: "q2", arms: { A: { verdict: "pass" }, A2: { verdict: "error" }, C: { verdict: "pass" } } },
    { id: "q3", arms: { A: { verdict: "abstain" }, A2: { verdict: "pass" } } },
    // Wrong against "didn't know" is wrong against wrong: both not right, as the agreement table counts it.
    { id: "q4", arms: { A: { verdict: "abstain" }, A2: { verdict: "error" }, C: { verdict: "fail" } } },
    { id: "q5", arms: { A: { verdict: "fail" }, A2: { verdict: "fail" }, C: { verdict: "pass" } } },
  ];

  it("filters to the questions where one arm was right and the other not", () => {
    expect(differs(rows[0], "C", "A")).toBe(true);
    expect(differs(rows[2], "C", "A")).toBe(false);          // C was never asked: not a difference
    expect(differs(rows[3], "C", "A")).toBe(false);          // fail vs abstain: neither right
    expect(differs(rows[3], "A2", "A")).toBe(false);         // error vs abstain: neither right
    expect(differs(rows[1], "A2", "A")).toBe(true);          // an error counts as not right
    expect(QUESTION_FILTERS.map((f) => f.id)).toEqual(["all", "c-vs-a", "a-vs-recorded"]);
    expect(filterRows(rows, "all").map((r) => r.id)).toEqual(["q1", "q2", "q3", "q4", "q5"]);
    expect(filterRows(rows, "c-vs-a").map((r) => r.id)).toEqual(["q1", "q5"]);
  });

  it("lists excluded questions with their text, old bare ids included, and infrastructure errors with arm and message", () => {
    const withRows: ComparisonRow[] = [{ id: "q9", question: "Bookings last week?" }];
    expect(excludedQuestions([{ id: "q7", question: " Revenue in May? " }, { id: "q8" }, "q9", { question: "no id" }, 3], withRows))
      .toEqual([{ id: "q7", question: "Revenue in May?" }, { id: "q8", question: "" }, { id: "q9", question: "Bookings last week?" }]);
    expect(excludedQuestions([], null)).toEqual([]);
    expect(excludedQuestions(undefined, null)).toBeNull();
    expect(infraErrors([{ id: "q1", arm: "C", message: "Timed out after 300 s" }, { id: "q2", arm: "A2" }, { arm: "A", message: "no id" }, "q3"]))
      .toEqual([{ id: "q1", arm: "C", message: "Timed out after 300 s" }, { id: "q2", arm: "A2", message: "—" }]);
    expect(infraErrors(null)).toBeNull();
  });

  it("styles each answer's verdict in plain words", () => {
    expect(answerStyle("pass")).toMatchObject({ label: "Right", className: expect.stringContaining("text-ok") });
    expect(answerStyle("fail")).toMatchObject({ label: "Wrong", className: expect.stringContaining("text-err") });
    expect(answerStyle("abstain").label).toBe("Didn't know");
    expect(answerStyle("error").label).toBe("Error");
    expect(answerStyle(undefined).label).toBe("—");
  });

  it("labels kinds and per-kind cells", () => {
    expect(kindLabel("multi_hop")).toBe("Multi hop");
    expect(kindLabel("")).toBe("—");
    expect(kindCell({ n: 10, pass: 7 })).toEqual({ main: "7 of 10", pct: "70%" });
    expect(kindCell({ n: 0, pass: 0 })).toEqual({ main: "0 of 0", pct: "—" });
    expect(kindCell(undefined)).toEqual({ main: "—", pct: "—" });
  });
});

// A full golden-set run's summary, as the runner posts it (analyzer/luccy_qdrant/compare).
const GOLDEN: ComparisonSummary = {
  questions: 121,
  complete: true,
  gold: { rows: 121, selected: 121, digest: "2670070ea44a", baseline_digest: "2670070ea44a", same_as_recorded: true, changed_ids: [] },
  luccy_recorded: {
    ts: "2026-09-28T19:58:41+00:00", n: 121, n_pass: 74, pass_rate: 0.6116, fact_ratio: 0.7059, judge_model: "gemini-3.5-flash-lite",
    judge_votes: 1, gold_digest: "2670070ea44a", pass_by_id: { "sh-001": true },
    on_this_run: { label: "Luccy, recorded", n: 121, pass: 74, pass_rate: 0.6116, fact_recall: 0.7059, p50_latency_s: 25.4 },
  },
  config_drift: {
    same: false,
    differences: [{ field: "prompts.route", recorded: "97c2", current: "4cb6", text: "The router prompt (route) changed since the recorded run." }, { field: "retrieval.RERANK_TOPK" }, {}],
    documents: { current: { documents: 88 }, text: "Documents: …" },
  },
  expected_docs_missing: [{ id: "sh-001", question: " Starbucks ride? ", missing: ["4435476587"] }],
};

describe("golden set and Luccy's recorded run", () => {
  it("dates and names the recorded run's column", () => {
    expect(fmtDay("2026-09-28T19:58:41+00:00")).toBe("28 Sep");
    expect(fmtDay("2026-09-28T23:30:00-05:00", true)).toBe("29 Sep 2026");           // UTC
    expect(fmtDay("not a date")).toBe("");
    expect(fmtDay(null)).toBe("");
    expect(recordedLabel(GOLDEN)).toBe("Luccy, recorded 28 Sep");
    expect(recordedLabel(null)).toBe("Luccy, recorded");
    expect(armLabel(RECORDED, GOLDEN)).toBe("Luccy, recorded 28 Sep");
    expect(armShortLabel(RECORDED, GOLDEN)).toBe("Recorded");
    expect(recordedArm(GOLDEN)).toMatchObject({ n: 121, pass: 74 });
    expect(withRecorded(["A", "A2", "C"], GOLDEN)).toEqual(["A", "A2", "C", RECORDED]);
    expect(withRecorded(["A", "C"], SUMMARY)).toEqual(["A", "C"]);                    // an older run: no column
  });

  it("says which golden set was asked, and warns when it is not the recorded one", () => {
    expect(goldenSetLine(GOLDEN)).toEqual({ same: true, text: "121 questions · the same set Luccy's recorded score used (digest 2670070ea44a)" });
    const changed = { ...GOLDEN, gold: { rows: 122, digest: "abc", baseline_digest: "2670070ea44a", same_as_recorded: false, changed_ids: ["mh-002", "zz-1"] } };
    expect(goldenSetLine(changed)).toEqual({
      same: false, text: "122 questions · not the set Luccy's recorded score used (digest abc, recorded 2670070ea44a); 2 questions changed since",
    });
    expect(goldenSetLine({ gold: { same_as_recorded: false } })?.text).toBe("— questions · not the set Luccy's recorded score used (digest —, recorded —)");
    expect(goldenSetLine(SUMMARY)).toBeNull();
    expect(goldenSetLine(null)).toBeNull();
  });

  it("calls a smoke run or a run stopped early partial, but not a full run or one still going", () => {
    const smoke = { ...GOLDEN, questions: 2, complete: false, gold: { ...GOLDEN.gold, selected: 2 } };
    expect(partialRunText({ status: "done", summary: smoke }))
      .toBe("This run asked 2 of the 121 golden questions (a smoke run). Its numbers can't be set against Luccy's recorded score on the full set.");
    expect(partialRunText({ status: "running", summary: smoke })).toMatch(/^This run asked 2 of the 121/);
    const stopped = { ...GOLDEN, questions: 37, complete: false };
    expect(partialRunText({ status: "failed", summary: stopped })).toMatch(/^This run stopped after 37 of the 121 golden questions\. /);
    expect(partialRunText({ status: "running", stale: true, summary: stopped })).toMatch(/^This run stopped after 37/);
    expect(partialRunText({ status: "running", summary: stopped })).toBeNull();        // still going
    expect(partialRunText({ status: "done", summary: GOLDEN })).toBeNull();
    expect(partialRunText({ status: "done", summary: SUMMARY })).toBeNull();           // an older run says nothing
    expect(partialRunText(null)).toBeNull();
  });

  it("states the recorded score, on the whole set and on this run's questions, with Luccy's own noise", () => {
    expect(recordedSentence(GOLDEN)).toBe("Luccy's recorded run (28 Sep 2026): 74 of 121 right (61.2%), facts covered 70.6%, judged by gemini-3.5-flash-lite.");
    expect(recordedSentence({ luccy_recorded: {} })).toBe("Luccy's recorded run: — right (—), facts covered —.");
    expect(recordedSentence(SUMMARY)).toBeNull();
    expect(recordedScoreSentence(GOLDEN)).toBe("Luccy's recorded score (28 Sep) on the same questions: 74 of 121 right (61.2%).");
    expect(recordedScoreSentence({ luccy_recorded: { on_this_run: { n: 0, pass: 0 } } })).toBe("");
    expect(recordedScoreSentence(SUMMARY)).toBe("");
    expect(RECORDED_NOISE).toContain("22–25");
    expect(flipsText({ b: RECORDED, only_a: 13, only_b: 11 })).toBe("24 questions flipped against the recorded run; identical Luccy runs flip 22–25.");
    expect(flipsText({ b: "A", only_a: 1, only_b: 2 })).toBe("");
    expect(flipsText({ b: RECORDED, only_a: null, only_b: 2 })).toBe("");
  });

  it("lists configuration drift in plain words, and the documents as an expected difference", () => {
    expect(configDrift(GOLDEN)).toEqual({
      same: false,
      lines: ["The router prompt (route) changed since the recorded run.", "retrieval.RERANK_TOPK changed since the recorded run."],
      documents: "Documents: Luccy's recorded run searched its own index; this run searches DataGov's copy (88 documents). This difference is expected: it is what the comparison measures.",
    });
    expect(configDrift({ config_drift: { same: true, differences: [] } })).toEqual({
      same: true, lines: [],
      documents: "Documents: Luccy's recorded run searched its own index; this run searches DataGov's copy. This difference is expected: it is what the comparison measures.",
    });
    expect(configDrift(SUMMARY)).toBeNull();
  });

  it("lists the questions whose expected documents DataGov does not hold", () => {
    expect(docsMissing(GOLDEN.expected_docs_missing)).toEqual([{ id: "sh-001", question: "Starbucks ride?", missing: ["4435476587"] }]);
    expect(docsMissing([{ id: "x", missing: ["1", 7, ""] }, { question: "no id" }, "y"])).toEqual([{ id: "x", question: "", missing: ["1"] }]);
    expect(docsMissing([])).toEqual([]);
    expect(docsMissing(undefined)).toBeNull();
    expect(rowDocsMissing({ id: "q", expected_docs_missing: ["1", "", 2 as unknown as string] })).toEqual(["1"]);
    expect(rowDocsMissing({ id: "q" })).toEqual([]);
  });

  it("filters to the questions where Luccy differs from its recorded verdict", () => {
    const rows: ComparisonRow[] = [
      { id: "q1", recorded: "pass", arms: { A: { verdict: "fail" }, C: { verdict: "pass" } } },
      { id: "q2", recorded: "fail", arms: { A: { verdict: "abstain" }, C: { verdict: "fail" } } },
      { id: "q3", recorded: null, arms: { A: { verdict: "pass" } } },               // no recorded verdict
      { id: "q4", recorded: "fail", arms: { A: { verdict: "pass" } } },
    ];
    expect(rowVerdict(rows[0], RECORDED)).toBe("pass");
    expect(rowVerdict(rows[2], RECORDED)).toBeUndefined();
    expect(rowVerdict(rows[2], "C")).toBeUndefined();
    expect(rowVerdict({ id: "q", arms: { A: {} } }, "A")).toBeNull();               // asked, no verdict: not right
    expect(differs(rows[1], "A", RECORDED)).toBe(false);                             // didn't know vs wrong: neither right
    expect(filterRows(rows, "a-vs-recorded").map((r) => r.id)).toEqual(["q1", "q4"]);
    expect(filterArms(["A", "C"], rows)).toEqual(["A", "C", RECORDED]);
    expect(filterArms(["A", "C"], [rows[2]])).toEqual(["A", "C"]);
  });
});

describe("runs", () => {
  it("names run states in plain words and knows which still change", () => {
    expect(runStatusLabel("done")).toBe("Finished");
    expect(runStatusLabel("RUNNING")).toBe("Running");
    expect(runStatusLabel("queued")).toBe("Waiting to start");
    expect(runStatusLabel("needs_review")).toBe("Needs review");
    expect(runStatusLabel(null)).toBe("—");
    expect(runStatusClass("failed")).toContain("text-err");
    expect(isRunActive({ status: "running" })).toBe(true);
    expect(isRunActive({ status: "done" })).toBe(false);
    expect(isRunActive(null)).toBe(false);
  });

  it("shows a stale run as stopped and stops asking for it", () => {
    expect(isRunActive({ status: "running", stale: true })).toBe(false);
    expect(isRunActive({ status: "running", stale: false })).toBe(true);
    expect(runLabel({ status: "running", stale: true })).toBe("Stopped — no progress for 30+ minutes");
    expect(runLabel({ status: "running", stale: true })).toBe(STALE_RUN_LABEL);
    expect(runLabel({ status: "running" })).toBe("Running");
    expect(runClass({ status: "running", stale: true })).toContain("text-warn");
    expect(runClass({ status: "done" })).toContain("text-ok");
  });

  it("knows a dry run only by params.dry_run === true", () => {
    expect(isDryRun({ params: { dry_run: true } })).toBe(true);
    expect(isDryRun({ params: { dry_run: false } })).toBe(false);
    expect(isDryRun({ params: { dry_run: "true" } })).toBe(false);
    expect(isDryRun({ params: null })).toBe(false);
  });

  it("marks costs estimated unless measured, and the spend unless every arm was measured", () => {
    expect(fmtArmCost({ est_cost_usd: 0.42, cost_source: "measured" })).toBe("$0.42");
    expect(fmtArmCost({ est_cost_usd: 0.42, cost_source: "estimate" })).toBe("$0.42 (estimated)");
    expect(fmtArmCost({ est_cost_usd: 0.42, cost_source: "mixed" })).toBe("$0.42 (estimated)");
    expect(fmtArmCost({ est_cost_usd: 0.42 })).toBe("$0.42 (estimated)");
    expect(fmtArmCost({ est_cost_usd: null, cost_source: "estimate" })).toBe("—");
    const measured = { A: { cost_source: "measured" }, C: { cost_source: "measured" } };
    expect(fmtSpent({ spent_usd: 1.5, arms: measured })).toBe("$1.50");
    expect(fmtSpent({ spent_usd: 1.5, arms: { ...measured, A2: { cost_source: "mixed" } } })).toBe("$1.50 (estimated)");
    expect(fmtSpent({ spent_usd: 1.5, arms: { ...measured, A2: {} } })).toBe("$1.50 (estimated)");
    expect(fmtSpent({ spent_usd: 1.5 })).toBe("$1.50 (estimated)");
    expect(fmtSpent({ arms: measured })).toBe("—");
  });

  it("shows run settings as text, never markup", () => {
    expect(paramText(["A", "C"])).toBe("A, C");
    expect(paramText(true)).toBe("true");
    expect(paramText(null)).toBe("—");
    expect(paramText({ limit: 50 })).toBe('{"limit":50}');
    expect(paramText({ long: "x".repeat(300) })).toHaveLength(200);
  });
});
