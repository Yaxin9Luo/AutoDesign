/** Inline SVG icons — no library, no JS bundle bloat. */
import type { SVGProps } from "react";

/** Editorial-grade icon stroke. 1.4px reads as a hand-drafted hairline at
 *  16-18px sizes; the icon scales gracefully via Tailwind size classes. */
const base = (p: SVGProps<SVGSVGElement>) => ({
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.4,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  ...p,
});

export const I = {
  Send: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M22 2L11 13" />
      <path d="M22 2l-7 20-4-9-9-4 20-7z" />
    </svg>
  ),
  Paperclip: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
    </svg>
  ),
  Sparkle: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" />
    </svg>
  ),
  /** Quiet variant — fewer rays, thinner. Use at 14-16px in dense chrome. */
  SparkleQuiet: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)} strokeWidth={1.2}>
      <path d="M12 4v3M12 17v3M4 12h3M17 12h3M6.5 6.5l2 2M15.5 15.5l2 2" />
    </svg>
  ),
  /** Asterism — three offset glyphs for editorial section breaks. */
  Asterism: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)} strokeWidth={1.2}>
      <circle cx="6" cy="9" r="1" fill="currentColor" stroke="none" />
      <circle cx="18" cy="9" r="1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="15" r="1" fill="currentColor" stroke="none" />
    </svg>
  ),
  ArrowRight: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M5 12h14M13 5l7 7-7 7" />
    </svg>
  ),
  ArrowLeft: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M19 12H5M11 5l-7 7 7 7" />
    </svg>
  ),
  Edit: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 113 3L7 19l-4 1 1-4z" />
    </svg>
  ),
  Copy: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M4 16V6a2 2 0 012-2h10" />
    </svg>
  ),
  Clipboard: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M9 4h6a2 2 0 012 2v1H7V6a2 2 0 012-2z" />
      <rect x="5" y="6" width="14" height="16" rx="2" />
      <path d="M9 12h6M9 16h4" />
    </svg>
  ),
  Paintbrush: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M18.5 3.5a2.1 2.1 0 013 3L13 15l-4 1 1-4z" />
      <path d="M9 16c0 2-1.5 4-5 4 1.2-1.3 1.4-2.5 1.1-3.7A2.2 2.2 0 019 16z" />
    </svg>
  ),
  Duplicate: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="7" y="7" width="11" height="11" rx="1.5" />
      <path d="M4 14V5a1 1 0 011-1h9" />
      <path d="M12.5 10v5M10 12.5h5" />
    </svg>
  ),
  Focus: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M4 9V5a1 1 0 011-1h4M15 4h4a1 1 0 011 1v4M20 15v4a1 1 0 01-1 1h-4M9 20H5a1 1 0 01-1-1v-4" />
      <path d="M9 12h6" />
    </svg>
  ),
  Move: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M12 3v18M3 12h18" />
      <path d="M8 7l4-4 4 4M8 17l4 4 4-4M7 8l-4 4 4 4M17 8l4 4-4 4" />
    </svg>
  ),
  Undo: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M9 7H4v5" />
      <path d="M4 12a8 8 0 112.3 5.7" />
    </svg>
  ),
  Redo: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M15 7h5v5" />
      <path d="M20 12a8 8 0 10-2.3 5.7" />
    </svg>
  ),
  AlignLeft: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M3 6h18M3 12h12M3 18h15" />
    </svg>
  ),
  AlignCenter: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M3 6h18M6 12h12M5 18h14" />
    </svg>
  ),
  AlignRight: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M3 6h18M9 12h12M6 18h15" />
    </svg>
  ),
  Bold: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M6 4h7a4 4 0 010 8H6zM6 12h8a4 4 0 010 8H6z" />
    </svg>
  ),
  Square: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="4" y="4" width="16" height="16" rx="1" />
    </svg>
  ),
  Circle: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <circle cx="12" cy="12" r="8" />
    </svg>
  ),
  Line: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M5 19L19 5" />
    </svg>
  ),
  Arrow: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M5 19L19 5M19 5h-7M19 5v7" />
    </svg>
  ),
  Type: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M4 7V4h16v3M9 20h6M12 4v16" />
    </svg>
  ),
  Image: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="9" cy="9" r="2" />
      <path d="M21 15l-5-5L5 21" />
    </svg>
  ),
  Eye: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
  EyeOff: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M17.94 17.94A10.94 10.94 0 0112 20C5 20 1 12 1 12a18.45 18.45 0 015.06-5.94M9.9 4.24A10.94 10.94 0 0112 4c7 0 11 8 11 8a18.43 18.43 0 01-2.16 3.19M14.12 14.12a3 3 0 11-4.24-4.24M1 1l22 22" />
    </svg>
  ),
  Lock: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 018 0v4" />
    </svg>
  ),
  Unlock: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 018-0" />
    </svg>
  ),
  Trash: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6" />
    </svg>
  ),
  ChevronUp: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M18 15l-6-6-6 6" />
    </svg>
  ),
  ChevronDown: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M6 9l6 6 6-6" />
    </svg>
  ),
  Plus: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  ),
  X: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  ),
  File: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
      <path d="M14 2v6h6" />
    </svg>
  ),
  Save: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M5 3h12l2 2v16H5z" />
      <path d="M8 3v7h8V3" />
      <path d="M8 21v-7h8v7" />
    </svg>
  ),
  Report: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M14 2H7a2 2 0 00-2 2v16a2 2 0 002 2h10a2 2 0 002-2V7z" />
      <path d="M14 2v5h5" />
      <path d="M8 12h8M8 16h5" />
      <circle cx="16.5" cy="17.5" r="2.5" />
      <path d="M18.4 19.4L21 22" />
    </svg>
  ),
  Poster: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="5" y="3" width="14" height="18" rx="1.5" />
      <rect x="8" y="6" width="8" height="5" rx="0.5" />
      <path d="M8 14h8M8 17h5" />
    </svg>
  ),
  Layout: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="3" y="4" width="18" height="16" rx="1.5" />
      <path d="M3 9h18" />
      <rect x="6" y="12" width="5" height="6" rx="0.5" />
      <path d="M13 13h5M13 16h5" />
    </svg>
  ),
  Deck: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="3" y="7" width="13" height="11" rx="1.5" />
      <path d="M7 4h13a1 1 0 011 1v12" />
    </svg>
  ),
  Video: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="3" y="6" width="13" height="12" rx="1.5" />
      <path d="M16 10l5-3v10l-5-3z" />
    </svg>
  ),
  PanelLeft: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 3v18" />
    </svg>
  ),
  PanelRight: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M15 3v18" />
    </svg>
  ),
  Settings: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33h.01a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v.01a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" />
    </svg>
  ),
  Close: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  ),
  Check: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)} strokeWidth={2.5}>
      <path d="M5 12l5 5L20 7" />
    </svg>
  ),
  /** Open triangle with central dot — the warning sigil for failure
   *  cards. Stays editorial: thin stroke, no fill. */
  Alert: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </svg>
  ),
  /** Curved arrow + tail — used for "Retry". Distinct from ArrowRight
   *  so the visual grammar of CTAs stays consistent. */
  Refresh: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M3 12a9 9 0 0115.39-6.36L21 8M21 3v5h-5" />
      <path d="M21 12a9 9 0 01-15.39 6.36L3 16M3 21v-5h5" />
    </svg>
  ),
  /** Two stacked Ts in different sizes — the font-size dropdown on
   *  the floating toolbar. */
  TextSize: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M3 7V5h7v2M6.5 5v14M5 19h3" />
      <path d="M14 11V9h7v2M17.5 9v10M16 19h3" />
    </svg>
  ),
  /** Italic glyph — kept for completeness even though v1 toolbar skips. */
  Italic: (p: SVGProps<SVGSVGElement>) => (
    <svg {...base(p)}>
      <path d="M19 4h-9M14 20H5M15 4L9 20" />
    </svg>
  ),
};
