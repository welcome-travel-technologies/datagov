import { describe, it, expect } from "vitest";
import { cn, fmtInt, getCookie } from "@/lib/utils";

describe("cn", () => {
  it("merges and dedupes tailwind classes", () => {
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("text-sm", false && "hidden", "font-bold")).toBe("text-sm font-bold");
  });
});

describe("fmtInt", () => {
  it("formats numbers with thousands separators", () => {
    expect(fmtInt(1234567)).toBe("1,234,567");
    expect(fmtInt(0)).toBe("0");
  });
  it("renders an em dash for nullish/NaN", () => {
    expect(fmtInt(null)).toBe("—");
    expect(fmtInt(undefined)).toBe("—");
    expect(fmtInt(NaN)).toBe("—");
  });
});

describe("getCookie", () => {
  it("reads a named cookie value", () => {
    document.cookie = "csrftoken=abc123";
    expect(getCookie("csrftoken")).toBe("abc123");
    expect(getCookie("missing")).toBeNull();
  });
});
