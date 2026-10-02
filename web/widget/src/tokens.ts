/**
 * Design tokens: the single source of truth for the house style (colours, radii, spacing,
 * shadows, type, motion). The widget reads them as CSS custom properties; the build also emits
 * dist/tokens.css so the dashboard can reuse exactly the same values.
 */
export const tokens = {
  color: {
    ink: "#111111", // primary buttons, launcher, main text
    inkSoft: "#3B3F46",
    muted: "#5C6370", // timestamps, labels (4.5:1+ on canvas and surface)
    surface: "#FFFFFF",
    canvas: "#F5F6F8", // very light grey background behind white surfaces
    subtle: "#EEF0F3", // customer bubbles, icon buttons
    subtleHover: "#E4E7EC",
    accent: "#C5EE4F", // platform default; each tenant overrides it
    violet: "#8B7CF6", // secondary
    violetSoft: "#F0EDFF", // source chips
    violetInk: "#4A3AB0", // text on violetSoft
    online: "#22C55E",
    danger: "#B42318",
    dangerSoft: "#FEF3F2",
    focus: "#6D5CE8",
  },
  radius: {
    panel: "24px",
    bubble: "18px",
    input: "999px",
    pill: "999px",
  },
  space: {
    xs: "4px",
    sm: "8px",
    md: "12px",
    lg: "16px",
    xl: "20px",
    xxl: "24px",
  },
  shadow: {
    panel: "0 24px 64px -16px rgba(17,17,17,0.22), 0 8px 24px -12px rgba(17,17,17,0.12)",
    launcher: "0 14px 34px -10px rgba(17,17,17,0.45)",
    soft: "0 2px 12px rgba(17,17,17,0.06)",
    focus: "0 0 0 3px rgba(109,92,232,0.45)",
  },
  font: {
    // System fonts only (no download), including Bengali faces on every major platform.
    family:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans", ' +
      '"Noto Sans Bengali", "Hind Siliguri", "Kohinoor Bangla", "Bangla Sangam MN", ' +
      '"Nirmala UI", Vrinda, "Helvetica Neue", Arial, sans-serif',
    size: "15px",
    small: "12px",
    label: "11px",
    lineHeight: "1.5",
  },
  motion: {
    fast: "140ms",
    base: "220ms",
    easing: "cubic-bezier(0.2, 0.8, 0.2, 1)",
  },
} as const;

function kebab(name: string): string {
  return name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

/** `--bap-<group>-<name>: value;` declarations for every token. */
export function tokenDeclarations(): string {
  const lines: string[] = [];
  for (const [group, values] of Object.entries(tokens)) {
    for (const [name, value] of Object.entries(values)) {
      lines.push(`--bap-${group}-${kebab(name)}:${value};`);
    }
  }
  return lines.join("");
}

export function tokensCss(selector: string): string {
  return `${selector}{${tokenDeclarations()}}`;
}
