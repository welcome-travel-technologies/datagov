import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AppShell } from "./app-shell";

const mocks = vi.hoisted(() => ({
  pathname: "/document-analyzer",
  user: { id: 7 } as { id: number } | null,
  loading: false,
  replace: vi.fn(),
  evidenceWorkspace: vi.fn(({ children }: { children: React.ReactNode }) => <section data-testid="evidence-workspace">{children}</section>),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
  useRouter: () => ({ replace: mocks.replace }),
}));
vi.mock("@/lib/auth", () => ({ useAuth: () => ({ user: mocks.user, loading: mocks.loading }) }));
vi.mock("@/lib/branding", () => ({ useBranding: () => ({ name: "Welcome" }) }));
vi.mock("@/components/layout/side-nav", () => ({ SideNav: () => <nav>DataGov sidebar</nav> }));
vi.mock("@/components/layout/user-menu", () => ({ UserMenu: () => <div>User menu</div> }));
vi.mock("@/components/knowledge/evidence-workspace", () => ({ EvidenceWorkspace: mocks.evidenceWorkspace }));

beforeEach(() => {
  mocks.pathname = "/document-analyzer";
  mocks.user = { id: 7 };
  mocks.loading = false;
  mocks.replace.mockReset();
  mocks.evidenceWorkspace.mockClear();
  window.history.replaceState({}, "", "/document-analyzer");
});
afterEach(() => { cleanup(); window.history.replaceState({}, "", "/"); });

describe("Luccy app shell", () => {
  it.each(["/document-analyzer", "/document-analyzer/session-1"])("renders %s as a full-height app without DataGov workspace chrome", (pathname) => {
    mocks.pathname = pathname;
    render(<AppShell><p>Luccy app</p></AppShell>);
    expect(screen.getByRole("main")).toHaveClass("h-dvh", "overflow-hidden");
    expect(screen.getByText("Luccy app")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.queryByRole("banner")).not.toBeInTheDocument();
    expect(mocks.evidenceWorkspace).not.toHaveBeenCalled();
  });

  it("still requires sign-in and preserves the destination", () => {
    mocks.user = null;
    render(<AppShell><p>Luccy app</p></AppShell>);
    expect(screen.queryByText("Luccy app")).not.toBeInTheDocument();
    expect(mocks.replace).toHaveBeenCalledWith("/login?next=%2Fdocument-analyzer");
  });

  it("does not render protected Luccy content while authentication loads", () => {
    mocks.loading = true;
    render(<AppShell><p>Luccy app</p></AppShell>);
    expect(screen.queryByText("Luccy app")).not.toBeInTheDocument();
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it.each(["/dashboard", "/document-analyzer-settings"])("retains the DataGov workspace for %s", (pathname) => {
    mocks.pathname = pathname;
    render(<AppShell><p>Workspace content</p></AppShell>);
    expect(screen.getByRole("navigation")).toHaveTextContent("DataGov sidebar");
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByTestId("evidence-workspace")).toContainElement(screen.getByText("Workspace content"));
  });
});
