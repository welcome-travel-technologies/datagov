import { describe, expect, it, vi } from "vitest";
import { ACTIVE_EVIDENCE_ID, highlightAnalyzerDocument } from "./analyzer-document-highlight";

const passage = (text: string, heading = "") => ({ text, heading });
const parsed = (html: string) => new DOMParser().parseFromString(html, "text/html");

describe("analyzer document passage highlighting", () => {
  it("marks the full cited passage across inline elements and block boundaries", () => {
    const body = '<h2>Strategy</h2><p>Establish <strong>Welcome</strong> as a trusted partner.</p><p>The next phase starts now.</p>';
    const result = highlightAnalyzerDocument(body, [passage("Establish Welcome as a trusted partner.\nThe next phase starts now.")], 0);
    const doc = parsed(result.html);
    expect(result.matchedPassages).toEqual([0]);
    expect(result.activeMatched).toBe(true);
    expect(doc.querySelector("strong mark")?.textContent).toBe("Welcome");
    expect(Array.from(doc.querySelectorAll("mark"), (mark) => mark.textContent).join("")).toBe("Establish Welcome as a trusted partner.The next phase starts now.");
    expect(doc.querySelectorAll(`#${ACTIVE_EVIDENCE_ID}`)).toHaveLength(1);
    expect(doc.querySelector("h2 mark")).toBeNull();
  });

  it("normalizes whitespace, case, accents, compatibility forms and emoji without corrupting original text", () => {
    const body = "<p>The o\ufb03ce\u00a0 CAFÉ\n  policy is 🧑‍💻 ready.</p>";
    const result = highlightAnalyzerDocument(body, [passage("the office cafe\u0301 policy is 🧑‍💻 ready.")], 0);
    expect(result.activeMatched).toBe(true);
    expect(parsed(result.html).querySelector("mark")?.textContent).toBe("The o\ufb03ce\u00a0 CAFÉ\n  policy is 🧑‍💻 ready.");
  });

  it("keeps unmatched and repeated passages unmarked instead of guessing", () => {
    const result = highlightAnalyzerDocument("<h2>Strategy</h2><p>Same policy.</p><p>Same policy.</p>", [passage("Same policy."), passage("Different policy.", "Strategy")], 0);
    expect(result.matchedPassages).toEqual([]);
    expect(result.activeMatched).toBe(false);
    expect(parsed(result.html).querySelector("mark")).toBeNull();
  });

  it("matches the visible form of Markdown passages without removing code punctuation", () => {
    const body = '<p>The <strong>office</strong> policy applies to <a href="https://example.com">everyone</a> &amp; staff.</p><ul><li>Use <code>is_valid</code> and <code>**literal**</code>.</li></ul>';
    const text = 'The **office** policy applies to [everyone](https://example.com) &amp; staff.\n- Use `is_valid` and `**literal**`.';
    const result = highlightAnalyzerDocument(body, [passage(text)], 0);
    expect(result.matchedPassages).toEqual([0]);
    expect(parsed(result.html).querySelector("code mark")?.textContent).toBe("is_valid");
    expect(parsed(result.html).body.textContent).toContain("**literal**");
  });

  it("matches entire Markdown table passages against rendered table cells", () => {
    const body = "<table><thead><tr><th>Team</th><th>Owner</th></tr></thead><tbody><tr><td>Growth</td><td>Ana</td></tr></tbody></table>";
    const result = highlightAnalyzerDocument(body, [passage("| Team | Owner |\n| --- | --- |\n| Growth | Ana |")], 0);
    expect(result.matchedPassages).toEqual([0]);
    expect(parsed(result.html).querySelectorAll("th mark, td mark")).toHaveLength(4);
  });

  it("only marks a heading-only passage when the actual heading is unique", () => {
    const result = highlightAnalyzerDocument("<h2>Strategy</h2><p>Strategy changes.</p><h3>Repeated</h3><h3>Repeated</h3>", [passage("", "strategy"), passage("", "Repeated")], 0);
    expect(result.matchedPassages).toEqual([0]);
    expect(parsed(result.html).querySelector("h2 mark")?.textContent).toBe("Strategy");
  });

  it("distinguishes the active passage, including overlapping evidence", () => {
    const body = "<p>First cited sentence. Another unique sentence.</p>";
    const result = highlightAnalyzerDocument(body, [passage("First cited sentence."), passage("Another unique sentence."), passage("unique sentence.")], 2);
    const doc = parsed(result.html);
    expect(result.matchedPassages).toEqual([0, 1, 2]);
    expect(doc.getElementById(ACTIVE_EVIDENCE_ID)?.textContent).toBe("unique sentence.");
    expect(doc.querySelector('mark[data-passage-index="0"]')?.className).toBe("analyzer-evidence");
    expect(doc.getElementById(ACTIVE_EVIDENCE_ID)?.className).toContain("analyzer-evidence-active");
    expect(doc.body.textContent).toBe("First cited sentence. Another unique sentence.");
  });

  it("removes active content and source anchor collisions while preserving quoted source text", () => {
    const body = '<script>alert(1)</script><iframe src="https://example.com"></iframe><a name="active-evidence"></a><p id="active-evidence" onclick="steal()">Use &lt;script&gt; safely.</p><a href="java&#x09;script:alert(1)" onmouseover="steal()">A link</a>';
    const result = highlightAnalyzerDocument(body, [passage("Use <script> safely.")], 0);
    const doc = parsed(result.html);
    expect(doc.querySelector("script, iframe, [onclick], [onmouseover], a[href]")).toBeNull();
    expect(doc.querySelector("mark")?.textContent).toBe("Use <script> safely.");
    expect(doc.querySelectorAll(`#${ACTIVE_EVIDENCE_ID}`)).toHaveLength(1);
    expect(doc.querySelector(`[name="${ACTIVE_EVIDENCE_ID}"]`)).toBeNull();
    expect(doc.querySelector("p")?.id).toBe("");
  });

  it("ignores hidden text when locating visible evidence", () => {
    const result = highlightAnalyzerDocument('<p hidden>Unique policy.</p><p>Unique policy.</p>', [passage("Unique policy.")], 0);
    expect(result.matchedPassages).toEqual([0]);
    const doc = parsed(result.html);
    expect(doc.querySelector("[hidden] mark")).toBeNull();
    expect(doc.querySelector("p:not([hidden]) mark")?.textContent).toBe("Unique policy.");
  });

  it("returns an empty safe body on the server until client parsing is available", () => {
    vi.stubGlobal("DOMParser", undefined);
    try {
      expect(highlightAnalyzerDocument("<p>Policy.</p>", [passage("Policy.")], 0)).toEqual({ html: "", matchedPassages: [], activeMatched: false });
    } finally { vi.unstubAllGlobals(); }
  });
});
