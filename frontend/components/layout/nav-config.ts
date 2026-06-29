import {
  LayoutDashboard,
  BookOpen,
  ClipboardCheck,
  Trophy,
  MessageSquare,
  Database,
  BarChart3,
  GitBranch,
  Trash2,
  TrendingUp,
  Boxes,
  Settings,
  Building2,
  Zap,
  Sigma,
  ListChecks,
  type LucideIcon,
} from "lucide-react";
import type { MePerms } from "@/lib/api";

export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Permission key in MePerms; when omitted the item is always shown. */
  perm?: keyof MePerms;
  /** When true, `is_admin` also unlocks the item. */
  orAdmin?: boolean;
};

export type NavGroup = { label: string; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    // Header-less group: Dashboard sits at the very top with no section heading.
    label: "",
    items: [
      { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
    ],
  },
  {
    label: "Company",
    items: [
      { href: "/dictionary", label: "Data Dictionary", icon: BookOpen, perm: "can_view_dictionary" },
      { href: "/tasks", label: "Task Manager", icon: ClipboardCheck, perm: "can_view_tasks" },
      { href: "/champions", label: "Data Champions", icon: Trophy, perm: "can_view_champions" },
      { href: "/chat", label: "AI Assistant", icon: MessageSquare, perm: "can_view_chat" },
      { href: "/powerbi/catalog", label: "PowerBI Catalog", icon: Database, perm: "can_view_powerbi", orAdmin: true },
      { href: "/powerbi/reports", label: "Report Health & Usage", icon: BarChart3, perm: "can_view_reports" },
      { href: "/powerbi/metrics-map", label: "Metrics Map", icon: Sigma, perm: "can_view_powerbi", orAdmin: true },
    ],
  },
  {
    label: "Analytics",
    items: [
      { href: "/lineage", label: "Lineage Graph", icon: GitBranch, perm: "can_view_lineage" },
      { href: "/powerbi/cleanup", label: "PowerBI Cleanup", icon: Trash2, perm: "can_view_unused" },
      { href: "/powerbi/top-assets", label: "PowerBI Top Assets", icon: TrendingUp, perm: "can_view_insights" },
      { href: "/dbt/catalog", label: "dbt Catalog", icon: Boxes, perm: "can_view_dbt", orAdmin: true },
      { href: "/dbt/cleanup", label: "dbt Cleanup", icon: Trash2, perm: "can_view_dbt", orAdmin: true },
      { href: "/dbt/top-assets", label: "dbt Top Assets", icon: TrendingUp, perm: "can_view_dbt", orAdmin: true },
    ],
  },
];

export const FOOTER_ITEMS: NavItem[] = [
  { href: "/settings/user", label: "User Settings", icon: Settings },
  { href: "/settings/org", label: "Org Settings", icon: Building2, perm: "can_view_org_settings", orAdmin: true },
  { href: "/settings/queues", label: "Queues", icon: ListChecks, perm: "can_view_org_settings", orAdmin: true },
  { href: "/integrations", label: "Integrations", icon: Zap, perm: "can_view_integrations", orAdmin: true },
];

/** Whether a nav item is visible for a given permission set. Missing perms
 *  default to visible so a not-yet-wired `/api/me/` never hides the whole app. */
export function canSee(item: NavItem, perms: MePerms | undefined): boolean {
  if (!item.perm) return true;
  if (!perms) return true;
  if (item.orAdmin && perms.is_admin) return true;
  return perms[item.perm] !== false;
}

/** Flat label lookup for breadcrumbs, keyed by href. */
export const LABEL_BY_HREF: Record<string, string> = [
  ...NAV_GROUPS.flatMap((g) => g.items),
  ...FOOTER_ITEMS,
].reduce<Record<string, string>>((acc, i) => {
  acc[i.href] = i.label;
  return acc;
}, {});
