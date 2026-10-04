/** Server-only helpers: the current session and calls to the platform API with its key. */
import "server-only";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { COOKIE, type Session, unseal } from "./session";

export function apiBase(): string {
  return (process.env.API_BASE_URL ?? "http://localhost:8000").replace(/\/+$/, "");
}

export async function getSession(): Promise<Session | null> {
  return unseal((await cookies()).get(COOKIE)?.value);
}

export async function requireSession(): Promise<Session> {
  const session = await getSession();
  if (!session) redirect("/login");
  return session;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** GET/POST to /v1/... with the session's key. Throws ApiError with the API's detail. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const session = await requireSession();
  const response = await fetch(`${apiBase()}/v1${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${session.key}` },
    cache: "no-store",
  });
  if (response.status === 401) redirect("/login?expired=1");
  if (!response.ok) {
    let detail = `The platform answered ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}
