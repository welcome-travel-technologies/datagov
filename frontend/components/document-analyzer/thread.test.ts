import { afterEach, describe, expect, it, vi } from "vitest";
import { clearAnalyzerThreads, historyOf, loadThread, parseThread, saveThread, syncThread, threadKey, type Turn } from "./thread";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

const answered = (question: string, text: string): Turn => ({
  id: question, question, style: "", started: 0, pending: false,
  answer: { text, footer: "", citations: [], gaps: [], log: null, checking: false, cut: false },
});

describe("analyzer thread storage", () => {
  it("starts a fresh thread when nothing or something unreadable is stored", () => {
    expect(loadThread(7)).toMatchObject({ turns: [], style: "", sources: ["confluence", "jira", "drive", "github"] });
    localStorage.setItem(threadKey(7), "{not json");
    expect(loadThread(7).conversation).toMatch(/^[a-z0-9]{24}$/);
    localStorage.setItem(threadKey(7), JSON.stringify({ conversation: "bad id!", turns: [] }));
    expect(loadThread(7).conversation).not.toBe("bad id!");
  });

  it("keeps only well-formed turns, citations and sources", () => {
    localStorage.setItem(threadKey(7), JSON.stringify({
      conversation: "c1", sources: ["jira", "slack"], style: "plain",
      turns: [
        { id: "a", question: "Q?", style: "plain", started: 5, pending: false, answer: { text: "A", citations: [{ id: "1", title: "T", url: "https://x" }, { title: "no id" }], log: "nothex!", gaps: ["g", 3] } },
        { id: "b" },
        "junk",
      ],
    }));
    const thread = loadThread(7);
    expect(thread).toMatchObject({ conversation: "c1", sources: ["jira"], style: "plain" });
    expect(thread.turns).toHaveLength(1);
    expect(thread.turns[0].answer).toMatchObject({ text: "A", citations: [{ id: "1", title: "T", url: "https://x" }], log: null, gaps: ["g"] });
  });

  it("works when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    const thread = loadThread(7);
    expect(thread.turns).toEqual([]);
    expect(() => saveThread(7, thread)).not.toThrow();
  });

  it("builds history from answered turns only", () => {
    const pending: Turn = { id: "p", question: "Still running?", style: "", started: 0, pending: true };
    const failed: Turn = { id: "f", question: "Failed?", style: "", started: 0, pending: false, error: "x" };
    expect(historyOf([answered("One?", "First line\nmore"), failed, answered("Two?", "Second"), pending])).toEqual([
      { who: "you", text: "One?" }, { who: "luccy", text: "First line" },
      { who: "you", text: "Two?" }, { who: "luccy", text: "Second" },
    ]);
  });

  it("keeps luccy's old-documents warning with a saved answer", () => {
    localStorage.setItem(threadKey(7), JSON.stringify({
      conversation: "c1", turns: [{ id: "a", question: "Q?", answer: { text: "A", stale: "⚠ These documents are 21 days old" } }],
    }));
    expect(loadThread(7).turns[0].answer?.stale).toBe("⚠ These documents are 21 days old");
    expect(parseThread(null).turns).toEqual([]);
    expect(parseThread("null").turns).toEqual([]);
  });

  it("starts a new conversation instead of reusing a saved id the backend refuses (\"dg…\")", () => {
    for (const reserved of ["dgabcdefghijklmnopqrstuv", "DGabc", "dGabc"]) {
      const thread = parseThread(JSON.stringify({ conversation: reserved, turns: [] }));
      expect(thread.conversation).not.toBe(reserved);
      expect(thread.conversation.slice(0, 2).toLowerCase()).not.toBe("dg");
    }
    expect(parseThread(JSON.stringify({ conversation: "pdgabc", turns: [] })).conversation).toBe("pdgabc");
  });

  it("follows another tab's thread, keeping the answer this tab streams in the same chat", () => {
    const thread = (conversation: string, turns: Turn[]) => ({ conversation, turns, sources: [], style: "" as const });
    const running: Turn = { id: "r", question: "Running?", style: "", started: 2, pending: true, stage: "retrieve" };
    const elsewhere = { ...answered("Other tab?", "Answer"), started: 3 };
    const current = thread("c1", [answered("One?", "A"), running]);
    const incoming = thread("c1", [answered("One?", "A"), { ...running, stage: "starting" }, elsewhere]);

    expect(syncThread(current, incoming, null)).toBe(incoming);
    expect(syncThread(current, incoming, "r").turns.map((t) => [t.id, t.stage]))
      .toEqual([["One?", undefined], ["r", "retrieve"], ["Other tab?", undefined]]);
    const newChat = thread("c2", []);
    expect(syncThread(current, newChat, "r")).toBe(newChat);   // a new chat in the other tab wins
  });

  it("clears every analyzer thread and nothing else", () => {
    localStorage.setItem(threadKey(7), "{}");
    localStorage.setItem(threadKey(8), "{}");
    localStorage.setItem("datagov:other", "keep");
    clearAnalyzerThreads();
    expect(localStorage.getItem(threadKey(7))).toBeNull();
    expect(localStorage.getItem(threadKey(8))).toBeNull();
    expect(localStorage.getItem("datagov:other")).toBe("keep");
    vi.spyOn(Storage.prototype, "key").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => clearAnalyzerThreads()).not.toThrow();
  });
});
