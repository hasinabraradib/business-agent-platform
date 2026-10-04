/** Same-origin proxy from the browser to the platform API. Only allowlisted paths, mutations
 * need the dashboard's CSRF header from the same origin, and the key stays on the server. */
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { allowed, csrfOk } from "@/lib/proxy";
import { apiBase } from "@/lib/server";
import { COOKIE, unseal } from "@/lib/session";

async function forward(request: Request, { params }: { params: Promise<{ path: string[] }> }) {
  const path = (await params).path.join("/");
  const method = request.method.toUpperCase();
  if (!allowed(method, path)) {
    return NextResponse.json({ detail: "Not available from the dashboard" }, { status: 404 });
  }
  if (!csrfOk(method, request.headers, request.headers.get("host"))) {
    return NextResponse.json({ detail: "Request refused" }, { status: 403 });
  }
  const session = unseal((await cookies()).get(COOKIE)?.value);
  if (!session) return NextResponse.json({ detail: "Signed out" }, { status: 401 });

  const url = new URL(request.url);
  const headers: Record<string, string> = { Authorization: `Bearer ${session.key}` };
  const contentType = request.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;
  const init: RequestInit & { duplex?: "half" } = { method, headers, cache: "no-store" };
  if (method !== "GET" && method !== "HEAD" && request.body) {
    init.body = request.body; // streamed through (file uploads)
    init.duplex = "half";
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/v1/${path}${url.search}`, init);
  } catch {
    return NextResponse.json({ detail: "The platform API can't be reached" }, { status: 502 });
  }
  const body = upstream.status === 204 ? null : await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
  });
}

export { forward as GET, forward as POST, forward as PUT, forward as PATCH, forward as DELETE };
