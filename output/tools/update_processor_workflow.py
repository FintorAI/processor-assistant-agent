"""update_processor_workflow — Tool for substep 10.1: Processor Workflow Update

Step 10 (STEP_10): Processor Workflow and Closing
Phase: FORM_UPDATES

Fills the Processor Workflow screen in Encompass:
  - CX.PRODUCTTYPE        — derived from loan type (Conventional → Conforming, etc.)
  - CX.DOCUMENTATIONTYPE  — Full Doc (for conventional/conforming)
  - CX.NONDEL.INV.APPROVAL — No (standard for conforming loans)

NOTE: CX.NONDEL.INV.APPROVAL field ID is unconfirmed — verify against Encompass
UI before going to production (check status bar when hovering over the field).
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

# Loan type → CX.PRODUCTTYPE value mapping
LOAN_TYPE_TO_PRODUCT: dict[str, str] = {
    "conventional": "Conforming",
    "conforming":   "Conforming",
    "fha":          "FHA",
    "va":           "VA",
    "usda":         "USDA",
    "rural housing":"USDA",
    "jumbo":        "Jumbo",
    "nonqm":        "NonQM",
    "non qm":       "NonQM",
    "non-qm":       "NonQM",
    "reverse":      "Reverse",
    "construction": "Construction",
    "bridge":       "Bridge",
    "private":      "Private",
}

FIELD_LABELS = {
    "CX.PRODUCTTYPE":         "Product Type",
    "CX.DOCUMENTATIONTYPE":   "Documentation Type",
    "CX.NONDEL.INV.APPROVAL": "Non-Del Inv. Approval",
}


def _map_product_type(loan_type: Optional[str]) -> Optional[str]:
    if not loan_type:
        return None
    return LOAN_TYPE_TO_PRODUCT.get(loan_type.strip().lower())


@tool
def update_processor_workflow(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Fill the Processor Workflow screen: set Product Type (from loan type),
    Non-Del Inv. Approval (No), and Documentation Type (Full Doc).

    Call this tool during STEP_10 (Processor Workflow and Closing) as substep 10.1.
    Reads LOS: loan_type, product_type, doc_type_submission, non_del_inv_approval
    Flags: Product Type Not Set (warning), Documentation Type Not Set (warning),
           Unknown Loan Type (warning)
    """
    loan_id = state.get("loan_id")
    if not loan_id:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_id in state. Run find_loan first."}),
            tool_call_id=tool_call_id,
        )]})

    logger.info(f"[UPDATE_PROCESSOR_WORKFLOW] Starting for loan {str(loan_id)[:8]}...")

    flags = []

    # ── Read current values ──
    loan_type           = _los(state, "loan_type")           # field 1172
    current_product     = _los(state, "product_type")        # CX.PRODUCTTYPE
    current_doc_type    = _los(state, "doc_type_submission") # CX.DOCUMENTATIONTYPE
    current_non_del     = _los(state, "non_del_inv_approval")# CX.NONDEL.INV.APPROVAL

    # ── Derive product type from loan type ──
    derived_product = _map_product_type(loan_type)

    if not derived_product:
        flags.append({
            "substep": "10.1",
            "title": "Unknown Loan Type — Product Type Not Mapped",
            "severity": "warning",
            "details": f"Loan type {loan_type!r} does not match any known product type mapping. "
                       f"Current CX.PRODUCTTYPE = {current_product!r}.",
            "suggestion": "Manually set Product Type on the Processor Workflow screen.",
            "resolved": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ── Write fields ──
    writes: dict[str, str] = {}

    if derived_product:
        writes["CX.PRODUCTTYPE"] = derived_product

    # Documentation Type — Full Doc for conventional/conforming
    # Non-QM loans may need a different value; for now always write Full Doc
    writes["CX.DOCUMENTATIONTYPE"] = "Full Doc"

    # Non-Del Inv. Approval — standard is No for conforming
    # TODO: confirm CX.NONDEL.INV.APPROVAL is the correct field ID
    writes["CX.NONDEL.INV.APPROVAL"] = "No"

    _write_fields(loan_id, writes, substep="10.1", flags=flags, state=state, labels=FIELD_LABELS)

    # ── Post-write check: flag if product type still blank ──
    if not derived_product and not current_product:
        flags.append({
            "substep": "10.1",
            "title": "Product Type Not Set",
            "severity": "warning",
            "details": "CX.PRODUCTTYPE is blank and could not be derived from loan type.",
            "suggestion": "Manually set Product Type on the Processor Workflow screen.",
            "resolved": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    result = {
        "success": True,
        "substep": "10.1",
        "tool": "update_processor_workflow",
        "loan_type": loan_type,
        "derived_product_type": derived_product,
        "fields_written": list(writes.keys()),
        "flags_count": len(flags),
        "message": (
            f"Processor Workflow: set Product={derived_product or '(unknown)'}, "
            f"DocType=Full Doc, Non-Del Inv=No"
            + (f" with {len(flags)} flag(s)" if flags else "")
        ),
    }

    logger.info(f"[UPDATE_PROCESSOR_WORKFLOW] {result['message']}")

    update = {"messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)]}
    if flags:
        update["flags"] = flags

    return Command(update=update)
