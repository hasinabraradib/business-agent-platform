import { expect, test } from "@playwright/test";
import { signIn } from "./helpers";
import { API, state } from "./stack";

test.describe.configure({ mode: "serial" });

test("sign in: the key ends up only in an httpOnly cookie", async ({ page, context }) => {
  await signIn(page);
  const cookies = await context.cookies();
  const session = cookies.find((c) => c.name === "bap_session");
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe("Lax");
  expect(session?.value).not.toContain("bap_admin");
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }) + document.cookie);
  expect(stored).not.toContain("bap_admin");
  expect(stored).not.toContain("bap_session"); // not readable from JavaScript
});

test("a wrong key is refused in plain language", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Admin API key").fill("bap_admin_thisisnotarealkeyatall1234567890");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("wasn't accepted");
});

test("see a conversation, reply as staff, hand back", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Inbox" }).click();
  await page.getByRole("button", { name: "Waiting for team" }).click();
  await page.getByRole("link", { name: /Website visitor.*Waiting for team/ }).first().click();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByText("Customer").first()).toBeVisible();

  await page.getByLabel("Reply as a team member").fill("Hi, this is the e2e test. We'll call you shortly.");
  await page.getByRole("button", { name: "Send reply" }).click();
  await expect(page.getByRole("status")).toContainText("Reply sent");
  await expect(page.getByText("Hi, this is the e2e test.")).toBeVisible();
  await expect(page.getByText("With team").first()).toBeVisible();

  await page.getByRole("button", { name: "Hand back to AI" }).click();
  await expect(page.getByRole("status")).toContainText("Handed back");
  await expect(page.getByText("Assistant", { exact: true }).first()).toBeVisible();
});

test("open a run trace from an assistant reply", async ({ page }) => {
  await signIn(page);
  await page.goto("/inbox?q=Kacchi");
  await page.getByLabel("Search conversations").fill("Kacchi");
  await page.getByRole("link", { name: /Website visitor/ }).first().click();
  await page.getByRole("link", { name: "View trace" }).first().click();
  await expect(page.getByRole("heading", { name: "Run trace" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Search 1/ })).toBeVisible();
  await expect(page.getByText("fake:fake-chat")).toBeVisible();
});

test("upload a document and see it indexed", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Knowledge" }).click();
  await page.getByLabel("Choose files to upload").setInputFiles({
    name: "e2e-parking.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(`# Valet parking\n\nWe offer valet parking on Fridays (run ${Date.now()}).\n`),
  });
  await expect(page.getByRole("cell", { name: /e2e-parking/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /e2e-parking/ }).getByText("Ready")).toBeVisible({ timeout: 30_000 });
});

test("close a knowledge gap with an answer", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Knowledge gaps" }).click();
  const gap = page.getByRole("article").filter({ hasText: /pizza/i }).first();
  await expect(gap).toBeVisible();
  await gap.getByRole("button", { name: "Add an answer" }).click();
  await page.getByLabel("Answer").fill("We don't serve pizza. Try our Kacchi Biryani instead.");
  await page.getByRole("button", { name: "Save answer" }).click();
  await expect(page.getByRole("status")).toContainText("Added to your knowledge");
  await expect(page.getByRole("article").filter({ hasText: /pizza/i })).toHaveCount(0);
});

test("change a setting and see it saved", async ({ page }) => {
  await signIn(page);
  await page.getByRole("link", { name: "Settings" }).click();
  const promise = page.getByLabel("Reply-time promise");
  await promise.fill("within two hours");
  await page.locator("#business").locator("..").getByRole("button", { name: "Save" }).click();
  await expect(page.locator("#business").locator("..").getByRole("status")).toHaveText("Saved.");
  const tenant = await (await fetch(`${API}/v1/tenant`, { headers: { Authorization: `Bearer ${state().key}` } })).json();
  expect(tenant.settings.follow_up_promise).toBe("within two hours");
  await page.reload();
  await expect(page.getByLabel("Reply-time promise")).toHaveValue("within two hours");
});

test("sign out ends the session", async ({ page }) => {
  await signIn(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto("/inbox");
  await expect(page).toHaveURL(/\/login/);
});
