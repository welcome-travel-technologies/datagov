"use client";

import { useRouter, usePathname } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";

// `/share/*` is the anonymous metrics-map viewer — no login required.
const PUBLIC_PATHS = ["/login", "/share"];

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (loading) return;
    const isPublic = PUBLIC_PATHS.some((p) => pathname?.startsWith(p));
    if (!user && !isPublic) {
      router.push(`/login?next=${encodeURIComponent(pathname ?? "/")}`);
    }
  }, [loading, user, pathname, router]);

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-[13px] text-muted-foreground">
        Loading…
      </div>
    );
  }

  return <>{children}</>;
}
