import re

from pydantic import Field, field_validator

from app.chat.tools import ToolArgs, TurnContext, WriteOutcome, WriteTool
from app.models import Lead, PendingAction
from app.tenancy import TenantDB

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LeadArgs(ToolArgs):
    name: str = Field(min_length=1, max_length=80)
    contact: str = Field(min_length=5, max_length=120, description="Phone number or email")
    interest: str = Field(
        min_length=3, max_length=500, description="What they want, e.g. '50 sarees for a wedding'"
    )

    @field_validator("contact")
    @classmethod
    def _phone_or_email(cls, value: str) -> str:
        if EMAIL.fullmatch(value):
            return value.lower()
        digits = re.sub(r"[^\d+]", "", value)
        if 7 <= len(digits.lstrip("+")) <= 15:
            return digits
        raise ValueError("contact must be a phone number or an email address")


class CaptureLeadTool(WriteTool):
    name = "capture_lead"
    args_model = LeadArgs

    def description(self, settings) -> str:
        return (
            "Save a request for the team to follow up: a callback, a quote, a bulk or catering "
            "order, or something you cannot do yourself. Needs name, contact and what they want. "
            "The first call returns the details for the customer to confirm."
        )

    def guidance(self, settings) -> str:
        promise = settings.follow_up_promise
        timing = (
            f"you may say the team usually follows up {promise}"
            if promise
            else "do not promise any time or date for the follow-up"
        )
        return (
            "- Callbacks, quotes, bulk or catering orders, or anything you cannot do: collect "
            "name, phone or email, and what they want, then call capture_lead; read the details "
            "back and call it again once they confirm. Then say the team will follow up; "
            f"{timing}."
        )

    def identity(self, args: LeadArgs) -> dict:
        return {
            "name": args.name.casefold(),
            "contact": args.contact,
            "interest": " ".join(args.interest.casefold().split()),
        }

    def read_back(self, context: TurnContext, args: LeadArgs) -> str:
        return f"name {args.name}, contact {args.contact}, request: {args.interest}"

    async def execute(
        self, context: TurnContext, db: TenantDB, args: LeadArgs, pending: PendingAction
    ) -> WriteOutcome:
        lead = Lead(
            conversation_id=context.conversation_id,
            pending_action_id=pending.id,
            name=args.name,
            contact=args.contact,
            interest=args.interest,
        )
        db.add(lead)
        await db.flush()
        return WriteOutcome(
            summary=f"SAVED: {self.read_back(context, args)}.",
            result_id=lead.id,
            event_type="lead.created",
            event_data={
                "id": str(lead.id),
                "name": args.name,
                "contact": args.contact,
                "interest": args.interest,
                "conversation_id": str(context.conversation_id),
            },
        )

    def done_message(self, outcome: WriteOutcome) -> str:
        return f"{outcome.summary} Tell the customer the team will follow up."
