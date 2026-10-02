import assert from "node:assert/strict";
import { test } from "node:test";
import { readEvents, SSEParser } from "../src/sse.ts";
import { byteStream } from "./helpers.ts";

async function collect(text: string, chunkSize: number) {
  const events = [];
  for await (const event of readEvents(byteStream(text, chunkSize))) events.push(event);
  return events;
}

test("events split across network chunks are reassembled", async () => {
  const text =
    'event: token\ndata: {"text":"Hello"}\n\nevent: token\ndata: {"text":" world"}\n\n' +
    'event: done\ndata: {"message_id":"m1"}\n\n';
  for (const size of [1, 2, 5, 13, text.length]) {
    const events = await collect(text, size);
    assert.deepEqual(
      events.map((e) => e.event),
      ["token", "token", "done"],
      `chunk size ${size}`,
    );
    assert.equal(JSON.parse(events[1].data).text, " world");
  }
});

test("multi-byte Bengali characters split across chunks stay intact", async () => {
  const text = `event: token\ndata: ${JSON.stringify({ text: "শুক্রবার দুপুর ২:৩০" })}\n\n`;
  for (const size of [1, 2, 3, 4]) {
    const [event] = await collect(text, size);
    assert.equal(JSON.parse(event.data).text, "শুক্রবার দুপুর ২:৩০");
  }
});

test("multi-line data is joined with newlines; comments and CRLF are handled", () => {
  const parser = new SSEParser();
  const events = parser.feed(": keep-alive\r\nevent: token\r\ndata: line one\r\ndata:line two\r\n\r\n");
  assert.deepEqual(events, [{ event: "token", data: "line one\nline two" }]);
});

test("a CR at the end of a chunk followed by LF is one line ending", () => {
  const parser = new SSEParser();
  assert.deepEqual(parser.feed("data: a\r"), []);
  assert.deepEqual(parser.feed("\n\r"), []);
  assert.deepEqual(parser.feed("\n"), [{ event: "message", data: "a" }]);
});

test("the error event is parsed with its payload", async () => {
  const text = `event: error\ndata: ${JSON.stringify({ message: "Sorry, call 017", message_id: "m2" })}\n\n`;
  const [event] = await collect(text, 4);
  assert.equal(event.event, "error");
  assert.equal(JSON.parse(event.data).message, "Sorry, call 017");
});

test("an unterminated event at the end of the stream is discarded", async () => {
  const events = await collect('event: token\ndata: {"text":"partial"}\n', 3);
  assert.deepEqual(events, []);
});
