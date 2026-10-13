import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AuthGuard } from "./auth-guard";
import { safeReturnPath } from "@/lib/safe-return-path";

const mocks = vi.hoisted(() => ({ replace: vi.fn(), user: null as { id: number } | null, pathname: "/knowledge/documents/doc-1" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: mocks.replace }), usePathname: () => mocks.pathname }));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: mocks.user, loading: false }) }));
beforeEach(() => { mocks.replace.mockReset(); mocks.user = null; mocks.pathname = "/knowledge/documents/doc-1"; });
afterEach(() => { cleanup(); window.history.replaceState({}, "", "/"); });

describe("document sign-in return", () => {
  it("preserves the version, PDF page and cited chunk without rendering protected children", () => {
    const destination = "/knowledge/documents/doc-1?version=version-1&page=12#chunk-chunk-1";
    window.history.replaceState({}, "", destination);
    render(<AuthGuard><p>Protected document text</p></AuthGuard>);
    expect(mocks.replace).toHaveBeenCalledWith(`/login?next=${encodeURIComponent(destination)}`);
    expect(screen.queryByText("Protected document text")).not.toBeInTheDocument();
    expect(safeReturnPath(destination)).toBe(destination);
  });

  it.each(["https://example.com", "//example.com/path", "/\\example.com", "/\n/example.com", "javascript:alert(1)"])("rejects an unsafe return target %s", (target) => {
    expect(safeReturnPath(target)).toBe("/dashboard");
  });

  it("does not mistake a private route beginning with login for the login page", () => {
    mocks.pathname = "/login-settings";
    window.history.replaceState({}, "", "/login-settings");
    render(<AuthGuard><p>Private</p></AuthGuard>);
    expect(mocks.replace).toHaveBeenCalled();
    expect(screen.queryByText("Private")).not.toBeInTheDocument();
  });
});
