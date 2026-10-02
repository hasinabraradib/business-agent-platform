import assert from "node:assert/strict";
import { test } from "node:test";
import {
  contrastRatio,
  DARK_TEXT,
  LIGHT_TEXT,
  readableTextOn,
  relativeLuminance,
  safeAccent,
} from "../src/color.ts";
import { tokens } from "../src/tokens.ts";

test("luminance and contrast follow WCAG", () => {
  assert.equal(relativeLuminance("#000000"), 0);
  assert.equal(relativeLuminance("#FFFFFF"), 1);
  assert.equal(contrastRatio("#000000", "#FFFFFF"), 21);
  assert.ok(Math.abs(contrastRatio("#777777", "#FFFFFF") - 4.48) < 0.01);
});

test("dark text on a light accent, white text on a dark accent", () => {
  for (const light of ["#C5EE4F", "#F2C14E", "#FFFFFF", "#9BE7FF"]) {
    assert.equal(readableTextOn(light), DARK_TEXT, light);
  }
  for (const dark of ["#B5432F", "#111111", "#1E3A8A", "#0F766E"]) {
    assert.equal(readableTextOn(dark), LIGHT_TEXT, dark);
  }
});

test("the chosen bubble text always meets WCAG AA (4.5:1) for typical accents", () => {
  for (const accent of ["#C5EE4F", "#F2C14E", "#B5432F", "#8B7CF6", "#0EA5E9", "#E11D48"]) {
    const ratio = contrastRatio(accent, readableTextOn(accent));
    assert.ok(ratio >= 4.5, `${accent} -> ${ratio.toFixed(2)}`);
  }
});

test("token text colours meet WCAG AA on the surfaces they are used on", () => {
  const c = tokens.color;
  const pairs: [string, string][] = [
    [c.ink, c.surface],
    [c.ink, c.canvas],
    [c.ink, c.subtle],
    [c.muted, c.surface],
    [c.muted, c.canvas],
    [c.violetInk, c.violetSoft],
    [c.danger, c.dangerSoft],
    ["#FFFFFF", c.ink],
  ];
  for (const [text, background] of pairs) {
    const ratio = contrastRatio(text, background);
    assert.ok(ratio >= 4.5, `${text} on ${background}: ${ratio.toFixed(2)}`);
  }
});

test("invalid accents fall back to the default", () => {
  assert.equal(safeAccent("#B5432F", "#C5EE4F"), "#B5432F");
  for (const bad of ["red", "#FFF", "url(x)", "#12345g", 42, null]) {
    assert.equal(safeAccent(bad, "#C5EE4F"), "#C5EE4F");
  }
});
