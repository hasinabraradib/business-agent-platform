import { type Page, expect } from "@playwright/test";
import { state } from "./stack";

export async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Admin API key").fill(state().key);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}
