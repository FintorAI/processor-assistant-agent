"""review_urla_employment — Tool for substep 4.1: Employment Verification (1b VOE)

Step 4 (STEP_04): 1003 URLA Page 2
Phase: DATA_REVIEW

# FACTORY-LOCK: true
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

from ._helpers import _los, _doc, _profile
from shared.encompass_io import read_employment

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _flag(substep: str, title: str, severity: str, details: str, suggestion: str) -> dict:
    return {
        "substep": substep,
        "title": title,
        "severity": severity,
        "details": details,
        "suggestion": suggestion,
        "resolved": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_name(v: Optional[str]) -> str:
    """Lowercase, strip punctuation for fuzzy employer name comparison."""
    if not v:
        return ""
    return re.sub(r"[^a-z0-9 ]", "", v.lower()).strip()


def _parse_amount(v: Optional[str]) -> Optional[float]:
    """Parse dollar string like '$7,500.00' or '7500' to float."""
    if not v:
        return None
    cleaned = re.sub(r"[^0-9.]", "", v)
    try:
        return float(cleaned)
    except ValueError:
        return None



def _entry_populated(entry: dict) -> bool:
    """True if at least employer_name is set."""
    return bool(entry.get("employer_name"))


# ── Main tool ─────────────────────────────────────────────────────────────────

@tool
def review_urla_employment(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Cross-check employment fields against VOE using BE01xx/BE02xx/BE03xx entries.

    For each populated employment entry:
    - Reads BE0x09 (employment type) to determine if Current or Prior.
    - Verifies LOS employment fields against the corresponding VOE doc fields
      (current_ prefix for Current employer, previous_ prefix for Prior employer).
    - Checks authorization checkbox (BE0236), date terminated logic, and
      employment duration for gap analysis.

    Call this tool during STEP_04 (1003 URLA Page 2) as substep 4.1.
    """
    loan_id = state.get("loan_id")
    if not loan_id:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_id in state. Run find_loan first."}),
            tool_call_id=tool_call_id,
        )]})

    logger.info(f"[REVIEW_URLA_EMPLOYMENT] Starting for loan {str(loan_id)[:8]}...")

    flags = []
    loan_type = _los(state, "loan_type") or ""

    # ── Fetch employment records from Encompass v3 API ─────────────────────────
    # GET /encompass/v3/loans/{loanId}/applications/{applicationId}/borrower/employment
    try:
        api_entries = read_employment(loan_id, state=state, applicant_type="borrower")
    except LookupError as e:
        # API returned "collection does not exist" — no rows created yet
        flags.append(_flag(
            "4.1",
            "VOE Form Not Populated in Encompass",
            "blocking",
            f"Encompass v3 employment API returned 'collection does not exist': {e}. "
            "No employment rows have been created in the VOE form.",
            "Open the VOE form in Encompass and add the borrower's current (and prior, if < 2 years) employer entries.",
        ))
        result = {
            "success": False,
            "substep": "4.1",
            "tool": "review_urla_employment",
            "entries_found": 0,
            "flags_count": len(flags),
            "message": "Employment Verification blocked — VOE form not populated in Encompass",
        }
        return Command(update={
            "flags": flags,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })
    except Exception as e:
        logger.error(f"[REVIEW_URLA_EMPLOYMENT] Employment API failed: {e}")
        flags.append(_flag(
            "4.1",
            "Employment API Error",
            "warning",
            f"Could not fetch employment records from Encompass v3 API: {e}",
            "Check Encompass connectivity and retry.",
        ))
        api_entries = []

    # ── Guard: empty response ─────────────────────────────────────────────────
    if not api_entries:
        flags.append(_flag(
            "4.1",
            "VOE Form Not Populated in Encompass",
            "blocking",
            "Encompass v3 employment API returned no records for this borrower.",
            "Open the VOE form in Encompass and add the borrower's current (and prior, if < 2 years) employer entries.",
        ))
        result = {
            "success": False,
            "substep": "4.1",
            "tool": "review_urla_employment",
            "entries_found": 0,
            "flags_count": len(flags),
            "message": "Employment Verification blocked — no VOE entries found",
        }
        return Command(update={
            "flags": flags,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })

    # ── Identify current and prior entries ────────────────────────────────────
    # currentEmploymentIndicator: True = Current, False = Prior
    current_entry = None
    prior_entries = []

    for e in api_entries:
        if not _entry_populated(e):
            continue
        if e.get("current"):
            if current_entry:
                flags.append(_flag("4.1",
                    "Multiple Current Employers",
                    "warning",
                    f"More than one employment record has currentEmploymentIndicator=True. "
                    f"First: '{current_entry.get('employer_name')}', also: '{e.get('employer_name')}'.",
                    "Verify only one entry should be Current; mark additional entries as Prior if applicable.",
                ))
            else:
                current_entry = e
        else:
            prior_entries.append(e)

    # Build a dict-of-entries keyed by index for backward compatibility
    entries = {f"api_{i}": e for i, e in enumerate(api_entries)}

    # ── Read VOE doc fields ───────────────────────────────────────────────────
    # We read both current_ and previous_ from the extracted VOE doc
    voe_cur_name       = _doc(state, "current_employer_name")
    voe_cur_phone      = _doc(state, "current_employer_phone")
    voe_cur_street     = _doc(state, "current_employer_street")
    voe_cur_city       = _doc(state, "current_employer_city")
    voe_cur_state      = _doc(state, "current_employer_state")
    voe_cur_zip        = _doc(state, "current_employer_zip")
    voe_cur_hire       = _doc(state, "current_original_hire_date")
    voe_cur_terminated = _doc(state, "current_date_terminated")
    voe_cur_years      = _doc(state, "current_years_in_job")
    voe_cur_months     = _doc(state, "current_months_in_job")
    voe_cur_yrs_line   = _doc(state, "current_years_in_line_of_work")
    voe_cur_mos_line   = _doc(state, "current_months_in_line_of_work")
    voe_cur_base_pay   = _doc(state, "current_monthly_base_pay")
    voe_cur_position   = _doc(state, "current_position_title")
    voe_cur_auth       = _doc(state, "authorization_printed")

    # ── Section 1b income totals + Does Not Apply checkboxes ─────────────
    borr_base_income        = _los(state, "borr_base_monthly_income")      # FE0119
    coborr_base_income      = _los(state, "coborr_base_monthly_income")    # FE0219
    borr_dna                = _los(state, "borr_income_does_not_apply")    # URLA.X201
    coborr_dna              = _los(state, "coborr_income_does_not_apply")  # URLA.X202

    voe_prev_position  = _doc(state, "previous_position_title")
    voe_prev_name      = _doc(state, "previous_employer_name")
    voe_prev_hire      = _doc(state, "previous_original_hire_date")
    voe_prev_terminated= _doc(state, "previous_date_terminated")
    voe_prev_years     = _doc(state, "previous_years_in_job")
    voe_prev_months    = _doc(state, "previous_months_in_job")
    voe_prev_base_pay  = _doc(state, "previous_monthly_base_pay")

    # ── Current employer cross-checks ─────────────────────────────────────────
    if current_entry:
        e = current_entry

        # printAttachmentIndicator — "print see attached borrower's authorization"
        raw = e.get("_raw") or {}
        print_auth = raw.get("printAttachmentIndicator")
        if print_auth is False:
            flags.append(_flag("4.1",
                "Authorization Attachment Not Checked",
                "warning",
                "printAttachmentIndicator is False — 'see attached borrower's authorization' will not print on the VOE signature line.",
                "Check the authorization attachment checkbox in Encompass for the current employer.",
            ))

        # Employer name
        if voe_cur_name:
            los_name = _normalize_name(e.get("employer_name"))
            doc_name = _normalize_name(voe_cur_name)
            if los_name and doc_name and los_name != doc_name:
                flags.append(_flag("4.1",
                    "Employer Name Mismatch (VOE vs 1003)",
                    "warning",
                    f"LOS: '{e.get('employer_name')}' | VOE: '{voe_cur_name}'",
                    "Correct the employer name in Encompass to match the VOE",
                ))
        elif not e.get("employer_name"):
            flags.append(_flag("4.1",
                "Current Employer Name Missing",
                "warning",
                "BE0102 (employer name) is empty for the Current employment entry.",
                "Enter the current employer name in Encompass",
            ))

        # Position / title / type of business (BE0110) — cross-check vs VOE
        if voe_cur_position and e.get("position_title"):
            if _normalize_name(e["position_title"]) != _normalize_name(voe_cur_position):
                flags.append(_flag("4.1",
                    "Position Title Mismatch — Current (VOE vs 1003)",
                    "warning",
                    f"LOS: '{e['position_title']}' | VOE: '{voe_cur_position}'",
                    "Correct the position/title in Encompass to match the VOE",
                ))

        # Employer phone — presence only
        if not e.get("employer_phone"):
            flags.append(_flag("4.1",
                "Current Employer Phone Missing (BE0117)",
                "warning",
                "BE0117 (employer phone) is empty for the current employer.",
                "Enter the current employer phone number in Encompass",
            ))

        # Date hired vs VOE
        if voe_cur_hire and e.get("date_hired"):
            if e["date_hired"].strip() != voe_cur_hire.strip():
                flags.append(_flag("4.1",
                    "Hire Date Mismatch — Current (VOE vs 1003)",
                    "warning",
                    f"LOS hire date: '{e['date_hired']}' | VOE original hire date: '{voe_cur_hire}'",
                    "Correct the hire date in Encompass to match the VOE",
                ))

        # Date terminated — must be null/empty for current employer
        if e.get("date_terminated"):
            flags.append(_flag("4.1",
                "Date Terminated Populated for Current Employer (BE0114)",
                "warning",
                f"BE0114 shows termination date '{e['date_terminated']}' but employment type is Current.",
                "Clear the termination date for the current employer in Encompass",
            ))

        # Monthly base pay vs VOE
        if voe_cur_base_pay or e.get("monthly_base_pay"):
            los_pay = _parse_amount(e.get("monthly_base_pay"))
            doc_pay = _parse_amount(voe_cur_base_pay)
            if los_pay is not None and doc_pay is not None:
                if abs(los_pay - doc_pay) > 1.00:  # allow $1 rounding tolerance
                    flags.append(_flag("4.1",
                        "Monthly Base Pay Mismatch — Current (VOE vs 1003)",
                        "warning",
                        f"LOS: ${los_pay:,.2f} | VOE: ${doc_pay:,.2f}",
                        "Reconcile the monthly base pay figure with the VOE",
                    ))

        # Employment duration — trigger gap check if < 2 years total
        yrs = e.get("years_in_job")
        mos = e.get("months_in_job")
        total_mos = (int(yrs or 0) * 12 + int(mos or 0)) if (yrs is not None or mos is not None) else None
        if total_mos is not None and total_mos < 24:
            if not prior_entries:
                flags.append(_flag("4.1",
                    "Employment History Gap — No Prior Employer (< 2 Years Current)",
                    "warning",
                    f"Current employment is {total_mos} months (< 2 years) and no prior employer entry found.",
                    "Add prior employment history entries in Encompass to document 2-year history",
                ))

    else:
        # No current employer found among populated entries
        if any(_entry_populated(e) for e in entries.values()):
            flags.append(_flag("4.1",
                "No Current Employer Entry Found",
                "warning",
                "Employment entries exist but none are marked as Current.",
                "Mark the current employer entry type as 'Current' in Encompass (BE0109)",
            ))

    # ── Prior employer cross-checks ───────────────────────────────────────────
    for e in prior_entries:
        # Employer name vs VOE previous
        if voe_prev_name and e.get("employer_name"):
            los_name = _normalize_name(e["employer_name"])
            doc_name = _normalize_name(voe_prev_name)
            if los_name and doc_name and los_name != doc_name:
                flags.append(_flag("4.1",
                    "Employer Name Mismatch — Prior (VOE vs 1003)",
                    "warning",
                    f"LOS prior employer: '{e['employer_name']}' | VOE previous: '{voe_prev_name}'",
                    "Correct the prior employer name in Encompass to match the VOE",
                ))

        # Date terminated must be populated for prior employer
        if not e.get("date_terminated"):
            flags.append(_flag("4.1",
                "Date Terminated Missing for Prior Employer (BE0114)",
                "warning",
                f"Employment entry for '{e.get('employer_name', 'unknown')}' is Prior but date terminated is empty.",
                "Enter the termination date for the prior employer in Encompass",
            ))

        # Monthly base pay vs VOE previous
        if voe_prev_base_pay or e.get("monthly_base_pay"):
            los_pay = _parse_amount(e.get("monthly_base_pay"))
            doc_pay = _parse_amount(voe_prev_base_pay)
            if los_pay is not None and doc_pay is not None:
                if abs(los_pay - doc_pay) > 1.00:
                    flags.append(_flag("4.1",
                        "Monthly Base Pay Mismatch — Prior (VOE vs 1003)",
                        "warning",
                        f"LOS: ${los_pay:,.2f} | VOE previous: ${doc_pay:,.2f}",
                        "Reconcile prior employer base pay with the VOE",
                    ))

    # ── Section 1b: FE0119 / FE0219 base monthly income + URLA.X201/X202 ─────────
    # Borrower
    borr_dna_checked = str(borr_dna or "").strip().lower() in ("true", "yes", "1", "checked")
    if not borr_dna_checked:
        if not borr_base_income or not borr_base_income.strip():
            flags.append(_flag("4.1",
                "Borrower Base Monthly Income Missing (FE0119)",
                "warning",
                "FE0119 (borrower base monthly income, Section 1b) is empty and URLA.X201 (does not apply) is not checked.",
                "Enter the borrower's base monthly income or check the 'Does Not Apply' box (URLA.X201)",
            ))

    # Co-borrower — only check if a co-borrower entry exists in the loan
    coborr_dna_checked = str(coborr_dna or "").strip().lower() in ("true", "yes", "1", "checked")
    # Co-borrower check: try fetching co-borrower employment from the API
    try:
        coborr_entries = read_employment(loan_id, state=state, applicant_type="coborrower")
        has_coborr = len(coborr_entries) > 0
    except LookupError:
        has_coborr = False
    except Exception:
        has_coborr = False
    if has_coborr and not coborr_dna_checked:
        if not coborr_base_income or not coborr_base_income.strip():
            flags.append(_flag("4.1",
                "Co-Borrower Base Monthly Income Missing (FE0219)",
                "warning",
                "FE0219 (co-borrower base monthly income, Section 1b) is empty and URLA.X202 (does not apply) is not checked.",
                "Enter the co-borrower's base monthly income or check the 'Does Not Apply' box (URLA.X202)",
            ))

    # ── Employment gap checks ─────────────────────────────────────────────────
    if current_entry and prior_entries:
        cur_hire = current_entry.get("date_hired")
        for prior in prior_entries:
            prior_term = prior.get("date_terminated")
            if cur_hire and prior_term:
                try:
                    fmt_options = ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"]
                    d_hire = d_term = None
                    for fmt in fmt_options:
                        try:
                            d_hire = datetime.strptime(cur_hire.strip(), fmt)
                            d_term = datetime.strptime(prior_term.strip(), fmt)
                            break
                        except ValueError:
                            continue
                    if d_hire and d_term:
                        gap_days = (d_hire - d_term).days
                        if gap_days > 30:
                            gap_months = gap_days // 30
                            if gap_months < 6:
                                if loan_type.upper() == "FHA":
                                    flags.append(_flag("4.1",
                                        "FHA Employment Gap — Explanation Required",
                                        "warning",
                                        f"Gap of ~{gap_months} month(s) between prior termination ({prior_term}) and current hire ({cur_hire}). FHA requires explanation for gaps < 6 months.",
                                        "Obtain a written explanation letter from the borrower for the employment gap",
                                    ))
                            else:
                                flags.append(_flag("4.1",
                                    "Employment Gap > 6 Months — Documentation Required",
                                    "warning",
                                    f"Gap of ~{gap_months} month(s) between prior termination ({prior_term}) and current hire ({cur_hire}). Requires documented 2-year history.",
                                    "Document the 2-year employment history before the gap and verify income continuity",
                                ))
                except Exception:
                    pass

    # ── Build result ──────────────────────────────────────────────────────────
    populated_entries = len(api_entries)
    result = {
        "success": True,
        "substep": "4.1",
        "tool": "review_urla_employment",
        "entries_found": populated_entries,
        "current_employer": current_entry.get("employer_name") if current_entry else None,
        "prior_employers": [e.get("employer_name") for e in prior_entries if e.get("employer_name")],
        "flags_count": len(flags),
        "message": (
            f"Employment Verification completed — {populated_entries} entr{'y' if populated_entries == 1 else 'ies'} reviewed"
            + (f", {len(flags)} flag(s)" if flags else ", no flags")
        ),
    }

    logger.info(f"[REVIEW_URLA_EMPLOYMENT] {result['message']}")

    update = {
        "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
    }
    if flags:
        update["flags"] = flags

    return Command(update=update)
