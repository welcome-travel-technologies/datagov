"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LogOut, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuth } from "@/lib/auth";
import {
  FOOTER_ITEMS,
  canSee,
  type NavItem,
} from "@/components/layout/nav-config";

/** Footer nav items shown in the user menu — excludes Queues and Integrations,
 *  which live only in the sidebar. */
const USER_MENU_ITEMS = FOOTER_ITEMS.filter(
  (i) => i.href !== "/settings/queues" && i.href !== "/integrations",
);

/** Avatar + hover dropdown shown in the top navbar. Surfaces the user's
 *  settings links (User Settings, Org Settings, Queues, Integrations) and a
 *  Sign-out action. Replaces the old sidebar user footer. */
export function UserMenu() {
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const perms = user?.perms;

  const initials = (user?.email ?? "")
    .replace(/@.*/, "")
    .slice(0, 2)
    .toUpperCase();

  const visibleItems = USER_MENU_ITEMS.filter((i) => canSee(i, perms));
  const isActive = (href: string) =>
    !!pathname && (pathname === href || pathname.startsWith(href + "/"));

  return (
    <div className="group relative">
      <button
        type="button"
        className="flex h-9 items-center gap-2 rounded-full border border-transparent pl-1 pr-2.5 text-[13px] font-medium transition-colors hover:border-line hover:bg-foreground/[0.04] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
        aria-haspopup="menu"
        aria-label="User menu"
      >
        <span className="grid h-7 w-7 place-items-center rounded-full bg-brand/10 text-[12px] font-semibold text-brand">
          {initials || "—"}
        </span>
        <ChevronDown className="h-3.5 w-3.5 text-faint transition-transform group-hover:rotate-180" />
      </button>

      {/* Hover/focus-within dropdown. Stays open while hovering into it. */}
      <div
        className="invisible absolute right-0 top-full z-50 mt-1 min-w-[220px] translate-y-0.5 opacity-0 transition-all duration-150 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
        role="menu"
      >
        <div className="overflow-hidden rounded-lg border border-line bg-card shadow-card">
          {/* header */}
          <div className="border-b border-line px-3 py-2.5">
            <div className="truncate text-[13px] font-semibold text-foreground">
              {user?.email ?? "Guest"}
            </div>
            <div className="text-[11px] capitalize text-faint">
              {user?.role ?? "—"}
            </div>
          </div>

          {/* settings links */}
          <nav className="py-1">
            {visibleItems.map((item) => (
              <MenuLink key={item.href} item={item} active={isActive(item.href)} />
            ))}
          </nav>

          {/* sign out */}
          <div className="border-t border-line py-1">
            <button
              onClick={() => logout()}
              role="menuitem"
              className="flex w-full items-center gap-2.5 px-3 py-2 text-[13px] font-medium text-err transition-colors hover:bg-err/10"
            >
              <LogOut className="h-4 w-4" />
              Sign out
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function MenuLink({ item, active }: { item: NavItem; active: boolean }) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      role="menuitem"
      className={cn(
        "flex items-center gap-2.5 px-3 py-2 text-[13px] font-medium transition-colors",
        active
          ? "bg-foreground/[0.04] text-foreground"
          : "text-muted-foreground hover:bg-foreground/[0.04] hover:text-foreground",
      )}
    >
      <Icon className="h-4 w-4 shrink-0 opacity-85" />
      <span className="truncate">{item.label}</span>
    </Link>
  );
}
