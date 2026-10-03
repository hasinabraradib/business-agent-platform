"""request_human: hand the conversation to a person on the business's team."""

from pydantic import Field

from app.chat.tools import Tool, ToolArgs, ToolResult, TurnContext
from app.handoff import request_handoff
from app.models import Conversation


class RequestHumanArgs(ToolArgs):
    reason: str = Field(
        min_length=3,
        max_length=300,
        description="Why, in a few words, e.g. 'asked for a person', 'upset about a late order'",
    )


class RequestHumanTool(Tool):
    name = "request_human"
    args_model = RequestHumanArgs
    rate_limit = (5, 3600)

    def description(self, settings) -> str:
        return (
            f"Hand this conversation to a person on {settings.business_name}'s team. They "
            "answer here; you stop replying. The customer is told automatically."
        )

    def guidance(self, settings) -> str:
        return (
            "- A person from the team: call request_human (reason in a few words) when the "
            "customer asks for a person, is clearly upset or angry, or wants something none of "
            "your tools can do that the team could sort out (a complaint, a refund, a special "
            "request). Don't write a reply yourself: the customer automatically gets a message "
            "that the team will answer here. For a fact you simply can't find, say so and give "
            "the contact instead. Never pretend to be the team member or claim to be human; if "
            f"they ask whether you're a bot, say you're {settings.business_name}'s virtual "
            "assistant and offer to bring in the team."
        )

    async def run(self, context: TurnContext, args: RequestHumanArgs) -> ToolResult:
        assert context.conversation_id is not None and context.now is not None
        async with context.db() as db:
            conversation = await db.get(Conversation, context.conversation_id, for_update=True)
            if conversation is None:
                return ToolResult("This conversation could not be handed over.", status="error")
            changed, delivery = await request_handoff(db, conversation, args.reason, context.now)
            await db.commit()
        if delivery is not None and context.queue is not None:
            await context.queue.enqueue_webhook(context.tenant_id, delivery)
        if changed and context.queue is not None:
            await context.queue.enqueue_staff_alert(
                context.tenant_id, context.conversation_id, "handoff"
            )
        context.handoff = True
        summary = "handed to the team" if changed else "already with the team"
        return ToolResult("Handed to the team. Write nothing more.", summary=summary)
