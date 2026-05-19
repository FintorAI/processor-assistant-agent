"""review_urla_declarations — Tool for substep 6.2: Declarations (Section 5)

Step 6 (STEP_06): 1003 URLA Part 4
Phase: DATA_REVIEW

# FACTORY-LOCK: true
"""

import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from ._helpers import _los, _profile

logger = logging.getLogger(__name__)

# ── Declaration cascade field gates ──────────────────────────────────────────
# 418 = "Will you occupy as primary residence?" (Y/N)
# 403 = "Have you had ownership interest in another property in last 3 years?" (Y/N)
# 981 = "What type of property did you own?" (PrimaryResidence/FHASecondaryResidence/SecondHome/InvestmentProperty)
# 1069 = "How did you hold title?" (Sole/JointWithSpouse/JointWithOtherThanSpouse)
# 1108 = Co-borrower version of 403
_JOINT_VALUES = {"JointWithSpouse", "JointWithOtherThanSpouse"}

_PRIOR_TYPE_LABELS = {
    "PrimaryResidence":       "Primary Residence (PR)",
    "FHASecondaryResidence":  "FHA Secondary Residence (SR)",
    "SecondHome":             "Second Home (SH)",
    "InvestmentProperty":     "Investment Property (IP)",
}

_TITLE_HELD_LABELS = {
    "Sole":                       "By Yourself (S)",
    "JointWithSpouse":            "Jointly with Spouse (SP)",
    "JointWithOtherThanSpouse":   "Jointly with Other Person (O)",
}


def _yn(val) -> str | None:
    """Normalize Y/N field: return 'Y', 'N', or None if blank."""
    v = str(val or "").strip().upper()
    if v in ("Y", "YES", "TRUE", "1"):
        return "Y"
    if v in ("N", "NO", "FALSE", "0"):
        return "N"
    return None


@tool
def review_urla_declarations(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Review Section 5 Declarations (5a). Run the 418→403→(981+1069) cascade check.
    Flag warnings when sub-fields are unpopulated after a gate field is Yes, and when
    answers are inconsistent with known loan file facts (occupancy, co-borrower status).

    Call this tool during STEP_06 (1003 URLA Part 4) as substep 6.2.
    Reads LOS: declaration_primary_residence (418), declaration_ownership_3yr (403),
               prior_property_type (981), prior_title_held (1069), coborr_ownership_3yr (1108),
               occupancy (1811), coborrower_first_name, loan_purpose.
    """
    loan_id = state.get("loan_id")
    if not loan_id:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_id in state. Run find_loan first."}),
            tool_call_id=tool_call_id,
        )]})

    logger.info(f"[REVIEW_URLA_DECLARATIONS] Starting for loan {str(loan_id)[:8]}...")

    flags = []
    now = datetime.now(timezone.utc).isoformat()

    # ── Read LOS Fields ──
    decl_primary      = _los(state, "declaration_primary_residence")  # 418: Y/N
    decl_ownership    = _los(state, "declaration_ownership_3yr")      # 403: Y/N
    prior_type        = _los(state, "prior_property_type")            # 981: PR/SR/SH/IP
    prior_title       = _los(state, "prior_title_held")               # 1069: Sole/JointWith...
    coborr_ownership  = _los(state, "coborr_ownership_3yr")           # 1108: Y/N (co-borrower)

    occupancy         = _los(state, "occupancy")                      # 1811: PrimaryResidence/etc.
    coborrower_name   = _los(state, "coborrower_first_name")          # 4004: co-borrower presence check
    loan_purpose      = _los(state, "loan_purpose")                   # 19: Purchase/Refinance/etc.
    estate_held       = _los(state, "estate_held")                    # 33: FeeSimple/Leasehold

    has_coborrower = bool(coborrower_name and str(coborrower_name).strip())

    primary_418 = _yn(decl_primary)
    ownership_403 = _yn(decl_ownership)

    def _flag(title, severity, details, suggestion, resolved=False):
        flags.append({
            "substep": "6.2",
            "title": title,
            "severity": severity,
            "details": details,
            "suggestion": suggestion,
            "resolved": resolved,
            "timestamp": now,
        })

    # ────────────────────────────────────────────────────────────────────────
    # TIER 0 — Field 418: Occupancy declaration must match loan file occupancy
    # ────────────────────────────────────────────────────────────────────────
    if not primary_418:
        _flag(
            "Declaration 5a — Primary Residence Question Blank (418)",
            "warning",
            "Field 418 (Will you occupy the property as your primary residence?) is blank.",
            "Set to Yes or No based on loan occupancy type.",
        )
    else:
        occupancy_is_primary = str(occupancy or "").strip().lower() in (
            "primaryresidence", "primary residence", "primary"
        )
        if occupancy_is_primary and primary_418 == "N":
            _flag(
                "Declaration 5a — Occupancy Mismatch (418 vs 1811)",
                "warning",
                f"Loan occupancy (field 1811) = '{occupancy}' (Primary Residence) but "
                f"field 418 (Declaration A — Occupy as Primary) = 'No'.",
                "Confirm with borrower and correct field 418 or update occupancy intent.",
            )
        elif not occupancy_is_primary and primary_418 == "Y":
            _flag(
                "Declaration 5a — Occupancy Mismatch (418 vs 1811)",
                "warning",
                f"Loan occupancy (field 1811) = '{occupancy}' but "
                f"field 418 declares primary residence intent = 'Yes'.",
                "Verify occupancy type matches borrower's stated intent.",
            )
        else:
            _flag(
                "Declaration 5a — Occupancy Intent Confirmed (418)",
                "info",
                f"Field 418 = '{decl_primary}' is consistent with occupancy (1811 = '{occupancy}').",
                "No action needed.",
                resolved=True,
            )

    # ────────────────────────────────────────────────────────────────────────
    # TIER 1 — Field 403: Ownership interest past 3 years must be answered
    # ────────────────────────────────────────────────────────────────────────
    if primary_418 == "Y" and not ownership_403:
        _flag(
            "Declaration 5a(A) — Ownership Interest Question Blank (403)",
            "warning",
            "Field 418 (primary residence) = Yes, but field 403 "
            "(Had ownership interest in another property in last 3 years?) is blank.",
            "Set field 403 to Yes or No based on borrower history.",
        )

    # ────────────────────────────────────────────────────────────────────────
    # TIER 2 — Fields 981 + 1069: Required when 403 = Yes
    # ────────────────────────────────────────────────────────────────────────
    if ownership_403 == "Y":
        type_label  = _PRIOR_TYPE_LABELS.get(str(prior_type or "").strip(), prior_type)
        title_label = _TITLE_HELD_LABELS.get(str(prior_title or "").strip(), prior_title)

        # 981 — type of property must be populated
        if not prior_type or str(prior_type).strip() == "":
            _flag(
                "Declaration 5a(A)(1) — Prior Property Type Blank (981)",
                "warning",
                "Field 403 = Yes (owned property in last 3 years) but field 981 "
                "(What type of property: PR/SR/SH/IP?) is blank.",
                "Select the prior property type: Primary Residence, FHA Secondary Residence, "
                "Second Home, or Investment Property.",
            )

        # 1069 — how title was held must be populated
        if not prior_title or str(prior_title).strip() == "":
            _flag(
                "Declaration 5a(A)(2) — How Title Was Held Blank (1069)",
                "warning",
                "Field 403 = Yes (owned property in last 3 years) but field 1069 "
                "(How did you hold title: S/SP/O?) is blank.",
                "Select: By Yourself (Sole), Jointly with Spouse, or Jointly with Other Person.",
            )

        # Discrepancy: joint title but no co-borrower on application
        if prior_title and str(prior_title).strip() in _JOINT_VALUES and not has_coborrower:
            _flag(
                "Declaration 5a(A)(2) — Joint Title but No Co-Borrower (1069)",
                "warning",
                f"Field 1069 = '{title_label}' (jointly held prior property) but this is "
                f"a borrower-only application (no co-borrower). If the prior property was "
                f"held jointly with a spouse/partner, verify whether the other party has any "
                f"remaining interest or obligation on that property.",
                "Confirm with borrower. If no co-borrower is intended, verify prior joint "
                "ownership has been resolved (sale, quitclaim, etc.).",
            )

        # Discrepancy: prior property was primary but borrower is buying another primary
        if (
            str(prior_type or "").strip() == "PrimaryResidence"
            and str(occupancy or "").strip().lower() in ("primaryresidence", "primary residence", "primary")
            and str(loan_purpose or "").strip().lower() in ("purchase", "constructiononly", "constructiontopermanent")
        ):
            _flag(
                "Declaration 5a — Prior Primary Residence + New Primary Purchase",
                "info",
                f"Borrower had a prior Primary Residence (field 981 = 'Primary Residence') "
                f"and is now purchasing another Primary Residence (occupancy = '{occupancy}'). "
                f"Verify the prior primary has been or will be sold/vacated at closing.",
                "Confirm disposition of prior primary residence with borrower. "
                "If selling, ensure sale is reflected in the loan file.",
            )

        # Summary info flag when all sub-fields are populated
        if prior_type and prior_title:
            _flag(
                "Declaration 5a(A) — Prior Ownership on File",
                "info",
                f"Borrower owned a prior property in the last 3 years:\n"
                f"  • Type: {type_label}\n"
                f"  • How title was held: {title_label}",
                "Verify details match loan entity and credit report.",
                resolved=True,
            )

    elif ownership_403 == "N":
        _flag(
            "Declaration 5a(A) — No Prior Ownership in Last 3 Years",
            "info",
            "Field 403 = No — borrower had no ownership interest in another property in the last 3 years.",
            "Verify this is consistent with credit report and prior address history.",
            resolved=True,
        )

    # ────────────────────────────────────────────────────────────────────────
    # Co-borrower declarations (field 1108) — only check if co-borrower present
    # ────────────────────────────────────────────────────────────────────────
    if has_coborrower:
        coborr_403 = _yn(coborr_ownership)
        if not coborr_403:
            _flag(
                "Declaration 5a(A) — Co-Borrower Ownership Question Blank (1108)",
                "warning",
                "Co-borrower is present but field 1108 "
                "(Co-Borrower Ownership Interest Past 3 Years?) is blank.",
                "Set field 1108 to Yes or No for the co-borrower.",
            )
        elif coborr_403 == "Y":
            _flag(
                "Declaration 5a(A) — Co-Borrower Had Prior Ownership",
                "info",
                f"Co-borrower field 1108 = Yes (owned property in last 3 years). "
                f"Cross-check co-borrower prior property details with loan entity.",
                "Verify co-borrower prior property is correctly documented.",
            )

    # ── Build result ──
    result = {
        "success": True,
        "substep": "6.2",
        "tool": "review_urla_declarations",
        "flags_count": len(flags),
        "ownership_403": decl_ownership,
        "prior_type_981": prior_type,
        "prior_title_1069": prior_title,
        "message": (
            f"Declarations (Section 5) completed"
            + (f" — ownership past 3yr: {decl_ownership}, prior type: {prior_type}, title held: {prior_title}")
            + (f" — {len(flags)} flag(s)" if flags else "")
        ),
    }

    logger.info(f"[REVIEW_URLA_DECLARATIONS] {result['message']}")

    update = {
        "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
    }
    if flags:
        update["flags"] = flags

    return Command(update=update)
