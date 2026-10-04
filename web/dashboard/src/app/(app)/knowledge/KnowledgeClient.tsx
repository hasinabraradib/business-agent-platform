"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Dialog } from "@/components/Dialog";
import { Badge, Button, Card, EmptyState, ErrorState, Loading, inputClass } from "@/components/ui";
import { request } from "@/lib/client";
import { formatBytes, relativeTime } from "@/lib/format";
import { ACCEPTED_TYPES, DOCUMENT_STATUS, checkFile, plainError, typeLabel } from "@/lib/ingestion";
import { CSRF_HEADER } from "@/lib/proxy";
import type { DocumentRow } from "@/lib/types";
import { usePolling } from "@/lib/usePolling";

const STATUS_TONE = { pending: "neutral", processing: "violet", ready: "accent", failed: "danger" } as const;

interface Upload {
  name: string;
  progress: number; // 0..1
  error?: string;
}

/** Upload with XMLHttpRequest, the only browser API that reports upload progress. */
function uploadFile(file: File, onProgress: (fraction: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/documents");
    xhr.setRequestHeader(CSRF_HEADER, "1");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let detail = "The upload was refused.";
      try {
        detail = JSON.parse(xhr.responseText).detail ?? detail;
      } catch {
        // keep the generic message
      }
      reject(new Error(typeof detail === "string" ? detail : "The upload was refused."));
    };
    xhr.onerror = () => reject(new Error("The upload failed. Check your connection."));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export function KnowledgeClient() {
  const [docs, setDocs] = useState<DocumentRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [dragging, setDragging] = useState(false);
  const [url, setUrl] = useState("");
  const [urlError, setUrlError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<DocumentRow | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setDocs(await request<DocumentRow[]>("documents"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  const busy = docs?.some((d) => d.status === "pending" || d.status === "processing") ?? false;
  usePolling(load, 4000, busy);

  async function addFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      const problem = checkFile(file);
      if (problem) {
        setUploads((u) => [...u, { name: file.name, progress: 0, error: problem }]);
        continue;
      }
      setUploads((u) => [...u, { name: file.name, progress: 0 }]);
      const update = (patch: Partial<Upload>) => setUploads((u) => u.map((x) => (x.name === file.name ? { ...x, ...patch } : x)));
      try {
        await uploadFile(file, (progress) => update({ progress }));
        update({ progress: 1 });
        setTimeout(() => setUploads((u) => u.filter((x) => x.name !== file.name || x.error)), 1500);
      } catch (e) {
        update({ error: `${file.name}: ${(e as Error).message}` });
      }
      await load();
    }
  }

  async function addUrl(event: React.FormEvent) {
    event.preventDefault();
    setUrlError(null);
    try {
      await request("documents/url", { method: "POST", body: JSON.stringify({ url: url.trim() }) });
      setUrl("");
      await load();
    } catch (e) {
      setUrlError((e as Error).message);
    }
  }

  async function remove(doc: DocumentRow) {
    try {
      await request(`documents/${doc.id}`, { method: "DELETE" });
      setDeleting(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
      setDeleting(null);
    }
  }

  async function retry(doc: DocumentRow) {
    try {
      await request(`documents/${doc.id}/reingest`, { method: "POST" });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            void addFiles(e.dataTransfer.files);
          }}
          className={`flex flex-col items-center justify-center gap-3 rounded-panel border-2 border-dashed px-6 py-10 text-center transition ${dragging ? "border-ink bg-accent/40" : "border-subtle-hover bg-surface"}`}
        >
          <p className="text-lg font-semibold">Drop files here</p>
          <p className="text-sm text-muted">Text, Markdown, CSV, PDF or HTML, up to 10 MB each.</p>
          <Button onClick={() => input.current?.click()}>Choose files</Button>
          <input
            ref={input}
            type="file"
            multiple
            accept={ACCEPTED_TYPES.join(",")}
            className="sr-only"
            aria-label="Choose files to upload"
            onChange={(e) => {
              if (e.target.files) void addFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </div>
        <Card as="div">
          <form onSubmit={addUrl} className="flex flex-col gap-3">
            <label htmlFor="source-url" className="font-semibold">
              Add a web page
            </label>
            <input id="source-url" type="url" required placeholder="https://example.com/menu" value={url} onChange={(e) => setUrl(e.target.value)} className={inputClass} aria-describedby={urlError ? "url-error" : undefined} />
            {urlError ? (
              <p id="url-error" role="alert" className="text-sm font-medium text-danger">
                {urlError}
              </p>
            ) : null}
            <Button type="submit" tone="secondary" disabled={!url.trim()}>
              Add page
            </Button>
          </form>
        </Card>
      </div>
      {uploads.length ? (
        <ul aria-label="Uploads" className="flex flex-col gap-2">
          {uploads.map((u) => (
            <li key={u.name} className="rounded-2xl bg-surface px-4 py-3 shadow-soft">
              {u.error ? (
                <div className="flex items-center justify-between gap-3">
                  <p role="alert" className="text-sm font-medium text-danger">
                    {u.error}
                  </p>
                  <Button size="sm" tone="ghost" onClick={() => setUploads((all) => all.filter((x) => x !== u))}>
                    Dismiss
                  </Button>
                </div>
              ) : (
                <div className="flex items-center gap-3">
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">{u.name}</span>
                  <progress value={u.progress} max={1} aria-label={`Uploading ${u.name}`} className="h-2 w-40 accent-[#111]" />
                  <span className="w-20 text-right text-xs text-muted">{u.progress >= 1 ? "Indexing…" : `${Math.round(u.progress * 100)}%`}</span>
                </div>
              )}
            </li>
          ))}
        </ul>
      ) : null}
      {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}
      {docs === null && !error ? <Loading label="Loading your sources" /> : null}
      {docs && docs.length === 0 ? (
        <EmptyState title="No knowledge yet">Upload your menu, price list or policies, or add a page from your website. The assistant answers only from what is here.</EmptyState>
      ) : null}
      {docs && docs.length > 0 ? (
        <Card as="div" className="overflow-x-auto p-0">
          <table className="w-full min-w-[44rem] text-left text-sm">
            <caption className="sr-only">Knowledge sources</caption>
            <thead className="text-xs uppercase tracking-wide text-muted">
              <tr>
                <th scope="col" className="px-5 py-3">Source</th>
                <th scope="col" className="px-3 py-3">Type</th>
                <th scope="col" className="px-3 py-3 text-right">Size</th>
                <th scope="col" className="px-3 py-3 text-right">Chunks</th>
                <th scope="col" className="px-3 py-3">Status</th>
                <th scope="col" className="px-3 py-3">Updated</th>
                <th scope="col" className="px-5 py-3"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {docs.map((doc) => (
                <tr key={doc.id} className="align-top">
                  <td className="px-5 py-3.5">
                    <p className="font-semibold">{doc.title}</p>
                    {doc.source_uri && doc.source_uri !== doc.title ? <p className="max-w-xs truncate text-xs text-muted">{doc.source_uri}</p> : null}
                    {doc.status === "failed" ? (
                      <p role="note" className="mt-1 max-w-sm text-xs font-medium text-danger">
                        {plainError(doc.error)}
                      </p>
                    ) : null}
                  </td>
                  <td className="px-3 py-3.5">{typeLabel(doc.source_type)}</td>
                  <td className="px-3 py-3.5 text-right">{formatBytes(doc.size_bytes)}</td>
                  <td className="px-3 py-3.5 text-right">{doc.chunk_count}</td>
                  <td className="px-3 py-3.5">
                    <Badge tone={STATUS_TONE[doc.status]}>{DOCUMENT_STATUS[doc.status]}</Badge>
                  </td>
                  <td className="px-3 py-3.5 text-muted">{relativeTime(doc.updated_at)}</td>
                  <td className="px-5 py-3.5">
                    <div className="flex justify-end gap-2">
                      {doc.status === "failed" ? (
                        <Button size="sm" tone="secondary" onClick={() => void retry(doc)}>
                          Try again
                        </Button>
                      ) : null}
                      <Button size="sm" tone="danger" onClick={() => setDeleting(doc)} aria-label={`Delete ${doc.title}`}>
                        Delete
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      ) : null}
      <Dialog open={deleting !== null} title="Delete this source?" onClose={() => setDeleting(null)}>
        <p>
          <strong>{deleting?.title}</strong> will be removed, and the assistant will stop using it. This can&apos;t be undone.
        </p>
        <div className="flex justify-end gap-2">
          <Button tone="secondary" onClick={() => setDeleting(null)}>
            Cancel
          </Button>
          <Button tone="danger" onClick={() => deleting && void remove(deleting)}>
            Delete source
          </Button>
        </div>
      </Dialog>
    </div>
  );
}
