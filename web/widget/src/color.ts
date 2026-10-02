/** WCAG 2.x colour maths, used to pick readable text on the tenant's accent colour. */

export const DARK_TEXT = "#111111";
export const LIGHT_TEXT = "#FFFFFF";
const HEX = /^#([0-9a-f]{6})$/i;

export function parseHex(hex: string): [number, number, number] | null {
  const match = HEX.exec(hex.trim());
  if (!match) return null;
  const value = Number.parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

export function relativeLuminance(hex: string): number {
  const rgb = parseHex(hex);
  if (!rgb) throw new Error(`not a #RRGGBB colour: ${hex}`);
  const [r, g, b] = rgb.map((channel) => {
    const c = channel / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Dark or white text, whichever contrasts more with the background (lime -> dark). */
export function readableTextOn(background: string): string {
  return contrastRatio(background, DARK_TEXT) >= contrastRatio(background, LIGHT_TEXT)
    ? DARK_TEXT
    : LIGHT_TEXT;
}

/** The tenant's accent if it is a valid #RRGGBB colour, otherwise the fallback. */
export function safeAccent(value: unknown, fallback: string): string {
  return typeof value === "string" && HEX.test(value.trim()) ? value.trim() : fallback;
}
