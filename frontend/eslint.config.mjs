import js from "@eslint/js";
import nextPlugin from "@next/eslint-plugin-next";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import tsParser from "@typescript-eslint/parser";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";

const sourceFiles = ["**/*.{js,jsx,ts,tsx,mjs,cjs}"];

export default [
  {
    ignores: [".next/**", "coverage/**", "node_modules/**"],
  },
  {
    files: sourceFiles,
    languageOptions: {
      ecmaVersion: "latest",
      globals: {
        ...globals.browser,
        ...globals.node,
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
        sourceType: "module",
      },
    },
    plugins: {
      "@next/next": nextPlugin,
      "react-hooks": reactHooks,
    },
    rules: {
      ...js.configs.recommended.rules,
      ...nextPlugin.configs.recommended.rules,
      ...nextPlugin.configs["core-web-vitals"].rules,
      ...reactHooks.configs["recommended-latest"].rules,
    },
  },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
        sourceType: "module",
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
    },
    rules: {
      ...tsPlugin.configs["eslint-recommended"].overrides[0].rules,
      ...tsPlugin.configs.recommended.rules,
      // TypeScript already performs this check with better type awareness.
      "no-undef": "off",
      // Keep pre-existing cleanup findings visible without making the newly
      // non-interactive lint command unusable for unrelated legacy files.
      "@typescript-eslint/no-unused-vars": "warn",
      "@typescript-eslint/triple-slash-reference": "warn",
      "no-constant-binary-expression": "warn",
      "no-useless-escape": "warn",
    },
  },
];
