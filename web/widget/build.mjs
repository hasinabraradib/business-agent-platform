// Bundle src/index.ts into one self-contained dist/widget.js, emit dist/tokens.css for the
// dashboard, report sizes, and fail if the gzipped bundle exceeds the budget.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { build } from "esbuild";
import { tokensCss } from "./src/tokens.ts";

const BUDGET_GZIP_BYTES = 12 * 1024;

mkdirSync("dist", { recursive: true });
await build({
  entryPoints: ["src/index.ts"],
  outfile: "dist/widget.js",
  bundle: true,
  minify: true,
  format: "iife",
  target: ["es2020"],
  legalComments: "none",
  charset: "utf8",
});
writeFileSync(
  "dist/tokens.css",
  `/* Generated from web/widget/src/tokens.ts. */\n${tokensCss(":root")}\n`,
);

const bundle = readFileSync("dist/widget.js");
const gzipped = gzipSync(bundle, { level: 9 }).length;
const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;
console.log(`dist/widget.js: ${kb(bundle.length)} raw, ${kb(gzipped)} gzipped (budget ${kb(BUDGET_GZIP_BYTES)})`);
if (gzipped > BUDGET_GZIP_BYTES) {
  console.error(`widget.js is over its gzip budget by ${kb(gzipped - BUDGET_GZIP_BYTES)}`);
  process.exit(1);
}
