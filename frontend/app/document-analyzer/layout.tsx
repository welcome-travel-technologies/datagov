import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Luccy (Beta)",
  description: "Ask Luccy questions about your organization's documents.",
};

export default function LuccyLayout({ children }: { children: React.ReactNode }) {
  return children;
}
