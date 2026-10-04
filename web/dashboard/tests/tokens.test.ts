// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { tokensFile } from "../scripts/sync-tokens";

describe("design tokens", () => {
  it("src/app/tokens.css matches web/widget/src/tokens.ts (run `npm run tokens`)", () => {
    expect(readFileSync(new URL("../src/app/tokens.css", import.meta.url), "utf8")).toBe(tokensFile());
  });

  it("carries the house colours", () => {
    expect(tokensFile()).toContain("--bap-color-accent:#C5EE4F");
    expect(tokensFile()).toContain("--bap-color-ink:#111111");
  });
});
