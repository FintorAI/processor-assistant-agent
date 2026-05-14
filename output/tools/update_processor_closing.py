"""update_processor_closing — Tool for substep 10.2: Processor Closing Update

Step 10 (STEP_10): Processor Workflow and Closing
Phase: FORM_UPDATES

For purchase loans, signing date = wire requested date = closing date.
Reads closing date from field 748 and writes to:
  - CUST50FV     — Signing Date
  - CX.WIREDATELO — Wire Requested Date
"""
# FACTORY-LOCK: true

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from ._helpers import _los, _profile, _write_fields

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "CUST50FV":     "Signing Date",
    "CX.WIREDATELO": "Wire Requested Date",
}


@tool
def update_processor_closing(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Fill the Processor Closing screen. For purchase loans, set Signing Date
    and Wire Requested Date to the closing date value (field 748).

    Call this tool during STEP_10 (Processor Workflow and Closing) as substep 10.2.
    Reads LOS: closing_date, signing_date, wire_requested_date, loan_purpose
    Flags: Closing Date Not Set (warning), Signing Date Not Set (warning)
    """
    loan_id = state.get("loan_id")
    if not loan_id:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_id in state. Run find_loan first."}),
            tool_call_id=tool_call_id,
        )]})

    logger.info(f"[UPDATE_PROCESSOR_CLOSING] Starting for loan {str(loan_id)[:8]}...")

    flags = []

    closing_date      = _los(state, "closing_date")       # field 748
    signing_date      = _los(state, "signing_date")       # CUST50FV (current value)
    wire_date         = _los(state, "wire_requested_date") # CX.WIREDATELO (current value)
    loan_purpose      = _los(state, "loan_purpose")       # field 19
    is_purchase       = (loan_purpose or "").strip().lower() == "purchase"

    writes: dict[str, str] = {}

    if is_purchase:
        if not closing_date:
            flags.append({
                "substep": "10.2",
                "title": "Closing Date Not Set",
                "severity": "warning",
                "details": "Field 748 (Closing Date) is blank — cannot populate Signing Date or Wire Requested Date.",
                "suggestion": "Set the closing date in Encompass before running this step.",
                "resolved": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        else:
            # For purchase: all three dates are the same
            writes["CUST50FV"]      = closing_date
            writes["CX.WIREDATELO"] = closing_date
    else:
        # Non-purchase: flag if signing date is blank, don't auto-fill
        if not signing_date:
            flags.append({
                "substep": "10.2",
                "title": "Signing Date Not Set",
                "severity": "warning",
                "details": f"Loan purpose is {loan_purpose!r} (not Purchase) — signing date must be set manually.",
                "suggestion": "Set the signing date on the Processor Closing screen.",
                "resolved": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    if writes:
        _write_fields(loan_id, writes, substep="10.2", flags=flags, state=state, labels=FIELD_LABELS)

    result = {
        "success": True,
        "substep": "10.2",
        "tool": "update_processor_closing",
        "loan_purpose": loan_purpose,
        "closing_date": closing_date,
        "fields_written": list(writes.keys()),
        "flags_count": len(flags),
        "message": (
            (f"Processor Closing: set Signing Date and Wire Date to {closing_date} (purchase loan)"
             if writes else
             f"Processor Closing: no writes — {'closing date blank' if is_purchase else 'non-purchase loan'}")
            + (f" with {len(flags)} flag(s)" if flags else "")
        ),
    }

    logger.info(f"[UPDATE_PROCESSOR_CLOSING] {result['message']}")

    update = {"messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)]}
    if flags:
        update["flags"] = flags

    return Command(update=update)
