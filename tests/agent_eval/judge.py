"""LLM judge for the agent stress cases: a separate model grades each transcript against the
business's own documents and the case rubric, at temperature 0, with a strict JSON schema."""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
from harness import REPO, EvalCase

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
KNOWLEDGE_FILES = ("demo/restaurant/about.md", "demo/restaurant/menu.csv")

SYSTEM = """You grade a restaurant's customer-service chat assistant. You are strict and literal.

You get: the business's documents and settings (the ONLY facts the assistant may state), the
current date and time at the business, a test case (its purpose, expectations and rubric), and
the transcript, including every tool call the assistant made with its arguments and results.

Score each dimension from 1 (bad) to 5 (excellent):
- correctness: does the reply do what the rubric asks, with the right facts for this question?
- groundedness: every factual claim (prices, times, dishes, policies, availability, numbers,
  names) must come from the documents, the settings, the tool results or the customer's own
  words. One invented or wrong fact means groundedness of 2 or less. Saying it doesn't know is
  grounded.
- helpfulness: does it move the customer forward (answers, asks for what's missing, offers the
  contact or the team when it can't help)?
- tone: polite, warm and concise; never rude, never matching abuse, no over-apologising, in the
  customer's language when the rubric says so.

pass is true only if correctness >= 4, groundedness >= 4, helpfulness >= 3 and tone >= 3, and
nothing in the transcript contradicts the rubric. Judge only what the assistant wrote and did,
not the customer. Tool results marked NOT DONE YET mean nothing was booked yet. Facts that follow
from the settings and the current time are grounded: whether the business is open now, whether
the team is online, and when it is back. The handoff acknowledgement ("I've passed this to our
team...") is written by the platform from those settings, not invented by the assistant.
reason: one line naming the decisive problem, or what made it pass."""

SCHEMA = {
    "name": "grade",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "correctness": {"type": "integer", "minimum": 1, "maximum": 5},
            "groundedness": {"type": "integer", "minimum": 1, "maximum": 5},
            "helpfulness": {"type": "integer", "minimum": 1, "maximum": 5},
            "tone": {"type": "integer", "minimum": 1, "maximum": 5},
            "pass": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["correctness", "groundedness", "helpfulness", "tone", "pass", "reason"],
    },
}
SCORES = ("correctness", "groundedness", "helpfulness", "tone")


def knowledge_text() -> str:
    return "\n\n".join(
        f"### {name}\n{(REPO / name).read_text(encoding='utf-8')}" for name in KNOWLEDGE_FILES
    )


def transcript_text(transcript, max_result: int = 700) -> str:
    lines = []
    for i, turn in enumerate(transcript.turns, 1):
        lines.append(f"[turn {i}] CUSTOMER: {turn.say}")
        for tool in turn.tools:
            args = json.dumps(tool.get("arguments") or {}, ensure_ascii=False)
            result = (tool.get("result") or tool.get("summary") or "")[:max_result]
            lines.append(f"  TOOL {tool['tool']}({args}) -> {tool.get('status')}: {result}")
        if turn.silent:
            lines.append("  (no assistant reply: the conversation is waiting for the team)")
        elif turn.error:
            lines.append(f"  ERROR: {turn.error}")
        else:
            lines.append(
                f"  ASSISTANT [{turn.outcome}, {len(turn.citations)} citations]: {turn.reply}"
            )
        lines.append(f"  conversation status after this turn: {turn.status}")
    if transcript.stopped:
        lines.append(f"(run stopped: {transcript.stopped})")
    lines.append(
        f"Reservations created in this conversation: {transcript.facts.get('reservations')}"
    )
    return "\n".join(lines)


@dataclass
class Verdict:
    scores: dict[str, int]
    passed: bool
    reason: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Judge:
    def __init__(
        self,
        api_key: str,
        model: str,
        settings_text: str,
        now_text: str,
        client: httpx.AsyncClient | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.client = client or httpx.AsyncClient(timeout=90)
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.context = (
            f"# Business documents\n{knowledge_text()}\n\n# Business settings\n{settings_text}\n\n"
            f"# Now at the business\n{now_text}"
        )

    def messages(self, case: EvalCase, transcript) -> list[dict[str, str]]:
        expect = json.dumps(case.expect, ensure_ascii=False)
        user = (
            f"{self.context}\n\n# Test case {case.id} ({case.category})\nRubric: {case.rubric}\n"
            f"Deterministic expectations (already checked separately, for context): {expect}\n\n"
            f"# Transcript\n{transcript_text(transcript)}"
        )
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    async def grade(self, case: EvalCase, transcript) -> Verdict:
        body = {
            "model": self.model,
            "messages": self.messages(case, transcript),
            "response_format": {"type": "json_schema", "json_schema": SCHEMA},
        }
        # gpt-6-luna takes temperature 0 only with reasoning off; with reasoning on it allows
        # only the default, so the judge reasons instead (see README).
        if self.reasoning_effort and self.reasoning_effort != "none":
            body["reasoning_effort"] = self.reasoning_effort
        else:
            body["temperature"] = 0
        last_error = None
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(2**attempt * 2)  # back off on rate limits and outages
            try:
                response = await self.client.post(OPENAI_URL, json=body, headers=self.headers)
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    continue
                response.raise_for_status()
                data = response.json()
                content = json.loads(data["choices"][0]["message"]["content"])
                usage = data.get("usage") or {}
                scores = {k: int(content[k]) for k in SCORES}
                strict_pass = (
                    bool(content["pass"])
                    and scores["correctness"] >= 4
                    and (scores["groundedness"] >= 4 and min(scores.values()) >= 3)
                )
                return Verdict(
                    scores,
                    strict_pass,
                    content["reason"],
                    self.model,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return Verdict({}, False, "judge failed", self.model, error=last_error)
