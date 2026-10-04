import { describe, expect, it } from "vitest";
import { cookieOptions, seal, unseal } from "@/lib/session";

const SECRET = "x".repeat(40);
const session = { key: "bap_admin_abcdefghijklmnopqrstuvwxyz", tenant: "Nodi Kitchen", slug: "demo-restaurant", expires: Math.floor(Date.now() / 1000) + 600 };

describe("session cookie", () => {
  it("round-trips, and the sealed value does not contain the key", () => {
    const sealed = seal(session, SECRET);
    expect(sealed).not.toContain("bap_admin");
    expect(unseal(sealed, SECRET)).toEqual(session);
  });

  it("rejects tampering, another secret and expiry", () => {
    const sealed = seal(session, SECRET);
    const tampered = `${sealed.slice(0, -2)}${sealed.endsWith("A") ? "B" : "A"}A`;
    expect(unseal(tampered, SECRET)).toBeNull();
    expect(unseal(sealed, "y".repeat(40))).toBeNull();
    expect(unseal(seal({ ...session, expires: 1 }, SECRET), SECRET)).toBeNull();
    expect(unseal(undefined, SECRET)).toBeNull();
    expect(unseal("garbage", SECRET)).toBeNull();
  });

  it("refuses a short secret", () => {
    expect(() => seal(session, "short")).toThrow(/SESSION_SECRET/);
  });

  it("is httpOnly and SameSite=Lax, secure in production unless COOKIE_SECURE=false", () => {
    const env = process.env as Record<string, string | undefined>;
    const before = { node: env.NODE_ENV, secure: env.COOKIE_SECURE };
    env.NODE_ENV = "production";
    delete env.COOKIE_SECURE;
    expect(cookieOptions()).toMatchObject({ httpOnly: true, sameSite: "lax", secure: true, path: "/" });
    env.COOKIE_SECURE = "false";
    expect(cookieOptions().secure).toBe(false);
    env.NODE_ENV = before.node;
    if (before.secure === undefined) delete env.COOKIE_SECURE;
    else env.COOKIE_SECURE = before.secure;
  });
});
