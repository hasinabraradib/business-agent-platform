/**
 * Server-Sent Events over a fetch() response body. EventSource cannot send POST requests or an
 * Authorization header, so the widget reads the stream itself.
 * Follows the WHATWG event-stream format: CR, LF or CRLF line endings; "event:" and "data:"
 * fields (multiple data lines are joined with "\n"); ":" comments; a blank line dispatches.
 */

export interface SSEEvent {
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = "";
  private eventName = "";
  private data: string[] = [];

  feed(text: string): SSEEvent[] {
    this.buffer += text;
    const events: SSEEvent[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const char = this.buffer[i];
      if (char !== "\n" && char !== "\r") continue;
      if (char === "\r" && i === this.buffer.length - 1) break; // may be the first half of CRLF
      const line = this.buffer.slice(start, i);
      if (char === "\r" && this.buffer[i + 1] === "\n") i++;
      start = i + 1;
      const event = this.line(line);
      if (event) events.push(event);
    }
    this.buffer = this.buffer.slice(start);
    return events;
  }

  private line(line: string): SSEEvent | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.eventName = "";
        return null;
      }
      const event = { event: this.eventName || "message", data: this.data.join("\n") };
      this.eventName = "";
      this.data = [];
      return event;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.eventName = value;
    else if (field === "data") this.data.push(value);
    return null;
  }
}

/** Yield events from a byte stream; multi-byte characters split across chunks are kept whole. */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<SSEEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = new SSEParser();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      yield* parser.feed(decoder.decode(value, { stream: true }));
    }
    yield* parser.feed(decoder.decode());
  } finally {
    reader.releaseLock();
  }
}
