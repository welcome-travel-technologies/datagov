import { describe, expect, it } from "vitest";
import { evidenceRange, highlightHtml } from "./evidence-highlight";

describe("citation offsets", () => {
  it("maps normalized whitespace and ligatures back to original characters", () => {
    const text = "Intro. The oﬃce\n   policy applies. End.";
    const range = evidenceRange(text, "The office policy applies.");
    expect(range && text.slice(...range)).toBe("The oﬃce\n   policy applies.");
  });
  it("refuses missing or ambiguous passages instead of guessing", () => {
    expect(evidenceRange("policy and policy", "policy")).toBeNull();
    expect(evidenceRange("different wording", "policy")).toBeNull();
  });
  it("escapes source text before returning PDF text-layer markup", () => {
    expect(highlightHtml('<script>alert(1)</script>', 0, 8)).toBe('<mark>&lt;script&gt;</mark>alert(1)&lt;/script&gt;');
  });
});
