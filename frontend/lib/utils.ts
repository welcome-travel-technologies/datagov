import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn convention: `cn("foo", condition && "bar")` -> merged class string. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Read a cookie by name (used for Django's csrftoken on write requests). */
export function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie
    .split(";")
    .map((c) => c.trim())
    .find((c) => c.startsWith(name + "="));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null;
}

/** Compact integer formatting: 1234 -> "1,234"; null/undefined -> "—". */
export function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-US");
}
