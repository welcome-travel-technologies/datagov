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

describe("Luccy (Beta) nav item", () => {
  const analyzer = NAV_GROUPS.find((g) => g.label === "Analytics")?.items.find((i) => i.href === "/document-analyzer");
  it("sits in the Analytics group behind its own permission", () => {
    expect(analyzer).toMatchObject({ label: "Luccy (Beta)", perm: "can_view_document_analyzer", openInNewTab: true });
    expect(analyzer?.orAdmin).toBeFalsy();
    expect(LABEL_BY_HREF["/document-analyzer"]).toBe("Luccy (Beta)");
  });
  it("is last in the Analytics group", () => {
    const analytics = NAV_GROUPS.find((g) => g.label === "Analytics")!;
    expect(analytics.items.at(-1)).toBe(analyzer);
  });
  it("is hidden when the permission is false, even for admins", () => {
    expect(canSee(analyzer!, { ...ALL_FALSE, can_view_document_analyzer: false })).toBe(false);
    expect(canSee(analyzer!, { ...ALL_FALSE, is_admin: true, can_view_document_analyzer: false })).toBe(false);
  });
  it("is shown when the permission is true", () => {
    expect(canSee(analyzer!, { ...ALL_FALSE, can_view_document_analyzer: true })).toBe(true);
  });
});
