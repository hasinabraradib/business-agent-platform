/** Browser calls go to the dashboard's own /api/v1 proxy, which adds the key on the server. */
import { CSRF_HEADER } from "./proxy";

export class RequestError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function describeStatus(status: number, detail?: string): string {
  if (status === 401) return "Your session has ended. Please sign in again.";
  if (status === 403) return "This key isn't allowed to do that.";
  if (status === 404) return "That item no longer exists.";
  if (status === 409 && detail) return detail;
  if (status === 413) return "That file is too large.";
  if (status === 415 && detail) return detail;
  if (status === 422) return detail ? `Please check the form: ${detail}` : "Please check the form.";
  if (status === 429) return "Too many requests right now. Please wait a moment and try again.";
  if (status >= 500) return "The platform had a problem. Please try again in a moment.";
  return detail ?? "Something went wrong. Please try again.";
}

function detailText(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (d && typeof d === "object" ? `${(d as { field?: string }).field ?? ""} ${(d as { message?: string; msg?: string }).message ?? (d as { msg?: string }).msg ?? ""}`.trim() : String(d)))
      .join("; ");
  }
  return undefined;
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET") headers.set(CSRF_HEADER, "1");
  if (init.body && typeof init.body === "string") headers.set("content-type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/api/v1/${path.replace(/^\/+/, "")}`, { ...init, method, headers, credentials: "same-origin" });
  } catch {
    throw new RequestError(0, "Can't reach the dashboard server. Check your connection.");
  }
  if (response.status === 401 && typeof window !== "undefined") {
    window.location.assign("/login?expired=1");
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      detail = detailText((await response.json())?.detail);
    } catch {
      // not JSON
    }
    throw new RequestError(response.status, describeStatus(response.status, detail));
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}
