"use client";

import { usePathname } from "next/navigation";
import { AuthGuard } from "@/components/layout/auth-guard";
import { SideNav } from "@/components/layout/side-nav";
import { useBranding } from "@/lib/branding";
import { LABEL_BY_HREF } from "@/components/layout/nav-config";
import { cn } from "@/lib/utils";

// Routes whose content fills the viewport and manages its own internal
// scrolling (full-height canvas / chat). The page itself must NOT scroll
// vertically: there's nothing to scroll to except the decorative
// .welcome-gradient blob that bleeds ~10rem below <main> (bottom: -10rem),
// which would otherwise extend main's scrollable region into empty space.
const FIXED_HEIGHT_ROUTES = ["/lineage", "/powerbi/metrics-map", "/chat"];

function isFixedHeightRoute(pathname: string | null): boolean {
  if (!pathname) return false;
  return FIXED_HEIGHT_ROUTES.some((r) => pathname === r || pathname.startsWith(r + "/"));
}

function titleCase(s: string) {
  return s.charAt(0).toUpperCase() + s.slice(1).replace(/-/g, " ");
}

/** Breadcrumb segments from the pathname, preferring nav labels. */
function crumbsFor(pathname: string | null): string[] {
  if (!pathname || pathname === "/") return ["Dashboard"];
  // Prefer the full nav label when a known section matches a prefix.
  const known = Object.keys(LABEL_BY_HREF)
    .filter((href) => pathname === href || pathname.startsWith(href + "/"))
    .sort((a, b) => b.length - a.length)[0];
  const parts = pathname.split("/").filter(Boolean);
  if (known) {
    const rest = pathname.slice(known.length).split("/").filter(Boolean);
    return [LABEL_BY_HREF[known], ...rest.map((p) => (/^\d+$/.test(p) ? `#${p}` : titleCase(decodeURIComponent(p))))];
  }
  return parts.map((p) => (/^\d+$/.test(p) ? `#${p}` : titleCase(decodeURIComponent(p))));
}

function Topbar() {
  const pathname = usePathname();
  const crumbs = crumbsFor(pathname);
  const { name: orgName } = useBranding();
  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-line bg-background px-7">
      <div className="flex items-center gap-2 text-[13px] text-faint">
        <span>{orgName}</span>
        {crumbs.map((crumb, i) => (
          <span key={i} className="flex items-center gap-2">
            <span className="opacity-50">/</span>
            <span className={i === crumbs.length - 1 ? "font-semibold text-foreground" : undefined}>
              {crumb}
            </span>
          </span>
        ))}
      </div>
    </header>
  );
}

/** Chrome wrapping every page. Hides the sidebar on /login. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const bare = pathname?.startsWith("/login");
  // The public share viewer renders its own full-screen layout — no app chrome.
  const isShare = pathname?.startsWith("/share");
  const fixedHeight = isFixedHeightRoute(pathname);

  if (isShare) {
    return <AuthGuard>{children}</AuthGuard>;
  }

  return (
    <AuthGuard>
      {bare ? (
        <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-slate-50 p-4">
          {/* Decorative background blobs — soft teal/blue wash, no grid (matches the marketing login look) */}
          <div className="pointer-events-none absolute right-0 top-0 -mr-20 -mt-20 h-80 w-80 rounded-full bg-welcome-teal/10 blur-3xl" />
          <div className="pointer-events-none absolute bottom-0 left-0 -mb-20 -ml-20 h-80 w-80 rounded-full bg-welcome-blue/10 blur-3xl" />
          {children}
        </div>
      ) : (
        <div className="flex h-screen overflow-hidden bg-background">
          <SideNav />
          <div className="flex min-w-0 flex-1 flex-col">
            <Topbar />
            {/* overflow-x-hidden: the decorative .welcome-gradient blobs bleed
                ~8rem past the right edge; without it they'd add a page-level
                horizontal scrollbar (most visible on the full-height lineage
                canvas). Wide content (tables, DAGs) scrolls in its own wrapper.
                overflow-y: full-height routes manage their own internal scroll,
                so the page is locked (overflow-y-hidden) to suppress the phantom
                vertical scroll from the blob that bleeds below <main>; ordinary
                content pages keep overflow-y-auto so long pages scroll. */}
            <main
              className={cn(
                "welcome-gradient min-h-0 flex-1 overflow-x-hidden px-7 py-7",
                fixedHeight ? "overflow-y-hidden" : "overflow-y-auto",
              )}
            >
              {children}
            </main>
          </div>
        </div>
      )}
    </AuthGuard>
  );
}
