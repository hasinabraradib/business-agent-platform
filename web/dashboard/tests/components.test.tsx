import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BarChart } from "@/components/BarChart";
import { MessageItem, SourceChips } from "@/components/Transcript";
import { usePolling } from "@/lib/usePolling";
import type { Message } from "@/lib/types";
import { LoginForm } from "@/app/login/LoginForm";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }), usePathname: () => "/" }));

const base: Message = {
  id: "m1",
  role: "user",
  content: "",
  citations: [],
  outcome: null,
  model: null,
  prompt_tokens: null,
  completion_tokens: null,
  timings: {},
  retrieval: null,
  error: null,
  created_at: "2026-10-04T10:00:00Z",
};

describe("transcript", () => {
  it("shows customer text as text, never as markup", () => {
    const { container } = render(
      <ol>
        <MessageItem message={{ ...base, content: '<img src=x onerror="alert(1)"><script>alert(2)</script>' }} />
      </ol>,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText(/<script>alert\(2\)<\/script>/)).toBeTruthy();
    expect(screen.getByText("Customer")).toBeTruthy();
  });

  it("labels team members and shows tools, sources and the trace link on assistant turns", () => {
    render(
      <ol>
        <MessageItem message={{ ...base, id: "s1", role: "staff", content: "Hi, this is Rumana" }} />
        <MessageItem
          message={{
            ...base,
            id: "a1",
            role: "assistant",
            content: "Kacchi is 480 taka [1].",
            outcome: "answered",
            citations: [{ marker: 1, chunk_id: "c1", document_title: "Menu", metadata: { row: 1 }, snippet: "dish: Kacchi" }],
            retrieval: { tools: [{ step: 1, tool: "search_knowledge", status: "ok", summary: "" }] },
          }}
        />
      </ol>,
    );
    expect(screen.getByText("Team member")).toBeTruthy();
    expect(screen.getByText("Menu · row 1")).toBeTruthy();
    expect(screen.getByText("search_knowledge · ok")).toBeTruthy();
    expect(screen.getByRole("link", { name: "View trace" }).getAttribute("href")).toBe("/traces/a1");
  });

  it("shows each source once", () => {
    const c = { marker: 1, chunk_id: "c1", document_title: "About", metadata: { section: "Nodi > Opening hours" }, snippet: "" };
    render(<SourceChips citations={[c, { ...c, marker: 2, chunk_id: "c1" }]} />);
    expect(screen.getAllByText("About · Opening hours")).toHaveLength(1);
  });
});

describe("bar chart", () => {
  it("writes every value out for screen readers", () => {
    render(<BarChart title="How replies ended" bars={[{ label: "Answered", value: 12 }, { label: "No answer", value: 3 }]} />);
    expect(screen.getByText("How replies ended")).toBeTruthy();
    expect(screen.getByText("12")).toBeTruthy();
    expect(screen.getByText("No answer")).toBeTruthy();
  });
});

function Poller({ fn }: { fn: () => void }) {
  usePolling(fn, 10_000);
  return null;
}

describe("polling", () => {
  afterEach(() => vi.useRealTimers());

  it("polls only while the page is visible", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    let state = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => state as DocumentVisibilityState);
    render(<Poller fn={fn} />);
    act(() => void vi.advanceTimersByTime(20_000));
    expect(fn).toHaveBeenCalledTimes(2);
    state = "hidden";
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    act(() => void vi.advanceTimersByTime(60_000));
    expect(fn).toHaveBeenCalledTimes(2);
    state = "visible";
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    expect(fn).toHaveBeenCalledTimes(3); // refreshes at once when the tab comes back
  });
});

describe("sign-in form", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the key and shows the server's plain-language error", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "That's a widget key. Sign in with an admin key." }), { status: 400 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginForm />);
    await userEvent.type(screen.getByLabelText("Admin API key"), "bap_widget_x");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toContain("widget key");
    expect(fetchMock).toHaveBeenCalledWith("/api/session", expect.objectContaining({ method: "POST" }));
    expect(window.localStorage.length).toBe(0); // the key is never stored in the browser
  });
});
