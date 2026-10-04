/** Sign in (POST {key}) and out (DELETE). The key is checked with the API, then sealed into an
 * httpOnly cookie; it is never returned to the browser. */
import { NextResponse } from "next/server";
import { apiBase } from "@/lib/server";
import { COOKIE, MAX_AGE_SECONDS, cookieOptions, seal } from "@/lib/session";

const KEY = /^bap_(admin|widget)_[A-Za-z0-9_-]{20,}$/;

export async function POST(request: Request) {
  let key = "";
  try {
    key = String((await request.json())?.key ?? "").trim();
  } catch {
    // fall through to the format check
  }
  if (!KEY.test(key)) {
    return NextResponse.json({ error: "That doesn't look like an API key. Admin keys start with bap_admin_." }, { status: 400 });
  }
  if (key.startsWith("bap_widget_")) {
    return NextResponse.json({ error: "That's a widget key. Sign in with an admin key." }, { status: 400 });
  }
  let response: Response;
  try {
    response = await fetch(`${apiBase()}/v1/tenant`, { headers: { Authorization: `Bearer ${key}` }, cache: "no-store" });
  } catch {
    return NextResponse.json({ error: "Can't reach the platform API. Check that it is running." }, { status: 502 });
  }
  if (response.status === 401 || response.status === 403) {
    return NextResponse.json({ error: "That key wasn't accepted. It may have been revoked." }, { status: 401 });
  }
  if (!response.ok) {
    return NextResponse.json({ error: "The platform had a problem. Please try again." }, { status: 502 });
  }
  const tenant = (await response.json()) as { name: string; slug: string; settings?: { business_name?: string } };
  const sealed = seal({
    key,
    tenant: tenant.settings?.business_name || tenant.name,
    slug: tenant.slug,
    expires: Math.floor(Date.now() / 1000) + MAX_AGE_SECONDS,
  });
  const result = NextResponse.json({ ok: true });
  result.cookies.set(COOKIE, sealed, cookieOptions());
  return result;
}

export async function DELETE() {
  const result = NextResponse.json({ ok: true });
  result.cookies.set(COOKIE, "", cookieOptions(0));
  return result;
}
