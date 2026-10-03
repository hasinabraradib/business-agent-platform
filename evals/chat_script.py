"""Run the demo conversation script against a live API and print each reply with timings.

Manual check against a real model (never part of the test suite):

    BAP_RESTAURANT_KEY=bap_admin_... BAP_SHOP_KEY=bap_admin_... \\
        backend/.venv/bin/python evals/chat_script.py [--suite actions] [--api http://localhost:8000]

Create the admin keys with `python -m app.cli create-key --tenant demo-restaurant --kind admin`.
Stops at the first quota (429) error instead of retrying.
"""

import argparse
import asyncio
import json
import os
import time
import uuid

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


async def run(api: str, pause: float, suite: str) -> None:
    async with httpx.AsyncClient(base_url=api, timeout=120) as client:
        for env, conversations in SUITES[suite].items():
            key = os.environ.get(env)
            if not key:
                print(f"skipping {env}: not set")
                continue
            headers = {"Authorization": f"Bearer {key}"}
            turns = [(conv, msg) for conv in conversations for msg in conv]
            current, conversation = None, None
            for n, (conv, message) in enumerate(turns, 1):
                if conv is not current:
                    current, conversation = conv, None
                await asyncio.sleep(pause)
                body = {
                    "visitor_id": "chat-script",
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
                detail = (
                    await client.get(f"/v1/conversations/{conversation}", headers=headers)
                ).json()
                stored = detail["messages"][-1]
                record = stored["retrieval"] or {}
                searches = [s["query"] for s in record.get("searches", [])]
                tools = [f"{t['tool']}:{t['status']}" for t in record.get("tools", [])]
                print(f"\n{n}. {message}\n   reply: {''.join(tokens) or final.get('message')}")
                print(
                    f"   outcome={stored['outcome']} model={stored['model']} tools={tools} "
                    f"searches={searches} "
                    f"ttft={ttft and round(ttft, 2)}s total={round(total, 2)}s"
                )
                if stored["error"] and ("429" in stored["error"] or "quota" in stored["error"]):
                    print(f"   quota error, stopping: {stored['error'][:300]}")
                    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--pause", type=float, default=4.0, help="seconds between messages")
    parser.add_argument("--suite", choices=sorted(SUITES), default="basics")
    args = parser.parse_args()
    asyncio.run(run(args.api, args.pause, args.suite))
