/**
 * Rules for the browser-to-API proxy (src/app/api/v1/[...path]/route.ts). The browser only
 * ever talks to the dashboard's own origin; the dashboard adds the API key on the server.
 */
export const CSRF_HEADER = "x-bap-dashboard";

// Paths (after /v1/) the dashboard may reach, by method. Anything else is refused.
const RULES: [RegExp, string[]][] = [
  [/^analytics\/overview$/, ["GET"]],
  [/^conversations$/, ["GET"]],
  [/^conversations\/[0-9a-f-]{36}$/, ["GET"]],
  [/^conversations\/[0-9a-f-]{36}\/(read|messages|hand-back|resolve)$/, ["POST"]],
  [/^messages\/[0-9a-f-]{36}\/trace$/, ["GET"]],
  [/^documents$/, ["GET", "POST"]],
  [/^documents\/url$/, ["POST"]],
  [/^documents\/[0-9a-f-]{36}$/, ["GET", "DELETE"]],
  [/^documents\/[0-9a-f-]{36}\/reingest$/, ["POST"]],
  [/^knowledge-gaps$/, ["GET"]],
  [/^knowledge-gaps\/answer$/, ["POST"]],
  [/^(reservations|leads)$/, ["GET"]],
  [/^reservations\/[0-9a-f-]{36}$/, ["PATCH"]],
  [/^tenant$/, ["GET"]],
  [/^tenant\/settings$/, ["PATCH"]],
  [/^webhooks\/endpoint$/, ["GET", "PUT"]],
  [/^webhooks\/endpoint\/rotate-secret$/, ["POST"]],
  [/^webhooks\/(deliveries)$/, ["GET"]],
  [/^webhooks\/test$/, ["POST"]],
  [/^channels\/telegram$/, ["GET", "PUT"]],
  [/^channels\/telegram\/status$/, ["GET"]],
  [/^channels\/telegram\/webhook$/, ["POST"]],
  [/^api-keys$/, ["GET", "POST"]],
  [/^api-keys\/[0-9a-f-]{36}$/, ["DELETE"]],
];

export function allowed(method: string, path: string): boolean {
  return RULES.some(([pattern, methods]) => pattern.test(path) && methods.includes(method));
}

/** Mutations must carry the dashboard's header and come from the dashboard's own origin. A
 * cross-site form cannot set custom headers, and SameSite=Lax keeps the cookie off cross-site
 * POSTs anyway; this is the second lock. */
export function csrfOk(method: string, headers: Headers, host: string | null): boolean {
  if (method === "GET" || method === "HEAD") return true;
  if (headers.get(CSRF_HEADER) !== "1") return false;
  const origin = headers.get("origin");
  if (!origin) return true; // same-origin fetches may omit it; the custom header still applies
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
