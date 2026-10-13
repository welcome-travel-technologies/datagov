"use client";

import { useRouter, usePathname } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";
import { safeReturnPath } from "@/lib/safe-return-path";

// `/share/*` is the anonymous metrics-map viewer — no login required.
const PUBLIC_PATHS = ["/login", "/share"];

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const isPublic = PUBLIC_PATHS.some((p) => pathname === p || pathname?.startsWith(`${p}/`));

  useEffect(() => {
    if (loading) return;
    if (!user && !isPublic) {
      const destination = safeReturnPath(window.location.pathname + window.location.search + window.location.hash);
      router.replace(`/login?next=${encodeURIComponent(destination)}`);
    }
  }, [loading, user, pathname, isPublic, router]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-[13px] text-muted-foreground">
        Loading…
      </div>
    );
  }

  return user || isPublic ? <>{children}</> : null;
}
