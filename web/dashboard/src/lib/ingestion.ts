/** Ingestion errors and statuses in plain language for the knowledge screen. */
export const ACCEPTED_TYPES = [".txt", ".md", ".markdown", ".csv", ".pdf", ".html", ".htm"];
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

export function checkFile(file: { name: string; size: number }): string | null {
  const dot = file.name.lastIndexOf(".");
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!ACCEPTED_TYPES.includes(extension)) {
    return `${file.name}: this file type can't be read. Use text, Markdown, CSV, PDF or HTML.`;
  }
  if (file.size === 0) return `${file.name} is empty.`;
  if (file.size > MAX_UPLOAD_BYTES) return `${file.name} is larger than 10 MB.`;
  return null;
}

const KNOWN: [RegExp, string][] = [
  [/no (extractable )?text|empty|nothing to index/i, "No readable text was found. If this is a scanned PDF, upload a text version instead."],
  [/not a valid pdf|pdf/i, "This PDF couldn't be read. Try exporting it again or uploading the text."],
  [/utf-8|encod/i, "The file isn't saved as UTF-8 text. Re-save it as UTF-8 and upload again."],
  [/non-public|unsafe|private address|not allowed/i, "That address can't be fetched: only public web pages are allowed."],
  [/could not resolve|timed? ?out|fetch failed|connection/i, "The web page couldn't be reached. Check the address and try again."],
  [/catalog|mapping|column/i, "The CSV columns couldn't be matched. Check that it has a name column."],
  [/embedding|quota|rate limit/i, "Indexing failed for a moment on our side. Use \"Try again\"."],
];

export function plainError(error: string | null): string | null {
  if (!error) return null;
  for (const [pattern, message] of KNOWN) if (pattern.test(error)) return message;
  return "This source couldn't be processed. Use \"Try again\", or upload it in another format.";
}

export const DOCUMENT_STATUS: Record<string, string> = {
  pending: "Waiting",
  processing: "Indexing",
  ready: "Ready",
  failed: "Failed",
};

export function typeLabel(sourceType: string): string {
  return ({ pdf: "PDF", markdown: "Markdown", text: "Text", csv: "CSV", html: "HTML", url: "Web page" } as Record<string, string>)[sourceType] ?? sourceType;
}
