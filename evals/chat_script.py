"""Run the demo conversation script against a live API and print each reply with timings.

Manual check against a real model (never part of the test suite):

    BAP_RESTAURANT_KEY=bap_admin_... BAP_SHOP_KEY=bap_admin_... \\
        backend/.venv/bin/python evals/chat_script.py [--api http://localhost:8000]

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

SCRIPTS = {
    "BAP_RESTAURANT_KEY": [
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
    ],
    "BAP_SHOP_KEY": ["jamdani saree ache?", "return policy ki?"],
}


async def run(api: str, pause: float) -> None:
    async with httpx.AsyncClient(base_url=api, timeout=120) as client:
        for env, messages in SCRIPTS.items():
            key = os.environ.get(env)
            if not key:
                print(f"skipping {env}: not set")
                continue
            headers = {"Authorization": f"Bearer {key}"}
            conversation = None
            for n, message in enumerate(messages, 1):
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
                async with client.stream(
                    "POST", "/v1/chat", json=body, headers=headers
                ) as resp:
                    buffer = ""
                    async for chunk in resp.aiter_text():
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            fields = dict(
                                line.split(": ", 1) for line in block.splitlines()
                            )
                            data = json.loads(fields["data"])
                            events.append((fields["event"], data))
                            if fields["event"] == "token":
                                ttft = ttft or time.perf_counter() - start
                                tokens.append(data["text"])
                total = time.perf_counter() - start
                final = next((d for e, d in events if e in ("done", "error")), {})
                conversation = final.get("conversation_id", conversation)
                detail = (
                    await client.get(
                        f"/v1/conversations/{conversation}", headers=headers
                    )
                ).json()
                stored = detail["messages"][-1]
                searches = [
                    s["query"] for s in (stored["retrieval"] or {}).get("searches", [])
                ]
                print(
                    f"\n{n}. {message}\n   reply: {''.join(tokens) or final.get('message')}"
                )
                print(
                    f"   outcome={stored['outcome']} model={stored['model']} searches={searches} "
                    f"ttft={ttft and round(ttft, 2)}s total={round(total, 2)}s"
                )
                if stored["error"] and (
                    "429" in stored["error"] or "quota" in stored["error"]
                ):
                    print(f"   quota error, stopping: {stored['error'][:300]}")
                    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument(
        "--pause", type=float, default=4.0, help="seconds between messages"
    )
    args = parser.parse_args()
    asyncio.run(run(args.api, args.pause))
