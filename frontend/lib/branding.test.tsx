import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { BrandingApplier } from "./branding";
import { metadata } from "@/app/document-analyzer/layout";

const mocks = vi.hoisted(() => ({ pathname: "/dashboard", name: "Welcome" }));
vi.mock("next/navigation", () => ({ usePathname: () => mocks.pathname }));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: { organization: { name: mocks.name } } }) }));
vi.mock("@tanstack/react-query", () => ({ useQuery: () => ({ data: undefined }) }));

beforeEach(() => { mocks.pathname = "/dashboard"; mocks.name = "Welcome"; });
afterEach(cleanup);

describe("app window title", () => {
  it("provides Luccy's initial server title", () => {
    expect(metadata.title).toBe("Luccy (Beta)");
  });

  it.each(["/document-analyzer", "/document-analyzer/session-1"])("keeps the Luccy title on %s after org branding loads", (pathname) => {
    mocks.pathname = pathname;
    const { rerender } = render(<BrandingApplier />);
    expect(document.title).toBe("Luccy (Beta)");
    mocks.name = "Another organization";
    rerender(<BrandingApplier />);
    expect(document.title).toBe("Luccy (Beta)");
  });

  it("restores the organization title when returning to DataGov", () => {
    mocks.pathname = "/document-analyzer";
    const { rerender } = render(<BrandingApplier />);
    mocks.pathname = "/dashboard";
    rerender(<BrandingApplier />);
    expect(document.title).toBe("Welcome DataGov");
  });
});
