import type { Metadata } from "next";
import "./globals.css";
import { QueryProvider } from "@/lib/query";
import { AuthProvider } from "@/lib/auth";
import { BrandingApplier } from "@/lib/branding";
import { AppShell } from "@/components/layout/app-shell";

export const metadata: Metadata = {
  title: "DataGov",
  description: "Data Governance Hub — catalog, lineage, dictionary and AI assistant.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <QueryProvider>
          <AuthProvider>
            <BrandingApplier />
            <AppShell>{children}</AppShell>
          </AuthProvider>
        </QueryProvider>
      </body>
    </html>
  );
}
