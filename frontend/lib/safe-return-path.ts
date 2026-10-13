/** Retain document version, page and anchor across sign-in without accepting an external redirect. */
export function safeReturnPath(value: string | null | undefined, fallback = "/dashboard"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return fallback;
  if (Array.from(value).some((char) => char === "\\" || char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)) {
    return fallback;
  }
  return value;
}
