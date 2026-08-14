import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Quiet warm-neutral product shell. Each value uses `<alpha-value>`
        // so Tailwind slash-opacity modifiers (e.g. `bg-ink-900/25`) work.
        ink: {
          900: "oklch(0.17 0.010 58 / <alpha-value>)",
          800: "oklch(0.25 0.010 58 / <alpha-value>)",
          700: "oklch(0.34 0.010 58 / <alpha-value>)",
          600: "oklch(0.46 0.010 62 / <alpha-value>)",
          500: "oklch(0.58 0.012 70 / <alpha-value>)",
          400: "oklch(0.73 0.010 76 / <alpha-value>)",
          300: "oklch(0.86 0.009 82 / <alpha-value>)",
          200: "oklch(0.92 0.008 84 / <alpha-value>)",
          100: "oklch(0.955 0.007 86 / <alpha-value>)",
          50:  "oklch(0.982 0.005 88 / <alpha-value>)",
        },
        // Clay primary, close to Claude Design's warm create/send action.
        accent: {
          DEFAULT: "oklch(0.62 0.145 43 / <alpha-value>)",
          deep:    "oklch(0.44 0.120 39 / <alpha-value>)",
          soft:    "oklch(0.945 0.035 55 / <alpha-value>)",
        },
        // App "shell" surfaces — pure white feels sterile against warm bg,
        // so use a barely-warmed white for cards and the chat surface.
        surface: {
          DEFAULT: "oklch(0.990 0.004 88 / <alpha-value>)",
          raised:  "oklch(1 0 0 / <alpha-value>)",
        },
        paper:  "oklch(0.965 0.006 88 / <alpha-value>)",
        vellum: "oklch(0.985 0.005 88 / <alpha-value>)",
      },
      fontFamily: {
        display: ["var(--serif)"],
        sans: ["var(--sans)"],
        serif: ["var(--serif)"],
        mono: ["ui-monospace", "SFMono-Regular", '"JetBrains Mono"', "monospace"],
      },
      boxShadow: {
        soft:   "0 1px 2px rgba(42,33,24,0.035), 0 8px 22px rgba(42,33,24,0.055)",
        raised: "0 1px 2px rgba(42,33,24,0.045), 0 16px 36px rgba(42,33,24,0.075)",
        page:   "0 0.5px 0 rgba(42,33,24,0.04), 0 1px 1px rgba(42,33,24,0.025), 0 22px 48px -22px rgba(42,33,24,0.12)",
      },
      transitionTimingFunction: {
        editorial: "cubic-bezier(0.2, 0.8, 0.2, 1)",
      },
      keyframes: {
        riseIn: {
          from: { opacity: "0", transform: "translateY(4px)" },
          to:   { opacity: "1", transform: "translateY(0)" },
        },
        slideInRight: {
          from: { opacity: "0", transform: "translateX(20px)" },
          to:   { opacity: "1", transform: "translateX(0)" },
        },
        fadeIn: {
          from: { opacity: "0" },
          to:   { opacity: "1" },
        },
      },
      animation: {
        riseIn:        "riseIn 320ms cubic-bezier(0.2, 0.8, 0.2, 1) both",
        slideInRight:  "slideInRight 280ms cubic-bezier(0.2, 0.8, 0.2, 1) both",
        fadeIn:        "fadeIn 200ms ease-out both",
      },
    },
  },
  plugins: [],
} satisfies Config;
