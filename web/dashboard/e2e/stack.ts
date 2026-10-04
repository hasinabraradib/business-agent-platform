/** Talking to the e2e stack from tests: keys (created once, kept out of logs and git), API
 * calls, and SQL for demo rows the offline model can't produce (bookings, leads, Telegram). */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";

export const API = process.env.E2E_API_URL ?? "http://localhost:18000";
const STATE = new URL("./.state/", import.meta.url);
const COMPOSE = ["compose", "-p", "bap-e2e", "-f", "../../docker-compose.yml", "-f", "../../docker-compose.e2e.yml"];

export function compose(args: string[], input?: string): string {
  return execFileSync("docker", [...COMPOSE, ...args], { encoding: "utf8", input, stdio: ["pipe", "pipe", "pipe"] });
}

export function createKey(tenant: string): string {
  const out = compose(["run", "--rm", "-T", "migrate", "python", "-m", "app.cli", "create-key", "--tenant", tenant, "--kind", "admin"]);
  const key = out.match(/bap_admin_[A-Za-z0-9_-]+/)?.[0];
  if (!key) throw new Error("could not create an admin key on the e2e stack");
  return key;
}

export function saveState(values: Record<string, string>): void {
  mkdirSync(STATE, { recursive: true });
  writeFileSync(new URL("state.json", STATE), JSON.stringify(values));
}

export function state(): Record<string, string> {
  const file = new URL("state.json", STATE);
  if (!existsSync(file)) throw new Error("run the global setup first");
  return JSON.parse(readFileSync(file, "utf8"));
}

export async function api<T>(key: string, path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API}/v1${path}`, {
    ...init,
    headers: { "content-type": "application/json", Authorization: `Bearer ${key}`, ...init.headers },
  });
  if (!response.ok) throw new Error(`${init.method ?? "GET"} ${path}: ${response.status} ${await response.text()}`);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export async function chat(key: string, visitor: string, message: string, conversation?: string) {
  return api<{ conversation_id: string; outcome: string }>(key, "/chat", {
    method: "POST",
    body: JSON.stringify({ visitor_id: visitor, message, stream: false, ...(conversation ? { conversation_id: conversation } : {}) }),
  });
}

export function sql(statement: string): string {
  return compose(["exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "app", "-v", "ON_ERROR_STOP=1", "-At", "-c", statement]);
}
