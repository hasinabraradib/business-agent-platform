/**
 * The signed-in session: the admin API key, sealed with AES-256-GCM into an httpOnly,
 * SameSite=Lax cookie. Browser JavaScript can never read the key (httpOnly, never sent to the
 * client, never in localStorage); only server code unseals it to call the API.
 */
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";

export const COOKIE = "bap_session";
export const MAX_AGE_SECONDS = 12 * 60 * 60;

export interface Session {
  key: string; // the admin API key (server-side only)
  tenant: string; // business name, for the sidebar
  slug: string;
  expires: number; // epoch seconds
}

// Without SESSION_SECRET (e.g. a quick local run) a random secret is made at start: nothing
// secret is committed, and sessions simply end when the server restarts.
let ephemeral: string | null = null;

function configuredSecret(): string {
  const value = process.env.SESSION_SECRET ?? "";
  if (value) return value;
  if (!ephemeral) {
    ephemeral = randomBytes(32).toString("hex");
    console.warn("SESSION_SECRET is not set: using a random one; sessions end on restart.");
  }
  return ephemeral;
}

function secretKey(secret = configuredSecret()): Buffer {
  if (secret.length < 32) {
    throw new Error("SESSION_SECRET must be set to at least 32 random characters");
  }
  return createHash("sha256").update(secret).digest();
}

export function seal(session: Session, secret?: string): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", secretKey(secret), iv);
  const body = Buffer.concat([cipher.update(JSON.stringify(session), "utf8"), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), body]).toString("base64url");
}

export function unseal(value: string | undefined, secret?: string, now = Date.now()): Session | null {
  if (!value) return null;
  try {
    const raw = Buffer.from(value, "base64url");
    const decipher = createDecipheriv("aes-256-gcm", secretKey(secret), raw.subarray(0, 12));
    decipher.setAuthTag(raw.subarray(12, 28));
    const json = Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString("utf8");
    const session = JSON.parse(json) as Session;
    if (typeof session.key !== "string" || session.expires * 1000 < now) return null;
    return session;
  } catch {
    return null; // tampered, wrong secret or malformed: treat as signed out
  }
}

export function cookieOptions(maxAge = MAX_AGE_SECONDS) {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    // Secure by default in production; COOKIE_SECURE=false only for plain-http local setups.
    secure: process.env.COOKIE_SECURE ? process.env.COOKIE_SECURE === "true" : process.env.NODE_ENV === "production",
    path: "/",
    maxAge,
  };
}
