import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { SideNav } from "./side-nav";

const mocks = vi.hoisted(() => ({ analyzerAllowed: true }));
vi.mock("next/navigation", () => ({ usePathname: () => "/dashboard" }));
vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ user: { perms: { can_view_document_analyzer: mocks.analyzerAllowed } } }),
}));
vi.mock("@/lib/branding", () => ({
  BrandLogo: () => <span />,
  useBranding: () => ({ name: "Welcome" }),
}));

beforeEach(() => {
  mocks.analyzerAllowed = true;
  localStorage.clear();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("standalone Luccy navigation", () => {
  it("opens Luccy separately while ordinary workspace links stay in the current tab", () => {
    render(<SideNav />);
    expect(screen.getByRole("link", { name: "Luccy (Beta)" })).toHaveAttribute("href", "/document-analyzer");
    expect(screen.getByRole("link", { name: "Luccy (Beta)" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "Luccy (Beta)" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("target");
  });

  it("omits the Luccy link when its permission is disabled", () => {
    mocks.analyzerAllowed = false;
    render(<SideNav />);
    expect(screen.queryByRole("link", { name: "Luccy (Beta)" })).not.toBeInTheDocument();
  });
});
