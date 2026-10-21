import { afterEach, describe, expect, it, vi } from "vitest";
import { analyzerApi, isVerbatimAnalyzerAnswer, splitClosingLine, withoutImages } from "./document-analyzer-api";

afterEach(() => { vi.unstubAllGlobals(); });

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

describe("Document Analyzer API", () => {
  it("reads the status endpoint", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(json({ enabled: true, available: false }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(analyzerApi.status()).resolves.toMatchObject({ available: false });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/document-analyzer/status/");
  });
});

describe("web chat answers", () => {
  it("escapes every image opener that is not escaped already, and nothing else", () => {
    const raw = String.raw;
    expect(withoutImages("See ![chart](https://x/p.png?d=1) and [a link](https://x).")).toBe(raw`See \![chart](https://x/p.png?d=1) and [a link](https://x).`);
    expect(withoutImages("![a][ref] ![b] [![c](u)](v)")).toBe(raw`\![a][ref] \![b] [\![c](u)](v)`);
    expect(withoutImages(raw`\![kept](u)`)).toBe(raw`\![kept](u)`);              // \! is already a literal "!"
    expect(withoutImages(raw`\\![img](u)`)).toBe(raw`\\\![img](u)`);            // \\ is a backslash, then an image
    expect(withoutImages(raw`\\\![kept](u)`)).toBe(raw`\\\![kept](u)`);
    expect(withoutImages("Wow! [x] !important")).toBe("Wow! [x] !important");
  });

  it("knows a verbatim analyzer answer only by the API's flag", () => {
    expect(isVerbatimAnalyzerAnswer({ verbatim: true, ref: "0a1b2c3d" })).toBe(true);
    expect(isVerbatimAnalyzerAnswer({ verbatim: true, ref: null })).toBe(true);
    expect(isVerbatimAnalyzerAnswer({ verbatim: false, ref: "0a1b2c3d" })).toBe(false);
    expect(isVerbatimAnalyzerAnswer(null)).toBe(false);
    expect(isVerbatimAnalyzerAnswer(undefined)).toBe(false);
    expect(isVerbatimAnalyzerAnswer({ verbatim: "true" } as never)).toBe(false);
  });

  it("splits the closing line off a verbatim answer, whatever it says", () => {
    const footnote = "Answered by the Document Analyzer · ref 0a1b2c3d";
    expect(splitClosingLine(`Hotels up to **€150**.\n\n- [Travel policy](https://x/wiki/1)\n\n${footnote}\n`))
      .toEqual({ body: "Hotels up to **€150**.\n\n- [Travel policy](https://x/wiki/1)", footnote });
    expect(splitClosingLine(`Answer\r\n\r\n${footnote}  \r\n`)).toEqual({ body: "Answer", footnote });
    expect(splitClosingLine("Nothing found.\n\nAnswered by the Document Analyzer · ref -"))
      .toEqual({ body: "Nothing found.", footnote: "Answered by the Document Analyzer · ref -" });
    expect(splitClosingLine(footnote)).toEqual({ body: "", footnote });
    expect(splitClosingLine("")).toEqual({ body: "", footnote: "" });
  });
});
