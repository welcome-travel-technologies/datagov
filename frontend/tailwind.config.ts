import type { Config } from "tailwindcss";

/** Wraps a bare-oklch CSS variable so Tailwind opacity modifiers (e.g. bg-ok/15) work. */
const c = (v: string) => `oklch(var(${v}) / <alpha-value>)`;

const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Hanken Grotesk"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      colors: {
        // shadcn/ui CSS-variable palette — values set in app/globals.css.
        border: c("--border"),
        input: c("--input"),
        ring: c("--ring"),
        background: c("--background"),
        foreground: c("--foreground"),
        primary: { DEFAULT: c("--primary"), foreground: c("--primary-foreground") },
        secondary: { DEFAULT: c("--secondary"), foreground: c("--secondary-foreground") },
        muted: { DEFAULT: c("--muted"), foreground: c("--muted-foreground") },
        accent: { DEFAULT: c("--accent"), foreground: c("--accent-foreground") },
        destructive: { DEFAULT: c("--destructive"), foreground: c("--destructive-foreground") },
        card: { DEFAULT: c("--card"), foreground: c("--card-foreground") },

        // datamov "paper" design tokens
        panel: c("--panel"),
        panel2: c("--panel-2"),
        faint: c("--faint"),
        line: c("--line"),
        "line-strong": c("--line-strong"),
        brand: c("--brand"),
        ok: c("--ok"),
        run: c("--run"),
        err: c("--err"),
        warn: c("--warn"),

        // Welcome brand palette (kept so legacy welcome-* names map cleanly)
        "welcome-teal": c("--welcome-teal"),
        "welcome-tealhover": c("--welcome-teal-hover"),
        "welcome-blue": c("--welcome-blue"),
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      boxShadow: {
        card: "0 1px 2px rgb(0 0 0 / 0.04)",
      },
    },
  },
  plugins: [],
};

export default config;
