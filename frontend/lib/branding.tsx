"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

/* ---------------------------------------------------------------------------
   Org branding — single source of truth for name, accent colour and logo.

   The colour/name/icon all live on the Organization model (editable in Django
   admin). Authenticated views read the user's own org from /api/me/; the
   login screen (and the initial favicon/title) fall back to the public
   /api/branding/ endpoint. Change the brand by editing the org in admin —
   nothing here is hardcoded except the last-resort default.
--------------------------------------------------------------------------- */

const DEFAULT_NAME = "Welcome";

/** The original "interlock" mark — fallback logo when no icon is uploaded. */
const MARK_PATH =
  "M14 0c7.732 0 14 6.268 14 13.999 0 7.731-6.268 13.999-14 13.999S0 21.73 0 13.999C0 6.268 6.268 0 14 0zm8.827 12.772a.836.836 0 0 0-1.077-.088l-.104.088-5.052 5.052a.834.834 0 0 0 1.077 1.269l.104-.088 5.052-5.052a.835.835 0 0 0 0-1.181zm-11.42 5.052L6.354 12.77a.836.836 0 0 0-1.27 1.077l.089.104 5.052 5.052a.833.833 0 0 0 1.182 0 .835.835 0 0 0 .088-1.078l-.088-.103-5.053-5.052 5.053 5.052zM14 9.897a3.26 3.26 0 0 0-3.257 3.256A3.26 3.26 0 0 0 14 16.41a3.26 3.26 0 0 0 3.257-3.257A3.26 3.26 0 0 0 14 9.896z";

type Oklch = { L: number; C: number; H: number };

/** Convert `#rrggbb` (or `#rgb`) to bare OKLCH components so it can be dropped
 * into the `--brand` CSS var, keeping Tailwind's `<alpha-value>` modifiers
 * (e.g. `bg-brand/12`) working. Returns null for unparseable input. */
function hexToOklch(hex: string): Oklch | null {
  const match = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return null;
  let h = match[1];
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  const lin = (c: number) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const R = lin(r), G = lin(g), B = lin(b);
  const l = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B);
  const m = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1073969566 * B);
  const s = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const Bb = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  const C = Math.sqrt(A * A + Bb * Bb);
  let H = (Math.atan2(Bb, A) * 180) / Math.PI;
  if (H < 0) H += 360;
  return { L, C, H };
}

function fmtOklch({ L, C, H }: Oklch, dL = 0): string {
  const ll = Math.max(0, Math.min(1, L + dL));
  return `${ll.toFixed(4)} ${C.toFixed(4)} ${H.toFixed(2)}`;
}

export type EffectiveBranding = {
  name: string;
  primaryColor: string | null;
  icon: string | null;
};

/** Effective branding for the current context: the signed-in user's org when
 * available, otherwise the public branding endpoint (login screen). */
export function useBranding(): EffectiveBranding {
  const { user } = useAuth();
  const org = user?.organization;
  const { data } = useQuery({
    queryKey: ["branding"],
    queryFn: api.branding,
    staleTime: 5 * 60 * 1000,
  });
  return {
    name: org?.name || data?.name || DEFAULT_NAME,
    primaryColor: org?.primary_color || data?.primary_color || null,
    icon: org?.icon || data?.icon || null,
  };
}

/** Applies the org accent colour, page title and favicon globally. Renders
 * nothing — mount once near the app root (inside Auth + Query providers). */
export function BrandingApplier() {
  const { name, primaryColor, icon } = useBranding();

  useEffect(() => {
    const root = document.documentElement;
    const okl = primaryColor ? hexToOklch(primaryColor) : null;
    const vars = ["--brand", "--welcome-teal", "--ring"] as const;
    if (okl) {
      const base = fmtOklch(okl);
      vars.forEach((v) => root.style.setProperty(v, base));
      // Hover = a touch darker, matching the hand-tuned default token.
      root.style.setProperty("--welcome-teal-hover", fmtOklch(okl, -0.08));
    } else {
      vars.forEach((v) => root.style.removeProperty(v));
      root.style.removeProperty("--welcome-teal-hover");
    }
  }, [primaryColor]);

  useEffect(() => {
    document.title = `${name} DataGov`;
  }, [name]);

  useEffect(() => {
    if (!icon) return;
    let link = document.querySelector<HTMLLinkElement>("link[rel='icon']");
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.href = icon;
  }, [icon]);

  return null;
}

/** The org logo badge. When an icon is uploaded it fills the box as-is (no
 * coloured chrome — the artwork owns its own look); otherwise the built-in mark
 * is shown on a coloured chip. `className` sizes the box (e.g. `h-12 w-12
 * rounded-xl`); `fillClassName`/`fallbackBg` style only the built-in fallback. */
export function BrandLogo({
  className,
  fillClassName = "fill-brand",
  fallbackBg = "bg-brand/12",
}: {
  className?: string;
  fillClassName?: string;
  fallbackBg?: string;
}) {
  const { icon, name } = useBranding();
  if (icon) {
    return (
      <span className={cn("grid place-items-center overflow-hidden", className)}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={icon} alt={name} className="h-full w-full object-contain" />
      </span>
    );
  }
  return (
    <span className={cn("grid place-items-center", fallbackBg, className)}>
      <svg viewBox="0 0 28 28" className="h-[64%] w-[64%]" aria-hidden="true">
        <path className={fillClassName} fillRule="evenodd" d={MARK_PATH} />
      </svg>
    </span>
  );
}
