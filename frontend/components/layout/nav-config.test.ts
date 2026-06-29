import { describe, it, expect } from "vitest";
import { Database } from "lucide-react";
import { canSee, NAV_GROUPS, LABEL_BY_HREF, type NavItem } from "@/components/layout/nav-config";
import type { MePerms } from "@/lib/api";

const ALL_FALSE: MePerms = {
  is_admin: false,
  can_view_dictionary: false,
  can_view_tasks: false,
  can_view_champions: false,
  can_view_chat: false,
  can_view_powerbi: false,
  can_view_reports: false,
  can_view_lineage: false,
  can_view_unused: false,
  can_view_insights: false,
  can_view_dbt: false,
  can_view_integrations: false,
  can_view_org_settings: false,
};

const item = (over: Partial<NavItem>): NavItem => ({ href: "/x", label: "X", icon: Database, ...over });

describe("canSee", () => {
  it("always shows ungated items", () => {
    expect(canSee(item({}), ALL_FALSE)).toBe(true);
  });
  it("defaults to visible when perms are not yet loaded", () => {
    expect(canSee(item({ perm: "can_view_lineage" }), undefined)).toBe(true);
  });
  it("hides items whose perm is false", () => {
    expect(canSee(item({ perm: "can_view_lineage" }), ALL_FALSE)).toBe(false);
  });
  it("shows items whose perm is true", () => {
    expect(canSee(item({ perm: "can_view_lineage" }), { ...ALL_FALSE, can_view_lineage: true })).toBe(true);
  });
  it("unlocks orAdmin items for admins even when the perm is false", () => {
    expect(canSee(item({ perm: "can_view_dbt", orAdmin: true }), { ...ALL_FALSE, is_admin: true })).toBe(true);
  });
});

describe("nav config", () => {
  it("exposes a label lookup for the lineage route", () => {
    expect(LABEL_BY_HREF["/lineage"]).toBe("Lineage Graph");
  });
  it("keeps the dashboard first", () => {
    expect(NAV_GROUPS[0].items[0].href).toBe("/dashboard");
  });
});
