"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { LogOut, PanelLeftClose, PanelLeftOpen, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
import { BrandLogo, useBranding } from "@/lib/branding";
import {
  NAV_GROUPS,
  FOOTER_ITEMS,
  canSee,
  type NavItem,
} from "@/components/layout/nav-config";

const COLLAPSE_KEY = "sidebar-collapsed";

function NavLink({
  item,
  active,
  collapsed,
}: {
  item: NavItem;
  active: boolean;
  collapsed: boolean;
}) {
  const Icon: LucideIcon = item.icon;
  return (
    <Link
      href={item.href}
      title={collapsed ? item.label : undefined}
      aria-label={collapsed ? item.label : undefined}
      className={cn(
        "group flex h-[34px] items-center gap-2.5 rounded-md px-2.5 text-[13.5px] font-medium transition-colors",
        active
          ? "bg-panel text-foreground shadow-card"
          : "text-muted-foreground hover:bg-foreground/[0.05] hover:text-foreground",
      )}
    >
      <Icon className={cn("h-4 w-4 shrink-0", active ? "text-brand opacity-100" : "opacity-85")} />
      {!collapsed && <span className="truncate">{item.label}</span>}
    </Link>
  );
}

/** Group heading: keeps a constant height in both states so the icons below
 *  don't shift vertically — full label when expanded, a thin divider when
 *  collapsed. */
function GroupHeading({ label, collapsed }: { label: string; collapsed: boolean }) {
  return (
    <div className={cn("flex h-6 items-center", collapsed ? "px-1.5" : "px-2.5")}>
      {collapsed ? (
        <div className="h-px w-full rounded-full bg-line" aria-hidden="true" />
      ) : (
        <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-faint">
          {label}
        </span>
      )}
    </div>
  );
}

export function SideNav() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const perms = user?.perms;
  const isActive = (href: string) => !!pathname && (pathname === href || pathname.startsWith(href + "/"));

  // Persisted collapse state. Start expanded on the server, then sync from
  // localStorage after mount to avoid a hydration mismatch.
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
  }, []);
  const toggle = () => {
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      return next;
    });
  };

  const { name: orgName } = useBranding();
  const initials = (user?.email ?? "")
    .replace(/@.*/, "")
    .slice(0, 2)
    .toUpperCase();

  return (
    <aside
      className={cn(
        "flex shrink-0 flex-col border-r border-line bg-panel2 transition-[width] duration-200 ease-in-out",
        collapsed ? "w-16" : "w-64",
      )}
    >
      {/* brand + collapse toggle */}
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-line px-5">
        <BrandLogo className="h-8 w-8 shrink-0 rounded-lg" fillClassName="fill-brand" fallbackBg="bg-brand/12" />
        {!collapsed && (
          <div className="leading-tight">
            <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-brand">
              {orgName}
            </div>
            <div className="text-[14px] font-semibold -tracking-[0.02em]">DataGov</div>
          </div>
        )}
      </div>

      {/* nav */}
      <nav className="min-h-0 flex-1 overflow-auto px-3 py-3">
        {NAV_GROUPS.map((group) => {
          const items = group.items.filter((i) => canSee(i, perms));
          if (items.length === 0) return null;
          return (
            <div key={group.label || group.items[0]?.href} className="mt-3.5 first:mt-0">
              {group.label && <GroupHeading label={group.label} collapsed={collapsed} />}
              <div className="space-y-0.5">
                {items.map((item) => (
                  <NavLink key={item.href} item={item} active={isActive(item.href)} collapsed={collapsed} />
                ))}
              </div>
            </div>
          );
        })}

        {/* footer settings group */}
        <div className="mt-3.5">
          <GroupHeading label="Settings" collapsed={collapsed} />
          <div className="space-y-0.5">
            {FOOTER_ITEMS.filter((i) => canSee(i, perms)).map((item) => (
              <NavLink key={item.href} item={item} active={isActive(item.href)} collapsed={collapsed} />
            ))}
          </div>
        </div>
      </nav>

      {/* user footer */}
      <div
        className={cn(
          "flex shrink-0 border-t border-line px-3.5 py-3",
          collapsed ? "flex-col items-start gap-2" : "items-center gap-2.5",
        )}
      >
        <div
          className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-brand/10 text-[12px] font-semibold text-brand"
          title={collapsed ? user?.email ?? "Guest" : undefined}
        >
          {initials || "—"}
        </div>
        {!collapsed && (
          <div className="min-w-0 flex-1 leading-tight">
            <div className="truncate text-[12.5px] font-semibold">{user?.email ?? "Guest"}</div>
            <div className="text-[11px] capitalize text-faint">{user?.role ?? "—"}</div>
          </div>
        )}
        <button
          onClick={() => logout()}
          aria-label="Sign out"
          title="Sign out"
          className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-faint transition-colors hover:bg-foreground/[0.05] hover:text-foreground"
        >
          <LogOut className="h-4 w-4" />
        </button>
      </div>

      {/* collapse / expand toggle */}
      <div className="shrink-0 border-t border-line px-3 py-2">
        <button
          onClick={toggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] font-medium text-faint transition-colors hover:bg-foreground/[0.05] hover:text-foreground"
        >
          {collapsed ? (
            <PanelLeftOpen className="h-[18px] w-[18px] shrink-0" />
          ) : (
            <PanelLeftClose className="h-[18px] w-[18px] shrink-0" />
          )}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  );
}
