import "./setup.ts";
import assert from "node:assert/strict";
import { test } from "node:test";
import { renderText, safeHttpUrl } from "../src/render.ts";

function render(text: string): HTMLElement {
  const div = document.createElement("div");
  div.appendChild(renderText(document, text));
  return div;
}

test("HTML and script payloads are shown as text, never parsed", () => {
  const payload = '<img src=x onerror="alert(1)"><script>alert(2)</script><b>bold</b>';
  const div = render(payload);
  assert.equal(div.textContent, payload);
  assert.equal(div.querySelector("img, script, b"), null);
  assert.equal(div.children.length, 0);
});

test("only http and https URLs become links", () => {
  const div = render(
    "Menu: https://nodi.example/menu. Old: http://old.example/x, " +
      "bad: javascript:alert(1) data:text/html,<b>x</b> vbscript:msgbox ftp://files.example",
  );
  const links = [...div.querySelectorAll("a")];
  assert.deepEqual(
    links.map((a) => a.getAttribute("href")),
    ["https://nodi.example/menu", "http://old.example/x"],
  );
  assert.deepEqual(
    links.map((a) => a.textContent),
    ["https://nodi.example/menu", "http://old.example/x"],
  );
  for (const link of links) {
    assert.equal(link.getAttribute("target"), "_blank");
    assert.match(link.getAttribute("rel") ?? "", /noopener/);
    assert.match(link.getAttribute("rel") ?? "", /noreferrer/);
  }
  assert.ok(div.textContent?.includes("javascript:alert(1)"));
});

test("a link-looking string with an embedded quote cannot break out of the href", () => {
  const div = render('see https://x.example/"onmouseover="alert(1)');
  const link = div.querySelector("a");
  assert.equal(link?.getAttribute("href"), "https://x.example/");
  assert.deepEqual(new Set(link?.getAttributeNames()), new Set(["href", "rel", "target"]));
});

test("citation markers become superscripts; text is otherwise unchanged", () => {
  const div = render("Kacchi costs 480 taka [1][2].");
  const sups = [...div.querySelectorAll("sup.cite")];
  assert.deepEqual(sups.map((s) => s.textContent), ["1", "2"]);
  assert.equal(div.textContent, "Kacchi costs 480 taka 12.");
});

test("safeHttpUrl rejects non-http protocols", () => {
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl("data:text/html,x"), null);
  assert.equal(safeHttpUrl("not a url"), null);
  assert.equal(safeHttpUrl("https://ok.example/")?.hostname, "ok.example");
});
