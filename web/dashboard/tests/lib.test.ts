import { describe, expect, it } from "vitest";
import { describeStatus } from "@/lib/client";
import { csvCell, toCsv } from "@/lib/csv";
import { customerName, formatBytes, formatMs, formatPercent, formatUsd, relativeTime } from "@/lib/format";
import { checkFile, plainError, typeLabel } from "@/lib/ingestion";
import { inboxQuery } from "@/app/(app)/inbox/InboxClient";
import { formToHours, hoursToForm, maskKey, parseOrigins } from "@/app/(app)/settings/Sections";
import { isActive } from "@/components/Sidebar";

describe("formatting", () => {
  it("formats sizes, durations, money and percentages", () => {
    expect([formatBytes(null), formatBytes(512), formatBytes(2048), formatBytes(3 * 1024 * 1024)]).toEqual(["–", "512 B", "2.0 KB", "3.0 MB"]);
    expect([formatMs(850), formatMs(2340), formatMs(null)]).toEqual(["850 ms", "2.34 s", "–"]);
    expect([formatUsd(0), formatUsd(0.004), formatUsd(1.234)]).toEqual(["$0", "under $0.01", "$1.23"]);
    expect([formatPercent(0.125), formatPercent(null)]).toEqual(["13%", "–"]);
  });

  it("says when, in words", () => {
    const now = Date.parse("2026-10-04T12:00:00Z");
    expect(relativeTime("2026-10-04T11:59:30Z", now)).toBe("just now");
    expect(relativeTime("2026-10-04T11:15:00Z", now)).toBe("45 min ago");
    expect(relativeTime("2026-10-03T11:00:00Z", now)).toBe("yesterday");
  });

  it("names customers without exposing ids", () => {
    expect(customerName({ customer_name: "Rahim Uddin", visitor_id: "tg:5" })).toBe("Rahim Uddin");
    expect(customerName({ customer_name: null, visitor_id: "tg:5" })).toBe("Telegram customer");
    expect(customerName({ customer_name: null, visitor_id: "abc" })).toBe("Website visitor");
  });
});

describe("CSV export", () => {
  it("quotes and neutralises spreadsheet formulas", () => {
    expect(csvCell('He said "hi", then left')).toBe('"He said ""hi"", then left"');
    expect(csvCell("=HYPERLINK(\"x\")")).toBe("\"'=HYPERLINK(\"\"x\"\")\"");
    expect(csvCell("+8801700000111")).toBe("'+8801700000111");
    expect(toCsv([{ a: 1, b: null }], [{ header: "A", value: (r) => r.a }, { header: "B", value: (r) => r.b }])).toBe("A,B\r\n1,\r\n");
  });
});

describe("knowledge uploads", () => {
  it("checks type and size before uploading", () => {
    expect(checkFile({ name: "menu.csv", size: 10 })).toBeNull();
    expect(checkFile({ name: "hours.HTML", size: 10 })).toBeNull();
    expect(checkFile({ name: "photo.jpg", size: 10 })).toMatch(/can't be read/);
    expect(checkFile({ name: "empty.txt", size: 0 })).toMatch(/empty/);
    expect(checkFile({ name: "huge.pdf", size: 11 * 1024 * 1024 })).toMatch(/10 MB/);
  });

  it("explains ingestion errors in plain language", () => {
    expect(plainError("no extractable text in PDF")).toMatch(/scanned PDF/);
    expect(plainError("refusing to fetch a non-public address (10.0.0.5)")).toMatch(/public web pages/);
    expect(plainError("something odd")).toMatch(/couldn't be processed/);
    expect(plainError(null)).toBeNull();
    expect(typeLabel("url")).toBe("Web page");
  });

  it("turns HTTP errors into sentences", () => {
    expect(describeStatus(429)).toMatch(/wait a moment/);
    expect(describeStatus(500)).toMatch(/try again/);
    expect(describeStatus(409, "Set a webhook URL first")).toBe("Set a webhook URL first");
  });
});

describe("screen helpers", () => {
  it("builds the inbox query", () => {
    expect(inboxQuery("", "")).toBe("conversations?limit=100");
    expect(inboxQuery("waiting_human", " kacchi ")).toBe("conversations?limit=100&status=waiting_human&q=kacchi");
  });

  it("edits opening hours without losing split shifts", () => {
    const raw = { mon: [["12:00", "15:00"], ["18:00", "23:00"]], fri: [["14:30", "23:00"]] };
    const form = hoursToForm(raw);
    expect(form.mon).toEqual({ closed: false, open: "12:00", close: "15:00" });
    expect(form.sun.closed).toBe(true);
    form.mon.open = "11:00";
    form.fri.closed = true;
    expect(formToHours(form, raw)).toEqual({ mon: [["11:00", "15:00"], ["18:00", "23:00"]] });
  });

  it("parses origins and masks keys", () => {
    expect(parseOrigins("https://a.example\n https://b.example, ")).toEqual(["https://a.example", "https://b.example"]);
    expect(maskKey({ id: "1", kind: "admin", prefix: "bap_admin_ab12", label: "", created_at: "", revoked_at: null })).toBe("bap_admin_ab12…");
  });

  it("highlights the current section, with traces under the inbox", () => {
    expect(isActive("/", "/")).toBe(true);
    expect(isActive("/inbox/123", "/inbox")).toBe(true);
    expect(isActive("/traces/9", "/inbox")).toBe(true);
    expect(isActive("/inbox", "/")).toBe(false);
  });
});
