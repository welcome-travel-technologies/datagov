import { describe, expect, it, vi } from "vitest";
import { collectPaginated, extractApiErrorMessage, type Paginated } from "@/lib/api";

describe("API response helpers", () => {
  it("surfaces DRF field-validation messages", () => {
    expect(
      extractApiErrorMessage(
        { name: ['A definition named "Revenue" already exists in this organization.'] },
        "Bad Request",
      ),
    ).toBe('A definition named "Revenue" already exists in this organization.');
  });

  it("prefers top-level API detail and falls back safely", () => {
    expect(extractApiErrorMessage({ detail: "Admin access required." }, "Forbidden")).toBe(
      "Admin access required.",
    );
    expect(extractApiErrorMessage({}, "Bad Request")).toBe("Bad Request");
  });

  it("consumes every page even when the endpoint ignores limit", async () => {
    const page = (number: number, next: string | null): Paginated<number> => ({
      count: 125,
      next,
      previous: number === 1 ? null : `?page=${number - 1}`,
      results: Array.from({ length: number < 3 ? 50 : 25 }, (_, i) => (number - 1) * 50 + i),
    });
    const fetchPage = vi
      .fn<(pageNumber: number) => Promise<Paginated<number>>>()
      .mockResolvedValueOnce(page(1, "?page=2"))
      .mockResolvedValueOnce(page(2, "?page=3"))
      .mockResolvedValueOnce(page(3, null));

    const result = await collectPaginated(fetchPage);

    expect(result).toHaveLength(125);
    expect(fetchPage.mock.calls.map(([number]) => number)).toEqual([1, 2, 3]);
  });
});
