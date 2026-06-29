"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/lib/auth";
import { BrandLogo, useBranding } from "@/lib/branding";

function LoginForm() {
  const { login } = useAuth();
  const { name: orgName } = useBranding();
  const router = useRouter();
  const search = useSearchParams();
  const next = search.get("next") || "/dashboard";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      router.push(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Your credentials didn't match. Please try again.");
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
        <p className="mt-4 text-[13px] text-muted-foreground">Sign in to your account</p>
      </div>

      <form onSubmit={submit} className="space-y-5">
        {error && (
          <div className="flex items-start gap-3 rounded-xl border border-err/20 bg-err/[0.08] p-4 text-[13px] text-err">
            <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

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

        <div className="space-y-1.5">
          <label htmlFor="password" className="ml-1 block text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
            Password
          </label>
          <Input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            className="h-11 rounded-xl"
          />
        </div>

        <Button type="submit" variant="brand" size="lg" disabled={busy} className="h-12 w-full rounded-xl text-[14px]">
          {busy ? "Signing in…" : "Sign In"}
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
