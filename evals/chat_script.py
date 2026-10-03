"""Run the demo conversation script against a live API and print each reply with timings.

Manual check against a real model (never part of the test suite):

    BAP_RESTAURANT_KEY=bap_admin_... BAP_SHOP_KEY=bap_admin_... \\
        backend/.venv/bin/python evals/chat_script.py [--suite actions] [--api http://localhost:8000]

Create the admin keys with `python -m app.cli create-key --tenant demo-restaurant --kind admin`.
A turn that fails with a rate-limit error (429) is retried after a pause; a daily-quota error
stops the run. --out appends one JSON line per turn (for joining with tool_calls later).
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx

# Each suite maps a key variable to its conversations; each conversation is a list of messages.
SUITES = {
    "basics": {
        "BAP_RESTAURANT_KEY": [
            [
                "hi",
                "apnara ki ekhon khola?",
                "kacchi koto?",
                "ota ki jhal?",
                "thanks bhai",
                "Which dishes have nuts?",
                "are you a real person?",
                "amar ekta table lagbe 6 jon er, kal raat e",
                "what's the weather in Chittagong?",
                "শুক্রবার আপনারা কখন খোলেন?",
            ]
        ],
        "BAP_SHOP_KEY": [["jamdani saree ache?", "return policy ki?"]],
    },
    "actions": {
        "BAP_RESTAURANT_KEY": [
            ["Which dishes have nuts?"],
            ["500 takar niche ki ki ache?"],
            [
                "I'd like to book a table for 4 people tomorrow at 8 pm",
                "My name is Rahim Uddin, phone 01711-000111",
                "yes, please confirm",
            ],
            ["Can I book a table for 15 people tomorrow at 8 pm?"],
            ["Table for 2 at 3 am tonight please, name Rahim, phone 01711-000111"],
        ],
        "BAP_SHOP_KEY": [
            ["5000 takar niche saree ache?"],
            ["Where is my order JL-10232? The last 4 digits of my phone are 6543"],
            ["Where is my order JL-10232? The last 4 digits of my phone are 1234"],
            [
                "I want 50 sarees for a wedding, can someone call me?",
                "I'm Nusrat Jahan, 01712-345678",
                "yes",
            ],
        ],
    },
}


RATE_LIMIT_PAUSE = 65.0
DAILY_QUOTA = ("per day", "requests per day", "RPD", "TPD", "free_tier_requests", "quota")


async def run(api: str, pause: float, suite: str, only: set[str], out: str | None) -> None:
    visitor = f"chat-script-{uuid.uuid4().hex[:8]}"  # fresh per-visitor limits for each run
    model_calls = 0
    async with httpx.AsyncClient(base_url=api, timeout=180) as client:
        for env, conversations in SUITES[suite].items():
            key = os.environ.get(env)
            if not key:
                print(f"skipping {env}: not set")
                continue
            headers = {"Authorization": f"Bearer {key}"}
            turns = [
                (conv, msg)
                for index, conv in enumerate(conversations, 1)
                if not only or f"{env}:{index}" in only
                for msg in conv
            ]
            current, conversation = None, None
            for n, (conv, message) in enumerate(turns, 1):
                if conv is not current:
                    current, conversation = conv, None
                await asyncio.sleep(pause)
                for attempt in range(3):
                    result = await _turn(client, headers, visitor, conversation, message)
                    error = result["stored"]["error"] or ""
                    if "429" not in error or any(q in error for q in DAILY_QUOTA) or attempt == 2:
                        break
                    print(f"   rate limited ({error[:160]}); waiting {RATE_LIMIT_PAUSE:.0f}s")
                    await asyncio.sleep(RATE_LIMIT_PAUSE)
                stored, conversation = result["stored"], result["conversation"]
                record = stored["retrieval"] or {}
                searches = [s["query"] for s in record.get("searches", [])]
                tools = [f"{t['tool']}:{t['status']}" for t in record.get("tools", [])]
                calls = sum(1 for k in stored["timings"] if k.startswith("model_"))
                model_calls += calls
                ttft, total = result["ttft"], result["total"]
                print(f"\n{n}. {message}\n   reply: {result['reply']}")
                print(
                    f"   outcome={stored['outcome']} model={stored['model']} tools={tools} "
                    f"searches={searches} ttft={ttft and round(ttft, 2)}s "
                    f"total={round(total, 2)}s model_calls={calls} "
                    f"tokens={stored['prompt_tokens']}/{stored['completion_tokens']}"
                )
                if out:
                    line = {"env": env, "turn": n, "message": message, **result}
                    with Path(out).open("a", encoding="utf-8") as fh:  # noqa: ASYNC230 (tiny)
                        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
                if error and any(q in error for q in DAILY_QUOTA) and "429" in error:
                    print(f"   daily quota error, stopping: {error[:300]}")
                    break
    print(f"\nmodel calls that produced an answer step: {model_calls}")


async def _turn(
    client: httpx.AsyncClient, headers: dict, visitor: str, conversation: str | None, message: str
) -> dict:
    body = {
        "visitor_id": visitor,
        "message": message,
        "stream": True,
        "client_message_id": uuid.uuid4().hex,
    }
    if conversation:
        body["conversation_id"] = conversation
    start, ttft, tokens, events = time.perf_counter(), None, [], []
    async with client.stream("POST", "/v1/chat", json=body, headers=headers) as resp:
        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                fields = dict(line.split(": ", 1) for line in block.splitlines())
                data = json.loads(fields["data"])
                events.append((fields["event"], data))
                if fields["event"] == "token":
                    ttft = ttft or time.perf_counter() - start
                    tokens.append(data["text"])
    total = time.perf_counter() - start
    final = next((d for e, d in events if e in ("done", "error")), {})
    conversation = final.get("conversation_id", conversation)
    detail = (await client.get(f"/v1/conversations/{conversation}", headers=headers)).json()
    return {
        "conversation": conversation,
        "reply": "".join(tokens) or final.get("message"),
        "ttft": ttft,
        "total": total,
        "stored": detail["messages"][-1],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--pause", type=float, default=4.0, help="seconds between messages")
    parser.add_argument("--suite", choices=sorted(SUITES), default="basics")
    parser.add_argument(
        "--only", default="", help="comma-separated conversations to run, e.g. BAP_SHOP_KEY:2"
    )
    parser.add_argument("--out", help="append one JSON line per turn to this file")
    args = parser.parse_args()
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    asyncio.run(run(args.api, args.pause, args.suite, only, args.out))
