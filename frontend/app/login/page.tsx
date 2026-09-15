"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertCircle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/lib/auth";
import { BrandLogo, useBranding } from "@/lib/branding";

const GOOGLE_ERRORS: Record<string, string> = {
  cancelled: "Google sign-in was cancelled. You can try again.",
  expired: "Your Google sign-in session expired. Please try again.",
  unavailable: "Google sign-in is currently unavailable. Please try again later.",
  failed: "Google sign-in failed. Please try again.",
  account_conflict: "This Google account could not be linked. Please sign in with your existing account or contact an administrator.",
  inactive: "Your account is inactive. Please contact an administrator.",
};

function LoginForm() {
  const { login } = useAuth();
  const { name: orgName } = useBranding();
  const router = useRouter();
  const search = useSearchParams();
  // Browser URL parsing treats backslashes as slashes and strips some control
  // characters, so reject those as well as external/protocol-relative URLs.
  const rawNext = search.get("next") || "/dashboard";
  const hasUnsafeCharacters = Array.from(rawNext).some(
    (char) => char === "\\" || char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
  );
  const next = rawNext.startsWith("/") && !rawNext.startsWith("//") && !hasUnsafeCharacters
    ? rawNext
    : "/dashboard";
  const googleError = search.get("google_error");
  const { data: googleConfig, isPending: googleConfigPending, refetch: refreshGoogleConfig } = useQuery({
    queryKey: ["google-sign-in-config"],
    queryFn: api.auth.googleConfig,
    staleTime: 60_000,
    retry: false,
  });

  // Only server-held Google verification can request account confirmation.
  const pendingLink = googleConfig?.pending_link;

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(() => {
    if (!googleError) return null;
    return Object.hasOwn(GOOGLE_ERRORS, googleError) ? GOOGLE_ERRORS[googleError] : GOOGLE_ERRORS.failed;
  });
  const [busy, setBusy] = useState(false);
  const [googleBusy, setGoogleBusy] = useState(false);

  async function continueWithGoogle() {
    if (!googleConfig?.enabled || busy || googleBusy) return;
    setGoogleBusy(true);
    setError(null);
    try {
      const { url } = await api.auth.googleStart(next);
      window.location.assign(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : GOOGLE_ERRORS.failed);
      setGoogleBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy || googleBusy) return;
    setBusy(true);
    setError(null);
    try {
      if (pendingLink) {
        const { url } = await api.auth.googleLink(password);
        window.location.assign(url);
        return;
      }
      await login(username, password);
      // The OAuth authorize page (and other Django-served pages) live under
      // /api/, outside the Next app — hard-navigate there so the browser
      // actually reaches Django; SPA routes use client-side navigation.
      if (next.startsWith("/api/")) {
        window.location.assign(next);
      } else {
        router.push(next);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Your credentials didn't match. Please try again.");
      if (pendingLink) {
        setPassword("");
        // Expired/conflicting requests clear the pending link on the server.
        // Keep the specific error visible while updating the available form.
        await refreshGoogleConfig();
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative z-10 w-full max-w-md rounded-2xl border border-line bg-card p-6 shadow-xl sm:p-10">
      <div className="mb-8 flex flex-col items-center">
        <BrandLogo
          className="mb-4 h-14 w-14 shrink-0"
          fillClassName="fill-brand"
          fallbackBg="bg-transparent"
        />
        <span className="text-[10px] font-semibold uppercase tracking-[0.26em] text-brand">{orgName}</span>
        <span className="mt-0.5 text-[23px] font-semibold -tracking-[0.01em]">DataGov</span>
        <p className="mt-4 text-[13px] text-muted-foreground">
          {pendingLink ? "Add Google sign-in to your account" : "Sign in or create your account"}
        </p>
      </div>

      {error && (
        <div role="alert" className="mb-5 flex items-start gap-3 rounded-xl border border-err/20 bg-err/[0.08] p-4 text-[13px] text-err">
          <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}

      <div className="space-y-3">
        <Button
          type="button"
          variant="outline"
          size="lg"
          onClick={continueWithGoogle}
          disabled={googleConfigPending || !googleConfig?.enabled || busy || googleBusy}
          aria-busy={googleConfigPending || googleBusy}
          aria-describedby="google-sign-in-help"
          className="h-12 w-full rounded-xl text-[14px]"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path fill="#4285F4" d="M21.6 12.23c0-.71-.06-1.39-.18-2.05H12v3.88h5.38a4.6 4.6 0 0 1-1.99 3.02v2.51h3.23c1.89-1.74 2.98-4.3 2.98-7.36Z" />
            <path fill="#34A853" d="M12 22c2.7 0 4.96-.9 6.62-2.41l-3.23-2.51c-.9.6-2.05.96-3.39.96-2.6 0-4.81-1.76-5.6-4.12H3.07v2.59A10 10 0 0 0 12 22Z" />
            <path fill="#FBBC05" d="M6.4 13.92a6 6 0 0 1 0-3.84V7.49H3.07a10 10 0 0 0 0 9.02l3.33-2.59Z" />
            <path fill="#EA4335" d="M12 5.96c1.47 0 2.79.5 3.82 1.49l2.87-2.87A9.61 9.61 0 0 0 12 2a10 10 0 0 0-8.93 5.49l3.33 2.59C7.19 7.72 9.4 5.96 12 5.96Z" />
          </svg>
          {googleBusy ? "Connecting to Google..." : pendingLink ? "Use another Google account" : "Continue with Google"}
        </Button>
        <p id="google-sign-in-help" className="text-center text-[12px] leading-relaxed text-muted-foreground" aria-live="polite">
          {googleConfigPending
            ? "Checking Google sign-in..."
            : pendingLink
              ? "Enter your existing password once to add Google sign-in. Your password will continue to work."
              : googleConfig?.enabled
                ? "New here? Google sign-up automatically creates your account with basic access. Already registered? Use the same email to add Google sign-in and keep your password access."
                : "Google sign-in is currently unavailable."}
        </p>
      </div>

      <div className="my-6 flex items-center gap-3 text-[11px] text-muted-foreground">
        <span className="h-px flex-1 bg-line" />
        <span>{pendingLink ? "confirm your existing account" : "or sign in with your password"}</span>
        <span className="h-px flex-1 bg-line" />
      </div>

      <form onSubmit={submit} className="space-y-5">
        {pendingLink ? (
          <div className="space-y-1.5">
            <label htmlFor="google-email" className="ml-1 block text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
              Account email
            </label>
            <Input id="google-email" value={pendingLink.email} readOnly className="h-11 rounded-xl" />
          </div>
        ) : (
          <div className="space-y-1.5">
            <label htmlFor="username" className="ml-1 block text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
              Email or Username
            </label>
            <Input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="name@company.com or username"
              autoFocus
              required
              className="h-11 rounded-xl"
            />
          </div>
        )}

        <div className="space-y-1.5">
          <label htmlFor="password" className="ml-1 block text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
            {pendingLink ? "Existing password" : "Password"}
          </label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            className="h-11 rounded-xl"
          />
        </div>

        <Button type="submit" variant="brand" size="lg" disabled={busy || googleBusy} className="h-12 w-full rounded-xl text-[14px]">
          {pendingLink ? (busy ? "Adding Google sign-in..." : "Add Google sign-in") : (busy ? "Signing in..." : "Sign In")}
        </Button>
      </form>

      <div className="mt-8 border-t border-line pt-6 text-center">
        <p className="text-[11px] font-medium tracking-wide text-faint">© {new Date().getFullYear()} {orgName}. All rights reserved.</p>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="text-[13px] text-muted-foreground">Loading…</div>}>
      <LoginForm />
    </Suspense>
  );
}
