"""Step 0: Data Gathering — Extended with dynamic document selection.

Originally auto-generated from field registry, then extended with:
  - Dynamic document type selection based on loan characteristics
  - eFolder GET-only flow (no POST /efolder/direct)
  - efolder_documents state population with DocRepo locations

The factory will NOT overwrite this file if it already exists.
To regenerate from scratch, delete this file first, then run `generate --all`.
"""
# FACTORY-LOCK: true

import json
import logging
import os
from datetime import datetime
from typing import Annotated, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command

logger = logging.getLogger(__name__)

# ── Config directory (output/config/) ──
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
_conditions_cache: dict | None = None
_doc_defs_cache: dict | None = None


def _load_conditions_config() -> dict:
    """Load required_docs_conditions.json (cached)."""
    global _conditions_cache
    if _conditions_cache is None:
        path = os.path.join(_CONFIG_DIR, "required_docs_conditions.json")
        with open(path) as f:
            _conditions_cache = json.load(f)
    return _conditions_cache


def _load_doc_definitions() -> dict:
    """Load required_docs.json (cached)."""
    global _doc_defs_cache
    if _doc_defs_cache is None:
        path = os.path.join(_CONFIG_DIR, "required_docs.json")
        with open(path) as f:
            _doc_defs_cache = json.load(f)
    return _doc_defs_cache


def get_doc_field_map() -> dict[str, list[str]]:
    """Build DOC_FIELD_MAP dynamically from required_docs.json.

    Returns dict: { "Credit Report": ["credit_score", "borrower_ssn", ...], ... }
    """
    defs = _load_doc_definitions()
    result: dict[str, list[str]] = {}
    for _key, doc_info in defs.get("documents", {}).items():
        doc_name = doc_info.get("name", "")
        fields = doc_info.get("fields_extracted", [])
        if doc_name and fields:
            result[doc_name] = fields
    return result


def get_required_documents_for_loan(
    loan_type: str,
    loan_purpose: str,
    borrower_count: int,
) -> tuple[list[str], dict[str, str]]:
    """Select required document types based on loan characteristics.

    Returns:
        (document_list, extraction_modes) where extraction_modes maps
        doc names to 'all' when multiple borrowers need full extraction.
    """
    conditions_cfg = _load_conditions_config()
    conditions = conditions_cfg.get("conditions", [])

    lt = (loan_type or "").strip()
    lp = (loan_purpose or "").strip()
    bc = borrower_count or 1

    # Try exact match first
    for entry in conditions:
        cond = entry.get("condition", {})
        if cond.get("fallback"):
            continue
        if (cond.get("loan_type", "").lower() == lt.lower()
                and cond.get("loan_purpose", "").lower() == lp.lower()
                and cond.get("borrower_count", 1) == bc):
            doc_list = entry.get("document_list", [])
            ext_modes = entry.get("extraction_mode", {})
            ext_modes.pop("_comment", None)
            logger.info(
                f"[DOC_SELECT] Matched: {lt}/{lp}/bc={bc} -> {len(doc_list)} docs"
            )
            return doc_list, ext_modes

    # Try match without borrower_count
    for entry in conditions:
        cond = entry.get("condition", {})
        if cond.get("fallback"):
            continue
        if (cond.get("loan_type", "").lower() == lt.lower()
                and cond.get("loan_purpose", "").lower() == lp.lower()):
            doc_list = entry.get("document_list", [])
            ext_modes = entry.get("extraction_mode", {})
            ext_modes.pop("_comment", None)
            logger.info(
                f"[DOC_SELECT] Partial match (type+purpose): {lt}/{lp} -> {len(doc_list)} docs"
            )
            return doc_list, ext_modes

    # Fallback
    for entry in conditions:
        cond = entry.get("condition", {})
        if cond.get("fallback"):
            doc_list = entry.get("document_list", [])
            logger.info(f"[DOC_SELECT] Using fallback -> {len(doc_list)} docs")
            return doc_list, {}

    logger.warning("[DOC_SELECT] No matching condition and no fallback!")
    return [], {}


def _derive_loan_characteristics(state: dict) -> tuple[str, str, int]:
    """Extract loan_type, loan_purpose, borrower_count from state."""
    los = state.get("los_fields", {})
    summary = state.get("loan_summary", {})

    # loan_type — prefer loan_summary (post-build_loan_summary), fall back to los_fields
    loan_type = ""
    if summary and summary.get("derived", {}).get("loan_type"):
        loan_type = summary["derived"]["loan_type"]
    elif los.get("loan_type", {}).get("value"):
        loan_type = los["loan_type"]["value"]

    # loan_purpose
    loan_purpose = ""
    if summary and summary.get("derived", {}).get("loan_purpose"):
        loan_purpose = summary["derived"]["loan_purpose"]
    elif los.get("loan_purpose", {}).get("value"):
        loan_purpose = los["loan_purpose"]["value"]

    # borrower_count — use coborrower_first_name as a presence signal
    borrower_count = 1
    if summary and summary.get("derived", {}).get("has_coborrower"):
        borrower_count = 2
    elif los.get("coborrower_first_name", {}).get("value"):
        borrower_count = 2

    return loan_type, loan_purpose, borrower_count

# ── Field mapping: field_id -> {key, field_name, category} ──
FIELD_MAP = {
    "1041": {"key": "property_type", "field_name": "Property Type", "category": "property"},
    "52": {"key": "borrower_marital_status", "field_name": "Borrower Marital Status", "category": "borrower_info"},
    "53": {"key": "borrower_dependents_count", "field_name": "Borrower Dependents Count", "category": "borrower_info"},
    "54": {"key": "borrower_dependent_ages", "field_name": "Borrower Dependent Ages", "category": "borrower_info"},
    "84": {"key": "coborrower_marital_status", "field_name": "Co-Borrower Marital Status", "category": "borrower_info"},
    "1068": {"key": "employment_start_date", "field_name": "Employment Start Date (Hire Date)", "category": "employment"},
    "1072": {"key": "base_monthly_income", "field_name": "Base Monthly Income", "category": "income"},
    "1073": {"key": "years_in_profession", "field_name": "Years in Profession", "category": "employment"},
    "11": {"key": "property_address", "field_name": "Property Street Address", "category": "property"},
    "1109": {"key": "loan_amount", "field_name": "Loan Amount", "category": "loan_info"},
    "1168": {"key": "credit_score", "field_name": "Credit Score (Middle)", "category": "credit"},
    "1169": {"key": "employer_name", "field_name": "Employer Name", "category": "employment"},
    "1172": {"key": "loan_type", "field_name": "Mortgage Type", "category": "loan_info"},
    # "1182": invalid field ID in Encompass batch API — removed 2026-05-14
    "12": {"key": "property_city", "field_name": "Property City", "category": "property"},
    # "1286": invalid field ID in Encompass batch API — removed 2026-05-14
    "14": {"key": "property_state", "field_name": "Property State", "category": "property"},
    "1402": {"key": "borrower_dob", "field_name": "Borrower Date of Birth", "category": "borrower_info"},
    "1480": {"key": "coborrower_cell_phone", "field_name": "Co-Borrower Cell Phone", "category": "borrower_info"},
    "1490": {"key": "borrower_cell_phone", "field_name": "Borrower Cell Phone", "category": "borrower_info"},
    # "1491": invalid field ID in Encompass batch API — removed 2026-05-14
    "15": {"key": "property_zip", "field_name": "Property ZIP", "category": "property"},
    "1544": {"key": "borrower_ethnicity", "field_name": "Borrower Ethnicity", "category": "borrower_info"},
    "172": {"key": "other_income_type", "field_name": "Other Income Type", "category": "income"},
    "173": {"key": "other_income_amount", "field_name": "Other Income Amount (Monthly)", "category": "income"},
    "1811": {"key": "occupancy", "field_name": "Occupancy", "category": "loan_info"},
    "186": {"key": "emd_amount", "field_name": "EMD Amount", "category": "assets"},
    "19": {"key": "loan_purpose", "field_name": "Loan Purpose", "category": "loan_info"},
    # "218": invalid field ID in Encompass batch API — removed 2026-05-14
    "231": {"key": "gift_amount", "field_name": "Gift Amount", "category": "assets"},
    "3": {"key": "note_rate", "field_name": "Note Rate", "category": "loan_info"},
    "33": {"key": "estate_held", "field_name": "Estate Will Be Held In", "category": "title"},
    "34": {"key": "manner_of_title", "field_name": "Manner in Which Title Will Be Held", "category": "title"},
    "35": {"key": "borrower_current_address", "field_name": "Borrower Current Street Address", "category": "borrower_info"},
    "350": {"key": "total_monthly_payments", "field_name": "Total Monthly Liabilities", "category": "liabilities"},
    "356": {"key": "appraised_value", "field_name": "Appraised / Estimated Value", "category": "collateral"},
    "364": {"key": "loan_number", "field_name": "Loan Number", "category": "loan_info"},
    "4000": {"key": "borrower_first_name", "field_name": "Borrower First Name", "category": "borrower_info"},
    "4001": {"key": "borrower_middle_name", "field_name": "Borrower Middle Name", "category": "borrower_info"},
    "4002": {"key": "borrower_last_name", "field_name": "Borrower Last Name", "category": "borrower_info"},
    "4004": {"key": "coborrower_first_name", "field_name": "Co-Borrower First Name", "category": "borrower_info"},
    "558": {"key": "owned_properties_count", "field_name": "Number of Owned Properties (REO)", "category": "assets"},
    "65": {"key": "borrower_ssn", "field_name": "Borrower SSN", "category": "borrower_info"},
    "66": {"key": "borrower_home_phone", "field_name": "Borrower Home Phone", "category": "borrower_info"},
    "732": {"key": "total_assets", "field_name": "Total Assets", "category": "assets"},
    "733": {"key": "checking_balance", "field_name": "Checking Account Balance", "category": "assets"},
    "734": {"key": "savings_balance", "field_name": "Savings Account Balance", "category": "assets"},
    "762": {"key": "lock_expires", "field_name": "Lock Expiration Date", "category": "lock"},
    "1014": {"key": "qualifying_rate", "field_name": "Qualifying Rate (Transmittal Summary)", "category": "loan_info"},
    "1553": {"key": "transmittal_project_type", "field_name": "Project Type (Transmittal Summary)", "category": "property"},
    "CX.AMI.ELIGIBILITY": {"key": "ami_eligibility", "field_name": "AMI / Affordable Loan Eligibility", "category": "grant_program"},
    "CX.AMI.PERCENTAGE": {"key": "ami_percentage", "field_name": "AMI Percentage", "category": "grant_program"},
    "CX.APPRAISAL.WAIVER": {"key": "appraisal_waiver", "field_name": "Appraisal Waiver", "category": "collateral"},
    "CX.ATTACHMENT.TYPE": {"key": "attachment_type", "field_name": "Attachment Type (Attached/Detached)", "category": "property"},
    "CX.AUS.COLLATERAL.RELIEF": {"key": "aus_collateral_relief", "field_name": "AUS Collateral Relief", "category": "aus"},
    "CX.CONDO.PROJECT.ID": {"key": "condo_project_id", "field_name": "Condo Project ID", "category": "property"},
    "CX.CONDO.PROJECT.NAME": {"key": "condo_project_name", "field_name": "Condo Project Name", "category": "property"},
    "CX.CONDO.PROJECT.TYPE": {"key": "condo_project_type", "field_name": "Condo Project Type", "category": "property"},
    "CX.DOC.TYPE": {"key": "doc_type", "field_name": "Doc Type (Wet / E-sign / Hybrid)", "category": "processor_workflow"},
    "CX.FINAL.VESTING": {"key": "final_vesting", "field_name": "Final Vesting", "category": "title"},
    "CX.FNMA.ADDITIONAL.DATA": {"key": "fnma_additional_data", "field_name": "Fannie Mae Additional Data - AMI", "category": "grant_program"},
    "CX.INVESTOR.TYPE": {"key": "investor_type", "field_name": "Investor Type (Conforming / Non-Del)", "category": "processor_workflow"},
    "CX.KM.CL.ADDITIONAL.NOTES": {"key": "cover_letter_additional_notes", "field_name": "Cover Letter - Additional Notes (pre-populated)", "category": "cover_letter"},
    "CX.KM.CL.APPRAISAL": {"key": "cover_letter_appraisal", "field_name": "Cover Letter - Appraisal (pre-populated)", "category": "cover_letter"},
    "CX.KM.CL.TITLE.COMPANY": {"key": "cover_letter_title_company", "field_name": "Cover Letter - Title Company (pre-populated)", "category": "cover_letter"},
    "CX.KM.SUBMISSION.NOTES": {"key": "submission_notes", "field_name": "Submission Notes (Cover Letter)", "category": "cover_letter"},
    "CX.LOAN.LOCKED": {"key": "loan_locked", "field_name": "Loan Locked Status", "category": "lock"},
    "CX.LOCKED.LE.PRESENT": {"key": "locked_le_present", "field_name": "Locked LE Present", "category": "disclosures"},
    "CX.MILESTONE.CURRENT": {"key": "current_milestone", "field_name": "Current Milestone", "category": "loan_info"},
    "CX.PROCESSOR.NAME": {"key": "processor_name", "field_name": "Processor Name", "category": "loan_info"},
    "CX.REALTOR.EMAIL": {"key": "realtor_email", "field_name": "Realtor Email", "category": "file_contacts"},
    "CX.REQUIRED.FIELDS.STATUS": {"key": "required_fields_status", "field_name": "Encompass Required Fields Status", "category": "submission"},
    "CUST50FV": {"key": "signing_date", "field_name": "Signing Date", "category": "closing"},
    "CX.TITLE.COMPANY.EMAIL": {"key": "title_company_email", "field_name": "Title Company Email", "category": "file_contacts"},
    "CX.TITLE.COMPANY.NAME": {"key": "title_company_name", "field_name": "Title Company Name", "category": "file_contacts"},
    "CX.VESTING.DESCRIPTION": {"key": "vesting_description", "field_name": "Vesting Description", "category": "title"},
    "CX.WIREDATELO": {"key": "wire_requested_date", "field_name": "Wire Requested Date", "category": "closing"},
    "748": {"key": "closing_date", "field_name": "Closing Date", "category": "closing"},
    # ── Borrower Contact Info ──
    "1240": {"key": "borrower_email", "field_name": "Borrower Email", "category": "borrower_info"},
    "1179": {"key": "coborrower_email", "field_name": "Co-Borrower Email", "category": "borrower_info"},
    "1715": {"key": "borrower_work_phone", "field_name": "Borrower Business/Work Phone", "category": "borrower_info"},
    "1716": {"key": "coborrower_work_phone", "field_name": "Co-Borrower Business/Work Phone", "category": "borrower_info"},
    "98": {"key": "coborrower_home_phone", "field_name": "Co-Borrower Home Phone", "category": "borrower_info"},
    "4920": {"key": "borrower_accept_sms", "field_name": "Borrower Accept Text/SMS", "category": "borrower_info"},
    "4935": {"key": "coborrower_accept_sms", "field_name": "Co-Borrower Accept Text/SMS", "category": "borrower_info"},
    "4003": {"key": "borrower_name_suffix", "field_name": "Borrower Name Suffix", "category": "borrower_info"},
    "97": {"key": "coborrower_ssn", "field_name": "Co-Borrower SSN", "category": "borrower_info"},
    "1403": {"key": "coborrower_dob", "field_name": "Co-Borrower Date of Birth", "category": "borrower_info"},
    "4114": {"key": "borrower_est_closing_date", "field_name": "Borrower Est Closing Date", "category": "borrower_info"},
    # ── Credit ──
    "67": {"key": "experian_score", "field_name": "Borrower Experian/FICO Score", "category": "credit"},
    "60": {"key": "coborrower_experian_score", "field_name": "Co-Borrower Experian/FICO Score", "category": "credit"},
    "1414": {"key": "equifax_score", "field_name": "Borrower Equifax/Beacon Score", "category": "credit"},
    "1415": {"key": "coborrower_equifax_score", "field_name": "Co-Borrower Equifax/Beacon Score", "category": "credit"},
    "1450": {"key": "transunion_score", "field_name": "Borrower TransUnion/Empirica Score", "category": "credit"},
    "1452": {"key": "coborrower_transunion_score", "field_name": "Co-Borrower TransUnion/Empirica Score", "category": "credit"},
    "300": {"key": "credit_reference_number", "field_name": "Credit Reference Number", "category": "credit"},
    "VASUMM.X23": {"key": "credit_score_decision", "field_name": "Credit Score for Decision Making", "category": "credit"},
    # ── Loan Info (Borrower Summary fields) ──
    "1264": {"key": "lender", "field_name": "Lender", "category": "loan_info"},
    "1401": {"key": "loan_program", "field_name": "Loan Program", "category": "loan_info"},
    "1785": {"key": "closing_cost_program", "field_name": "Closing Cost Program", "category": "loan_info"},
    "1051": {"key": "mers_min", "field_name": "MERS MIN", "category": "loan_info"},
    "420": {"key": "lien_position", "field_name": "Lien Position", "category": "loan_info"},
    "608": {"key": "amort_type", "field_name": "Amortization Type", "category": "loan_info"},
    "4": {"key": "loan_term_months", "field_name": "Loan Term (Months)", "category": "loan_info"},
    "325": {"key": "term_due_in_months", "field_name": "Term Due In (Months)", "category": "loan_info"},
    "3293": {"key": "undiscounted_rate", "field_name": "Undiscounted Rate", "category": "loan_info"},
    "3941": {"key": "secondary_registration", "field_name": "Secondary Registration", "category": "loan_info"},
    "432": {"key": "lock_days", "field_name": "Lock Period (# of Days)", "category": "loan_info"},
    "761": {"key": "lock_date", "field_name": "Lock Date", "category": "loan_info"},
    "2400": {"key": "rate_is_locked", "field_name": "Rate Is Locked (Y/N)", "category": "loan_info"},
    "3253": {"key": "last_rate_set_date", "field_name": "Last Rate Set Date", "category": "loan_info"},
    "3259": {"key": "rate_lock_disclosure_date", "field_name": "Rate Lock Disclosure Date", "category": "loan_info"},
    # ── Income / Payment ──
    "5": {"key": "monthly_payment", "field_name": "Monthly Payment (P&I)", "category": "income"},
    "912": {"key": "total_monthly_payment", "field_name": "Total Monthly Payment", "category": "income"},
    "736": {"key": "monthly_income", "field_name": "Monthly Income", "category": "income"},
    # ── Assets / Down Payment ──
    "136": {"key": "los_purchase_price", "field_name": "Purchase Price (LOS)", "category": "assets"},
    "1771": {"key": "down_payment_pct", "field_name": "Down Payment %", "category": "assets"},
    "1335": {"key": "down_payment_amount", "field_name": "Down Payment Amount", "category": "assets"},
    # ── Property ──
    "13": {"key": "property_county", "field_name": "Property County", "category": "property"},
    "1821": {"key": "estimated_value", "field_name": "Estimated Value", "category": "property"},
    # ── Closing ──
    "763": {"key": "est_closing_date", "field_name": "Est Closing Date", "category": "closing"},
    # ── Declarations ──
    "418": {"key": "declaration_primary_residence", "field_name": "Declaration 5a — Will Occupy as Primary Residence", "category": "declarations"},
    "403": {"key": "declaration_ownership_3yr", "field_name": "Declaration 5a(A) — Ownership Interest Past 3 Years", "category": "declarations"},
    "981": {"key": "prior_property_type", "field_name": "Declaration 5a(A)(1) — Type of Prior Property", "category": "declarations"},
    "1069": {"key": "prior_title_held", "field_name": "Declaration 5a(A)(2) — How Title Was Held (Prior Property)", "category": "declarations"},
    "1108": {"key": "coborr_ownership_3yr", "field_name": "Declaration 5a(A) — Co-Borrower Ownership Interest Past 3 Years", "category": "declarations"},
    # Note: field 1491 is invalid in the Encompass batch API — removed 2026-05-19
    "218": {"key": "rental_income", "field_name": "Rental Income", "category": "income"},
    # ── Step 01 — File Contacts ──
    "638": {"key": "seller_1_name", "field_name": "Seller 1 Name", "category": "file_contacts"},
    # ── Step 02 — Co-Borrower Name ──
    "4006": {"key": "coborrower_last_name", "field_name": "Co-Borrower Last Name", "category": "borrower_info"},
    # ── Step 03 — URLA Page 1 ──
    "16": {"key": "property_units", "field_name": "Subject Property Number of Units", "category": "property"},
    "85": {"key": "coborr_dependents_count", "field_name": "Co-Borrower Number of Dependents", "category": "borrower_info"},
    "86": {"key": "coborr_dependents_ages", "field_name": "Co-Borrower Dependents Ages", "category": "borrower_info"},
    "URLA.X1": {"key": "borrower_citizenship", "field_name": "Borrower Citizenship", "category": "borrower_info"},
    "URLA.X2": {"key": "coborrower_citizenship", "field_name": "Co-Borrower Citizenship", "category": "borrower_info"},
    "URLA.X21": {"key": "borr_language_preference", "field_name": "Borrower Language Preference", "category": "borrower_info"},
    "URLA.X22": {"key": "coborr_language_preference", "field_name": "Co-Borrower Language Preference", "category": "borrower_info"},
    "URLA.X13": {"key": "borr_military_service", "field_name": "Borrower Military Service Indicator", "category": "borrower_info"},
    "URLA.X14": {"key": "coborr_military_service", "field_name": "Co-Borrower Military Service Indicator", "category": "borrower_info"},
    "URLA.X19": {"key": "borr_military_surviving_spouse", "field_name": "Borrower Surviving Spouse", "category": "borrower_info"},
    "URLA.X20": {"key": "coborr_military_surviving_spouse", "field_name": "Co-Borrower Surviving Spouse", "category": "borrower_info"},
    "URLA.X123": {"key": "borr_military_active_duty", "field_name": "Borrower Currently Serving on Active Duty", "category": "borrower_info"},
    "URLA.X124": {"key": "borr_military_retired", "field_name": "Borrower Retired/Discharged/Separated", "category": "borrower_info"},
    "URLA.X125": {"key": "borr_military_reserve", "field_name": "Borrower Non-Activated Reserve/National Guard", "category": "borrower_info"},
    "URLA.X126": {"key": "coborr_military_active_duty", "field_name": "Co-Borrower Currently Serving on Active Duty", "category": "borrower_info"},
    "URLA.X127": {"key": "coborr_military_retired", "field_name": "Co-Borrower Retired/Discharged/Separated", "category": "borrower_info"},
    "URLA.X128": {"key": "coborr_military_reserve", "field_name": "Co-Borrower Non-Activated Reserve/National Guard", "category": "borrower_info"},
    "URLA.X265": {"key": "borr_former_addr_does_not_apply", "field_name": "Borrower Former Address Does Not Apply", "category": "borrower_info"},
    "URLA.X266": {"key": "coborr_former_addr_does_not_apply", "field_name": "Co-Borrower Former Address Does Not Apply", "category": "borrower_info"},
    "FR0126": {"key": "borr_present_addr", "field_name": "Borrower Present Street Address", "category": "borrower_info"},
    "FR0106": {"key": "borr_present_city", "field_name": "Borrower Present City", "category": "borrower_info"},
    "FR0107": {"key": "borr_present_state", "field_name": "Borrower Present State", "category": "borrower_info"},
    "FR0108": {"key": "borr_present_zip", "field_name": "Borrower Present Zip", "category": "borrower_info"},
    "FR0112": {"key": "borr_present_yrs", "field_name": "Borrower Years at Current Address", "category": "borrower_info"},
    "FR0115": {"key": "borr_housing_type", "field_name": "Borrower Current Housing Type", "category": "borrower_info"},
    "FR0116": {"key": "borr_housing_amount", "field_name": "Borrower Current Housing Expense Amount", "category": "borrower_info"},
    "FR0124": {"key": "borr_present_mos", "field_name": "Borrower Months at Current Address", "category": "borrower_info"},
    "FR0212": {"key": "coborr_present_yrs", "field_name": "Co-Borrower Years at Current Address", "category": "borrower_info"},
    "FR0224": {"key": "coborr_present_mos", "field_name": "Co-Borrower Months at Current Address", "category": "borrower_info"},
    "FR0326": {"key": "borr_former_addr", "field_name": "Borrower Former Street Address", "category": "borrower_info"},
    "FR0306": {"key": "borr_former_city", "field_name": "Borrower Former City", "category": "borrower_info"},
    "FR0307": {"key": "borr_former_state", "field_name": "Borrower Former State", "category": "borrower_info"},
    "FR0308": {"key": "borr_former_zip", "field_name": "Borrower Former Zip", "category": "borrower_info"},
    "FR0315": {"key": "borr_former_housing_type", "field_name": "Borrower Former Housing Type", "category": "borrower_info"},
    "FR0316": {"key": "borr_former_housing_amount", "field_name": "Borrower Former Housing Expense Amount", "category": "borrower_info"},
    "FR0415": {"key": "coborr_housing_type", "field_name": "Co-Borrower Current Housing Type", "category": "borrower_info"},
    "FR0416": {"key": "coborr_housing_amount", "field_name": "Co-Borrower Current Housing Expense Amount", "category": "borrower_info"},
    "FR0426": {"key": "coborr_former_addr", "field_name": "Co-Borrower Former Street Address", "category": "borrower_info"},
    "FR0406": {"key": "coborr_former_city", "field_name": "Co-Borrower Former City", "category": "borrower_info"},
    "FR0407": {"key": "coborr_former_state", "field_name": "Co-Borrower Former State", "category": "borrower_info"},
    "FR0408": {"key": "coborr_former_zip", "field_name": "Co-Borrower Former Zip", "category": "borrower_info"},
    "1819": {"key": "borr_mailing_same_as_present", "field_name": "Borrower Mailing Address Same as Present", "category": "borrower_info"},
    "1820": {"key": "coborr_mailing_same_as_present", "field_name": "Co-Borrower Mailing Address Same as Present", "category": "borrower_info"},
    # ── Step 04 — Employment / Income ──
    "FE0119": {"key": "borr_base_monthly_income", "field_name": "Borrower — Base Monthly Income (Section 1b)", "category": "income"},
    "FE0219": {"key": "coborr_base_monthly_income", "field_name": "Co-Borrower — Base Monthly Income (Section 1b)", "category": "income"},
    "URLA.X201": {"key": "borr_income_does_not_apply", "field_name": "Borrower — Employment Income Does Not Apply", "category": "employment"},
    "URLA.X202": {"key": "coborr_income_does_not_apply", "field_name": "Co-Borrower — Employment Income Does Not Apply", "category": "employment"},
    "URLA.X40": {"key": "borr_other_income_dna", "field_name": "Borrower — Other Income Does Not Apply (1e)", "category": "income"},
    "URLA.X41": {"key": "coborr_other_income_dna", "field_name": "Co-Borrower — Other Income Does Not Apply (1e)", "category": "income"},
    "BE0102": {"key": "be01_employer_name", "field_name": "Employment 1 — Employer Name", "category": "employment"},
    "BE0105": {"key": "be01_employer_city", "field_name": "Employment 1 — Employer City", "category": "employment"},
    "BE0106": {"key": "be01_employer_state", "field_name": "Employment 1 — Employer State", "category": "employment"},
    "BE0107": {"key": "be01_employer_zip", "field_name": "Employment 1 — Employer Zip", "category": "employment"},
    "BE0108": {"key": "be01_voe_is_for", "field_name": "Employment 1 — VOE Is For (Borrower / Co-Borrower)", "category": "employment"},
    "BE0109": {"key": "be01_employment_type", "field_name": "Employment 1 — Type (Current / Prior)", "category": "employment"},
    "BE0110": {"key": "be01_position_title", "field_name": "Employment 1 — Position / Title", "category": "employment"},
    "BE0113": {"key": "be01_years_in_job", "field_name": "Employment 1 — Years in This Job", "category": "employment"},
    "BE0114": {"key": "be01_date_terminated", "field_name": "Employment 1 — Date Terminated", "category": "employment"},
    "BE0116": {"key": "be01_years_in_line_of_work", "field_name": "Employment 1 — Years in Line of Work", "category": "employment"},
    "BE0117": {"key": "be01_employer_phone", "field_name": "Employment 1 — Employer Phone", "category": "employment"},
    "BE0119": {"key": "be01_monthly_base_pay", "field_name": "Employment 1 — Monthly Base Pay", "category": "employment"},
    "BE0133": {"key": "be01_months_in_job", "field_name": "Employment 1 — Months in This Job", "category": "employment"},
    "BE0151": {"key": "be01_date_hired", "field_name": "Employment 1 — Date Hired", "category": "employment"},
    "BE0152": {"key": "be01_months_in_line_of_work", "field_name": "Employment 1 — Months in Line of Work", "category": "employment"},
    "BE0158": {"key": "be01_employer_unit_type", "field_name": "Employment 1 — Unit Type", "category": "employment"},
    "BE0159": {"key": "be01_employer_unit_number", "field_name": "Employment 1 — Unit Number", "category": "employment"},
    "BE0160": {"key": "be01_employer_street", "field_name": "Employment 1 — Employer Street Address", "category": "employment"},
    "BE0180": {"key": "be01_foreign_address", "field_name": "Employment 1 — Foreign Address", "category": "employment"},
    "BE0236": {"key": "be01_authorization_printed", "field_name": "Employment 1 — Print Authorization", "category": "employment"},
    "BE0202": {"key": "be02_employer_name", "field_name": "Employment 2 — Employer Name", "category": "employment"},
    "BE0205": {"key": "be02_employer_city", "field_name": "Employment 2 — Employer City", "category": "employment"},
    "BE0206": {"key": "be02_employer_state", "field_name": "Employment 2 — Employer State", "category": "employment"},
    "BE0207": {"key": "be02_employer_zip", "field_name": "Employment 2 — Employer Zip", "category": "employment"},
    "BE0208": {"key": "be02_voe_is_for", "field_name": "Employment 2 — VOE Is For", "category": "employment"},
    "BE0209": {"key": "be02_employment_type", "field_name": "Employment 2 — Type (Current / Prior)", "category": "employment"},
    "BE0210": {"key": "be02_position_title", "field_name": "Employment 2 — Position / Title", "category": "employment"},
    "BE0213": {"key": "be02_years_in_job", "field_name": "Employment 2 — Years in This Job", "category": "employment"},
    "BE0214": {"key": "be02_date_terminated", "field_name": "Employment 2 — Date Terminated", "category": "employment"},
    "BE0216": {"key": "be02_years_in_line_of_work", "field_name": "Employment 2 — Years in Line of Work", "category": "employment"},
    "BE0217": {"key": "be02_employer_phone", "field_name": "Employment 2 — Employer Phone", "category": "employment"},
    "BE0219": {"key": "be02_monthly_base_pay", "field_name": "Employment 2 — Monthly Base Pay", "category": "employment"},
    "BE0233": {"key": "be02_months_in_job", "field_name": "Employment 2 — Months in This Job", "category": "employment"},
    "BE0251": {"key": "be02_date_hired", "field_name": "Employment 2 — Date Hired", "category": "employment"},
    "BE0252": {"key": "be02_months_in_line_of_work", "field_name": "Employment 2 — Months in Line of Work", "category": "employment"},
    "BE0258": {"key": "be02_employer_unit_type", "field_name": "Employment 2 — Unit Type", "category": "employment"},
    "BE0259": {"key": "be02_employer_unit_number", "field_name": "Employment 2 — Unit Number", "category": "employment"},
    "BE0260": {"key": "be02_employer_street", "field_name": "Employment 2 — Employer Street Address", "category": "employment"},
    "BE0280": {"key": "be02_foreign_address", "field_name": "Employment 2 — Foreign Address", "category": "employment"},
    "BE0302": {"key": "be03_employer_name", "field_name": "Employment 3 — Employer Name", "category": "employment"},
    "BE0308": {"key": "be03_voe_is_for", "field_name": "Employment 3 — VOE Is For", "category": "employment"},
    "BE0309": {"key": "be03_employment_type", "field_name": "Employment 3 — Type (Current / Prior)", "category": "employment"},
    "BE0310": {"key": "be03_position_title", "field_name": "Employment 3 — Position / Title", "category": "employment"},
    "BE0313": {"key": "be03_years_in_job", "field_name": "Employment 3 — Years in This Job", "category": "employment"},
    "BE0314": {"key": "be03_date_terminated", "field_name": "Employment 3 — Date Terminated", "category": "employment"},
    "BE0319": {"key": "be03_monthly_base_pay", "field_name": "Employment 3 — Monthly Base Pay", "category": "employment"},
    "BE0333": {"key": "be03_months_in_job", "field_name": "Employment 3 — Months in This Job", "category": "employment"},
    "BE0351": {"key": "be03_date_hired", "field_name": "Employment 3 — Date Hired", "category": "employment"},
    # ── Step 08 — Borrower Vesting ──
    "479": {"key": "marital_status", "field_name": "Borrower Marital Status (Vesting)", "category": "vesting"},
    "471": {"key": "borrower_sex", "field_name": "Borrower Sex (Male/Female)", "category": "borrower_info"},
    "478": {"key": "coborrower_sex", "field_name": "Co-Borrower Sex (Male/Female)", "category": "borrower_info"},
    "1069": {"key": "prior_title_held", "field_name": "Declaration — How Title Was Held (Prior Property)", "category": "declarations"},
    "1867": {"key": "final_vesting", "field_name": "Final Vesting", "category": "vesting"},
    "1868": {"key": "borrower_vesting_name", "field_name": "Borrower Vesting Name", "category": "vesting"},
    "1871": {"key": "borrower_vesting_type", "field_name": "Borrower Vesting Type", "category": "vesting"},
    "1872": {"key": "borrower_vesting_desc", "field_name": "Borrower Vesting Description", "category": "vesting"},
    "1873": {"key": "coborrower_vesting_name", "field_name": "Co-Borrower Vesting Name", "category": "vesting"},
    "1876": {"key": "coborrower_vesting_type", "field_name": "Co-Borrower Vesting Type", "category": "vesting"},
    "1877": {"key": "coborrower_vesting_desc", "field_name": "Co-Borrower Vesting Description", "category": "vesting"},
    "4005": {"key": "coborrower_middle_name", "field_name": "Co-Borrower Middle Name", "category": "borrower_info"},
    "Borr.OccupancyIntent": {"key": "borrower_occupancy_intent", "field_name": "Borrower Occupancy Intent", "category": "borrower_info"},
    "CoBorr.OccupancyIntent": {"key": "coborrower_occupancy_intent", "field_name": "Co-Borrower Occupancy Intent", "category": "borrower_info"},
    "CX.NBSFLAG": {"key": "nbs_flag", "field_name": "Non-Borrowing Spouse Flag", "category": "vesting"},
    "CX.NBSINFO": {"key": "nbs_info", "field_name": "Non-Borrowing Spouse Name", "category": "vesting"},
    # ── Step 10 — Processor Workflow ──
    "CX.PRODUCTTYPE": {"key": "product_type", "field_name": "Product Type", "category": "processor_workflow"},
    "CX.NONDEL.INV.APPROVAL": {"key": "non_del_inv_approval", "field_name": "Non-Del Inv. Approval", "category": "processor_workflow"},
    "CX.DOCUMENTATIONTYPE": {"key": "doc_type_submission", "field_name": "Documentation Type (Submission)", "category": "processor_workflow"},
}

ALL_FIELD_IDS = list(FIELD_MAP.keys())

# ── Doc field mapping: built dynamically from required_docs.json ──
DOC_FIELD_MAP = get_doc_field_map()

# Flat set of all expected doc field keys (for quick lookup during normalization)
ALL_DOC_FIELD_KEYS = set()
for _keys in DOC_FIELD_MAP.values():
    ALL_DOC_FIELD_KEYS.update(_keys)


def _normalize_efolder_output(
    documents: list[dict],
    field_map: dict[str, list[str]] | None = None,
    multi_copy_types: set[str] | None = None,
) -> dict:
    """Normalize efolderGet (GET /efolder) response into state['doc_fields'] format.

    The GET /efolder API returns DynamoDB records with PascalCase keys:
      DocType, Status, ExtractedFields, DocRepoLocation, etc.

    For multi-copy doc types (extraction_mode="all"), field entries include a
    ``copies`` list so that values from every copy are preserved.

    Args:
        documents: List of document dicts from efolderGet response.
        field_map: Optional override for DOC_FIELD_MAP (defaults to module-level).
        multi_copy_types: Doc types where every copy should be kept (extraction_mode="all").

    Returns:
        Dict keyed by field_key with value, source_document, confidence, all_sources,
        raw_key, and optionally ``copies`` for multi-copy doc types.
    """
    active_map = field_map or DOC_FIELD_MAP
    multi_copy = multi_copy_types or set()
    all_keys = set()
    for _keys in active_map.values():
        all_keys.update(_keys)

    doc_fields: dict = {}

    def _upsert_field(field_key: str, value, doc_type: str, confidence: float,
                      raw_key: str, copy_index: int | None):
        """Insert or update a single field entry, handling multi-copy copies list."""
        has_value = value is not None and str(value).strip() != ""
        is_multi = doc_type in multi_copy

        if field_key not in doc_fields:
            entry: dict = {
                "value": value,
                "source_document": doc_type,
                "confidence": confidence,
                "raw_key": raw_key,
                "all_sources": [doc_type],
            }
            if is_multi:
                entry["copies"] = [{
                    "value": value,
                    "source_document": doc_type,
                    "confidence": confidence,
                    "copy_index": copy_index if copy_index is not None else 0,
                }]
            doc_fields[field_key] = entry
        else:
            existing = doc_fields[field_key]
            existing["all_sources"].append(doc_type)

            if is_multi:
                if "copies" not in existing:
                    existing["copies"] = [{
                        "value": existing["value"],
                        "source_document": existing["source_document"],
                        "confidence": existing["confidence"],
                        "copy_index": 0,
                    }]
                existing["copies"].append({
                    "value": value,
                    "source_document": doc_type,
                    "confidence": confidence,
                    "copy_index": copy_index if copy_index is not None else len(existing["copies"]),
                })
                if has_value:
                    primary_empty = existing.get("value") is None or str(existing.get("value", "")).strip() == ""
                    if primary_empty:
                        existing["value"] = value
                        existing["source_document"] = doc_type
                        existing["confidence"] = confidence
                        existing["raw_key"] = raw_key
            else:
                existing_val = existing.get("value")
                existing_empty = existing_val is None or str(existing_val).strip() == ""
                if existing_empty and has_value:
                    existing["value"] = value
                    existing["source_document"] = doc_type
                    existing["confidence"] = confidence
                    existing["raw_key"] = raw_key
                elif has_value and confidence > existing.get("confidence", 0):
                    existing["value"] = value
                    existing["source_document"] = doc_type
                    existing["confidence"] = confidence
                    existing["raw_key"] = raw_key

    for doc in documents:
        doc_type = doc.get("DocType") or doc.get("doc_type", "")
        doc_status = (doc.get("Status") or doc.get("status", "")).lower()

        if doc_status not in ("completed", "stored_no_extraction", "success"):
            continue

        extracted = doc.get("ExtractedFields") or doc.get("extracted_fields", {})
        if not extracted:
            continue

        copy_index = doc.get("_copy_index")
        expected_keys = set(active_map.get(doc_type, []))

        normalized_extracted = {}
        for raw_key, raw_val in extracted.items():
            norm_key = raw_key.strip().lower().replace(" ", "_").replace("-", "_")
            normalized_extracted[norm_key] = (raw_key, raw_val)

        for expected_key in expected_keys:
            if expected_key in normalized_extracted:
                raw_key, raw_val = normalized_extracted[expected_key]
                value = raw_val if not isinstance(raw_val, dict) else raw_val.get("value", raw_val)
                confidence = raw_val.get("confidence", 1.0) if isinstance(raw_val, dict) else 1.0
                _upsert_field(expected_key, value, doc_type, confidence, raw_key, copy_index)

        for norm_key, (raw_key, raw_val) in normalized_extracted.items():
            if norm_key in all_keys:
                value = raw_val if not isinstance(raw_val, dict) else raw_val.get("value", raw_val)
                confidence = raw_val.get("confidence", 1.0) if isinstance(raw_val, dict) else 1.0
                _upsert_field(norm_key, value, doc_type, confidence, raw_key, copy_index)

    return doc_fields


@tool
def find_loan(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
    loan_number: Optional[str] = None,
    borrower_name: Optional[str] = None,
) -> Command:
    """Find the loan GUID using loan number or borrower name.

    Uses the Encompass API to search for the loan and return its GUID.
    The GUID is stored in state['loan_id'] for all subsequent tools.

    Args:
        loan_number: The loan number to search for (preferred).
        borrower_name: Borrower name to search for (fallback).
    """
    ln = loan_number or state.get("loan_number")
    bn = borrower_name or state.get("borrower_name")

    # If loan_id already in state, just confirm it
    existing_loan_id = state.get("loan_id")
    if existing_loan_id:
        logger.info(f"[FIND_LOAN] Loan ID already in state: {existing_loan_id[:8]}...")
        result = {
            "success": True,
            "loan_id": existing_loan_id,
            "loan_number": ln,
            "message": f"Loan ID already available: {existing_loan_id[:8]}...",
            "source": "state",
        }
        return Command(update={
            "loan_number": ln,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })

    if not ln and not bn:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_number or borrower_name provided"}),
            tool_call_id=tool_call_id,
        )]})

    logger.info(f"[FIND_LOAN] Searching for loan: {ln or bn}")

    try:
        # Import the encompass client from the project's client module
        try:
            from encompass_client import get_encompass_client
        except ImportError:
            from shared.field_utils import resolve_loan_id
            # Fallback: try to extract loan_id from additional_info
            additional = state.get("additional_info", {})
            if isinstance(additional, dict) and "loan_id" in additional:
                loan_id = additional["loan_id"]
                result = {
                    "success": True,
                    "loan_id": loan_id,
                    "loan_number": ln,
                    "message": f"Found loan GUID from additional_info: {loan_id[:8]}...",
                    "source": "additional_info",
                }
                return Command(update={
                    "loan_id": loan_id,
                    "loan_number": ln,
                    "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
                })
            return Command(update={"messages": [ToolMessage(
                content=json.dumps({"error": "Encompass client not available. Provide loan_id in additional_info."}),
                tool_call_id=tool_call_id,
            )]})

        client = get_encompass_client(state=state)
        if ln:
            results = client.search_loans_pipeline(loan_number=ln)
        else:
            results = client.search_loans_pipeline(borrower_name=bn)

        if not results:
            return Command(update={"messages": [ToolMessage(
                content=json.dumps({"error": f"No loan found for {ln or bn}"}),
                tool_call_id=tool_call_id,
            )]})

        loan_id = results[0] if isinstance(results[0], str) else results[0].get("loanGuid", results[0].get("id"))

        result = {
            "success": True,
            "loan_id": loan_id,
            "loan_number": ln,
            "message": f"Found loan GUID: {loan_id[:8]}...",
            "source": "encompass_search",
        }

        logger.info(f"[FIND_LOAN] Found: {loan_id[:8]}...")

        return Command(update={
            "loan_id": loan_id,
            "loan_number": ln,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })

    except Exception as e:
        logger.error(f"[FIND_LOAN] Error: {e}")
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": str(e)}),
            tool_call_id=tool_call_id,
        )]})


@tool
def fetch_los_fields(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Batch-read all 73 LOS fields from Encompass.

    Reads all field IDs in a single batch API call and organizes them into
    state['los_fields'] keyed by the internal field key.

    Each entry: {key: {value, field_id, field_name, category}}
    """
    from shared.encompass_io import read_fields

    loan_id = state.get("loan_id")
    if not loan_id:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_id in state. Run find_loan first."}),
            tool_call_id=tool_call_id,
        )]})

    logger.info(f"[FETCH_LOS] Reading {len(ALL_FIELD_IDS)} fields for loan {loan_id[:8]}...")

    try:
        raw_results = read_fields(loan_id, ALL_FIELD_IDS, context="step0_los", state=state)

        los_fields = {}
        found_count = 0
        missing_count = 0

        for field_id, value in raw_results.items():
            mapping = FIELD_MAP.get(field_id)
            if not mapping:
                continue

            key = mapping["key"]
            stripped = str(value).strip() if value is not None else ""
            has_value = value is not None and stripped != "" and stripped != "//"

            los_fields[key] = {
                "value": value if has_value else None,
                "field_id": field_id,
                "field_name": mapping["field_name"],
                "category": mapping["category"],
            }

            if has_value:
                found_count += 1
            else:
                missing_count += 1

        result = {
            "success": True,
            "fields_found": found_count,
            "fields_missing": missing_count,
            "total_fields": len(ALL_FIELD_IDS),
            "coverage_percent": round(found_count / max(len(ALL_FIELD_IDS), 1) * 100, 1),
            "message": f"Fetched {found_count}/{len(ALL_FIELD_IDS)} LOS fields ({missing_count} missing)",
        }

        logger.info(f"[FETCH_LOS] {result['message']}")

        return Command(update={
            "los_fields": los_fields,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })

    except Exception as e:
        logger.error(f"[FETCH_LOS] Error: {e}")
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": str(e)}),
            tool_call_id=tool_call_id,
        )]})


@tool
def fetch_doc_fields(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Fetch required document fields from eFolder DynamoDB cache via GET /efolder.

    1. Derives which doc types are REQUIRED based on loan characteristics
       (loan_type, loan_purpose, borrower_count) from required_docs_conditions.json.
    2. Calls GET /efolder?loanNumber=X&includeFields=true — reads DynamoDB cache.
    3. Filters response to ONLY required doc types (ignores irrelevant docs like
       FHA/VA forms on a Conventional loan).
    4. Stores required documents in state['efolder_documents'].
    5. Normalizes expected doc fields into state['doc_fields'].

    Flow: derive required docs -> GET /efolder -> filter to required -> normalize -> state
    """
    loan_number = state.get("loan_number")
    if not loan_number:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "No loan_number in state."}),
            tool_call_id=tool_call_id,
        )]})

    env = state.get("env", "Test").lower()

    # ── Derive loan characteristics for dynamic doc type selection ──
    loan_type, loan_purpose, borrower_count = _derive_loan_characteristics(state)
    required_doc_types, extraction_modes = get_required_documents_for_loan(
        loan_type, loan_purpose, borrower_count,
    )

    logger.info(
        f"[FETCH_DOCS] Loan: type={loan_type or '?'}, purpose={loan_purpose or '?'}, "
        f"borrowers={borrower_count} -> {len(required_doc_types)} required doc types"
    )
    logger.info(f"[FETCH_DOCS] Fetching docs for loan {loan_number} ({env}) via GET /efolder...")

    try:
        from shared.efolder_client import EfolderClient

        client = EfolderClient()

        # Single GET call — reads DynamoDB, returns ExtractedFields + DocRepoLocation
        get_resp = client.get_documents(loan_number, include_fields=True)

        if "error" in get_resp:
            error_msg = get_resp["error"]
            logger.error(f"[FETCH_DOCS] efolderGet error: {error_msg}")
            return Command(update={"messages": [ToolMessage(
                content=json.dumps({"error": error_msg}),
                tool_call_id=tool_call_id,
            )]})

        all_documents = get_resp.get("documents", [])
        logger.info(f"[FETCH_DOCS] efolderGet returned {len(all_documents)} total documents from DynamoDB")

        # Group ALL documents by DocType (keep every copy)
        all_docs_by_type: dict[str, list[dict]] = {}
        for doc in all_documents:
            dt = doc.get("DocType", "")
            if not dt:
                continue
            all_docs_by_type.setdefault(dt, []).append(doc)

        # Filter to ONLY required doc types — ignore irrelevant docs
        required_set = set(required_doc_types)
        docs_by_type: dict[str, list[dict]] = {
            dt: docs for dt, docs in all_docs_by_type.items() if dt in required_set
        }
        ignored_count = len(all_docs_by_type) - len(docs_by_type)

        # For "all" extraction mode keep every copy; otherwise keep best single doc
        multi_copy_types = {dt for dt, mode in extraction_modes.items() if str(mode).lower() == "all"}
        flat_docs_for_normalize: list[dict] = []
        for dt, doc_list in docs_by_type.items():
            if dt in multi_copy_types:
                for idx, d in enumerate(doc_list):
                    d["_copy_index"] = idx
                    flat_docs_for_normalize.append(d)
            else:
                best = doc_list[0]
                for d in doc_list[1:]:
                    s = (d.get("Status", "") or "").lower()
                    if s == "completed" and (best.get("Status", "") or "").lower() != "completed":
                        best = d
                flat_docs_for_normalize.append(best)

        total_docs_kept = len(flat_docs_for_normalize)
        logger.info(
            f"[FETCH_DOCS] DynamoDB: {len(all_docs_by_type)} unique types, "
            f"{len(docs_by_type)} required ({total_docs_kept} docs incl. copies), "
            f"{ignored_count} ignored"
        )

        # ── Normalize to doc_fields — only from required docs ──
        doc_fields = _normalize_efolder_output(
            flat_docs_for_normalize, multi_copy_types=multi_copy_types,
        )

        # ── Build document inventory — required docs only ──
        efolder_documents = {}
        completed_count = 0
        not_found_types = []
        pending_types = []
        failed_types = []

        for dt, doc_list in docs_by_type.items():
            is_multi = dt in multi_copy_types
            copies_info: list[dict] = []
            dt_completed = 0

            for idx, doc in enumerate(doc_list):
                status = (doc.get("Status", "") or "").lower()
                extracted = doc.get("ExtractedFields", {})

                fields_summary = {}
                for field_name, field_val in extracted.items():
                    if isinstance(field_val, dict):
                        fields_summary[field_name] = {
                            "value": field_val.get("value", field_val),
                            "confidence": field_val.get("confidence", 1.0),
                        }
                    else:
                        fields_summary[field_name] = {
                            "value": field_val,
                            "confidence": 1.0,
                        }

                copy_entry = {
                    "copy_index": idx,
                    "status": status,
                    "source": doc.get("Source", ""),
                    "document_title": doc.get("DocumentTitle", ""),
                    "attachment_id": doc.get("AttachmentID", ""),
                    "attachment_name": doc.get("AttachmentName", ""),
                    "file_size": doc.get("FileSizeBytes", 0),
                    "extracted_fields_count": doc.get("ExtractedFieldsCount", len(extracted)),
                    "extracted_fields": fields_summary,
                    "docrepo_location": doc.get("DocRepoLocation", ""),
                    "docrepo_bucket": doc.get("Bucket", ""),
                    "docrepo_client_id": doc.get("Client", ""),
                    "error": doc.get("FailureReason"),
                }
                copies_info.append(copy_entry)

                if status in ("completed", "stored_no_extraction"):
                    dt_completed += 1
                elif status == "pending" and dt not in pending_types:
                    pending_types.append(dt)
                elif status.startswith("error") and dt not in failed_types:
                    failed_types.append(dt)

                logger.info(
                    f"[FETCH_DOCS]   {dt}[{idx}]: status={status}, "
                    f"fields={len(extracted)}, "
                    f"docrepo={doc.get('DocRepoLocation', '') or 'N/A'}"
                )

            if dt_completed > 0:
                completed_count += 1

            primary = copies_info[0] if copies_info else {}
            efolder_documents[dt] = {
                "doc_type": dt,
                "copy_count": len(doc_list),
                "is_multi_copy": is_multi,
                "status": primary.get("status", "unknown") if len(copies_info) == 1 else "multiple",
                "source": primary.get("source", ""),
                "document_title": primary.get("document_title", ""),
                "attachment_id": primary.get("attachment_id", ""),
                "attachment_name": primary.get("attachment_name", ""),
                "file_size": primary.get("file_size", 0),
                "extracted_fields_count": primary.get("extracted_fields_count", 0),
                "extracted_fields": primary.get("extracted_fields", {}),
                "docrepo_location": primary.get("docrepo_location", ""),
                "docrepo_bucket": primary.get("docrepo_bucket", ""),
                "docrepo_client_id": primary.get("docrepo_client_id", ""),
                "extraction_mode": extraction_modes.get(dt, "best"),
                "error": primary.get("error"),
                "copies": copies_info,
            }

        # Mark required docs that are missing from DynamoDB
        for dt in required_doc_types:
            if dt not in docs_by_type:
                not_found_types.append(dt)
                efolder_documents[dt] = {
                    "doc_type": dt,
                    "copy_count": 0,
                    "is_multi_copy": dt in multi_copy_types,
                    "status": "not_found",
                    "source": "",
                    "document_title": "",
                    "attachment_id": "",
                    "attachment_name": "",
                    "file_size": 0,
                    "extracted_fields_count": 0,
                    "extracted_fields": {},
                    "docrepo_location": "",
                    "docrepo_bucket": "",
                    "docrepo_client_id": "",
                    "extraction_mode": extraction_modes.get(dt, "best"),
                    "error": "Not found in DynamoDB cache",
                    "copies": [],
                }
                logger.info(f"[FETCH_DOCS]   {dt}: NOT FOUND in DynamoDB")

        # Overall metadata
        first_doc_list = next(iter(docs_by_type.values()), [])
        first_doc = first_doc_list[0] if first_doc_list else {}
        efolder_documents["_meta"] = {
            "loan_number": loan_number,
            "loan_guid": first_doc.get("LoanGuid", ""),
            "loan_type": loan_type,
            "loan_purpose": loan_purpose,
            "borrower_count": borrower_count,
            "multi_copy_doc_types": sorted(list(multi_copy_types)),
            "total_in_dynamodb": len(all_docs_by_type),
            "total_required": len(required_doc_types),
            "ignored_non_required": ignored_count,
            "completed": completed_count,
            "not_found": len(not_found_types),
            "pending": len(pending_types),
            "failed": len(failed_types),
            "not_found_doc_types": not_found_types,
            "pending_doc_types": pending_types,
            "failed_doc_types": failed_types,
            "required_doc_types": required_doc_types,
            "source": "efolderGet (GET /efolder DynamoDB)",
            "retrieved_at": datetime.now().isoformat(),
        }

        # Track which expected doc fields are still missing
        expected_total = len(ALL_DOC_FIELD_KEYS)
        found_keys = set(doc_fields.keys())
        missing_keys = ALL_DOC_FIELD_KEYS - found_keys

        result = {
            "success": True,
            "loan_type": loan_type,
            "loan_purpose": loan_purpose,
            "borrower_count": borrower_count,
            "total_in_dynamodb": len(all_docs_by_type),
            "required_doc_types": len(required_doc_types),
            "ignored_non_required": ignored_count,
            "completed": completed_count,
            "not_found": len(not_found_types),
            "pending": len(pending_types),
            "fields_normalized": len(doc_fields),
            "fields_expected": expected_total,
            "fields_missing": sorted(list(missing_keys)),
            "not_found_doc_types": not_found_types,
            "pending_doc_types": pending_types,
            "message": (
                f"{completed_count}/{len(required_doc_types)} required docs found "
                f"({ignored_count} non-required ignored). "
                f"Normalized {len(doc_fields)}/{expected_total} doc fields. "
                f"Loan: {loan_type} {loan_purpose}, {borrower_count} borrower(s)."
            ),
        }

        if not_found_types:
            result["message"] += f" Missing: {', '.join(not_found_types)}."
        if pending_types:
            result["message"] += f" Pending: {', '.join(pending_types)}."

        logger.info(f"[FETCH_DOCS] {result['message']}")

        return Command(update={
            "doc_fields": doc_fields,
            "efolder_documents": efolder_documents,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })

    except Exception as e:
        logger.error(f"[FETCH_DOCS] Error: {e}")
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": str(e)}),
            tool_call_id=tool_call_id,
        )]})


# ── URLA field key → loan_summary section mapping ──
# Maps los_fields keys to their slot in the loan_summary dict.
# This is auto-generated from the field registry.
URLA_BORROWER_KEYS = [
    "borrower_first_name", "borrower_middle_name", "borrower_last_name",
    "borrower_ssn", "borrower_dob", "borrower_marital_status",
    "borrower_sex", "borrower_aka",
]
URLA_PROPERTY_KEYS = [
    "property_address", "property_city", "property_state",
    "property_zip", "property_county",
]
URLA_LOAN_TERMS_KEYS = [
    "preflight_mortgage_type", "preflight_loan_purpose",
    "preflight_loan_amount", "preflight_appraised_value",
    "preflight_ltv", "preflight_note_rate",
    "occupancy_status",
]
URLA_DATES_KEYS = [
    "closing_date", "preflight_lock_expiration",
]
URLA_VESTING_KEYS = [
    "final_vesting", "manner_held",
]
URLA_PREFLIGHT_KEYS = [
    "preflight_ctc_status", "preflight_cd_status",  # field 2305 (Clear to Close date), CX.CD.APPROVED.DATE
    "preflight_over_under",
]
URLA_CLOSING_KEYS = [
    "closing_conditions_text", "elective_insurance",
]


def _safe_get(los_fields: dict, key: str) -> str | None:
    """Safely extract a value from los_fields."""
    entry = los_fields.get(key)
    if entry and isinstance(entry, dict):
        v = entry.get("value")
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _mask_ssn(ssn: str | None) -> str | None:
    """Mask SSN to show only last 4 digits."""
    if not ssn:
        return None
    digits = ssn.replace("-", "").replace(" ", "")
    if len(digits) >= 4:
        return f"***-**-{digits[-4:]}"
    return ssn


@tool
def build_loan_summary(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Build the loan summary (URLA equivalent) from los_fields.

    Creates a categorized, human-readable snapshot of the loan stored in
    state['loan_summary']. This runs once in Step 0.4 and NEVER changes
    afterwards — it is the single source of truth for the loan profile.

    Categories: borrower, property, loan_terms, dates, vesting, preflight,
    closing, derived (has_coborrower, is_note_llc), _meta.
    """
    los = state.get("los_fields", {})

    if not los:
        return Command(update={"messages": [ToolMessage(
            content=json.dumps({"error": "los_fields is empty. Run fetch_los_fields first."}),
            tool_call_id=tool_call_id,
        )]})

    logger.info("[LOAN_SUMMARY] Building loan summary (URLA) from los_fields...")

    def _get(key):
        return _safe_get(los, key)

    # ── Borrower ──
    borrower = {}
    for k in URLA_BORROWER_KEYS:
        v = _get(k)
        if k == "borrower_ssn":
            v = _mask_ssn(v)
        if v is not None:
            borrower[k] = v

    # ── Property ──
    prop = {}
    for k in URLA_PROPERTY_KEYS:
        v = _get(k)
        if v is not None:
            prop[k] = v

    # ── Loan Terms ──
    loan_terms = {}
    for k in URLA_LOAN_TERMS_KEYS:
        v = _get(k)
        if v is not None:
            loan_terms[k] = v

    # ── Dates ──
    dates = {}
    for k in URLA_DATES_KEYS:
        v = _get(k)
        if v is not None:
            dates[k] = v

    # ── Vesting ──
    vesting = {}
    for k in URLA_VESTING_KEYS:
        v = _get(k)
        if v is not None:
            vesting[k] = v

    # ── Preflight ──
    preflight = {}
    for k in URLA_PREFLIGHT_KEYS:
        v = _get(k)
        if v is not None:
            preflight[k] = v

    # ── Closing ──
    closing = {}
    for k in URLA_CLOSING_KEYS:
        v = _get(k)
        if v is not None:
            closing[k] = v

    # ── Derived flags ──
    has_coborrower = False
    coborrower_last = _get("preflight_has_coborrower")
    if coborrower_last:
        has_coborrower = True

    # is_note_llc: check LO/processor email, lender name, or CD page 5 lender
    is_note_llc = False
    prop_state = (_get("property_state") or "").upper()
    mortgage_type = (_get("preflight_mortgage_type") or "").lower()
    lo_email = (_get("lo_email") or "").lower()
    processor_email = (_get("processor_email") or "").lower()
    lender_name = (_get("lender_name_alt") or "").lower()
    cd5_lender = (_get("cd5_lender_name") or "").lower()
    _NOTE_LLC_NAMES = ("note mortgage", "note llc", "note mortgage llc")
    if (
        "@notemortgage.com" in lo_email
        or "@notemortgage.com" in processor_email
        or any(n in lender_name for n in _NOTE_LLC_NAMES)
        or any(n in cd5_lender for n in _NOTE_LLC_NAMES)
    ):
        is_note_llc = True

    # Trust flag
    is_trust = False
    trust_flag = (_get("close_trust_flag") or "").strip().lower()
    if trust_flag in ("true", "yes", "1", "y"):
        is_trust = True

    ltv_str = _get("preflight_ltv")
    ltv = None
    if ltv_str:
        try:
            ltv = float(ltv_str)
        except (ValueError, TypeError):
            ltv = None

    derived = {
        "has_coborrower": has_coborrower,
        "is_note_llc": is_note_llc,
        "is_trust": is_trust,
        "loan_type": _get("preflight_mortgage_type"),
        "loan_purpose": _get("preflight_loan_purpose"),
        "ltv": ltv,
    }

    # ── Loan Profile (5 discriminators for rule modifiers) ──
    loan_profile = {
        "loan_type": _get("preflight_mortgage_type") or "Conventional",
        "purpose": _get("preflight_loan_purpose") or "Purchase",
        "state": prop_state,
        "trust": is_trust,
        "note_llc": is_note_llc,
    }

    # ── Coverage stats ──
    all_urla_keys = (
        URLA_BORROWER_KEYS + URLA_PROPERTY_KEYS + URLA_LOAN_TERMS_KEYS
        + URLA_DATES_KEYS + URLA_VESTING_KEYS + URLA_PREFLIGHT_KEYS
        + URLA_CLOSING_KEYS
    )
    available = sum(1 for k in all_urla_keys if _get(k) is not None)
    missing_keys = [k for k in all_urla_keys if _get(k) is None]

    loan_summary = {
        "borrower": borrower,
        "property": prop,
        "loan_terms": loan_terms,
        "dates": dates,
        "vesting": vesting,
        "preflight": preflight,
        "closing": closing,
        "derived": derived,
        "_meta": {
            "total_urla_fields": len(all_urla_keys),
            "fields_available": available,
            "fields_missing": missing_keys,
            "coverage_percent": round(available / max(len(all_urla_keys), 1) * 100, 1),
            "built_at": datetime.now().isoformat(),
            "source": "los_fields",
            "immutable": True,
        },
    }

    result = {
        "success": True,
        "coverage_percent": loan_summary["_meta"]["coverage_percent"],
        "fields_available": available,
        "fields_missing_count": len(missing_keys),
        "has_coborrower": has_coborrower,
        "loan_type": derived["loan_type"],
        "loan_purpose": derived["loan_purpose"],
        "ltv": ltv,
        "loan_profile": loan_profile,
        "message": (
            f"Loan summary (URLA) built: {available}/{len(all_urla_keys)} fields "
            f"({loan_summary['_meta']['coverage_percent']}%). "
            f"Loan: {derived['loan_type']} {derived['loan_purpose']}, "
            f"LTV: {ltv}, CoBorrower: {has_coborrower}. "
            f"Profile: type={loan_profile['loan_type']}, "
            f"purpose={loan_profile['purpose']}, state={loan_profile['state']}, "
            f"trust={loan_profile['trust']}, note_llc={loan_profile['note_llc']}"
        ),
    }

    logger.info(f"[LOAN_SUMMARY] {result['message']}")

    return Command(update={
        "loan_summary": loan_summary,
        "loan_profile": loan_profile,
        "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
    })


@tool
def validate_property_address(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Validate the subject property address via USPS API and cross-check against the Purchase Contract.

    Reads property address from state (pre-fetched by fetch_los_fields) and the Purchase Contract
    address from state (pre-fetched by fetch_doc_fields). Calls USPS Address API v3 to confirm
    the address is deliverable and returns a normalized form.

    Stores result in state['address_validation'] for use by downstream tools (e.g. review_borrower_summary).

    Substep 0.5 — run after fetch_los_fields and fetch_doc_fields.
    """
    from shared.usps_validator import validate_address_sync

    los_fields = state.get("los_fields", {})
    doc_fields = state.get("doc_fields", {})

    def _los_val(key: str) -> str:
        entry = los_fields.get(key, {})
        val = entry.get("value") if isinstance(entry, dict) else None
        return str(val).strip() if val else ""

    def _doc_val(key: str) -> str:
        entry = doc_fields.get(key, {})
        val = entry.get("value") if isinstance(entry, dict) else None
        return str(val).strip() if val else ""

    street  = _los_val("property_address")
    city    = _los_val("property_city")
    state_  = _los_val("property_state")
    zip_    = _los_val("property_zip")
    purchase_contract_address = _doc_val("purchase_property_address")

    if not street:
        result = {
            "valid": None,
            "skipped": True,
            "skip_reason": "property_address not in los_fields — fetch_los_fields may not have run yet or failed",
            "normalized": None,
            "mismatch_with_purchase_contract": None,
            "purchase_contract_address": purchase_contract_address or None,
        }
        logger.warning("[VALIDATE_ADDRESS] Skipping — property_address not available in los_fields")
        return Command(update={
            "address_validation": result,
            "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
        })

    logger.info(f"[VALIDATE_ADDRESS] Validating: {street}, {city}, {state_} {zip_}")

    try:
        usps = validate_address_sync(
            street_address=street,
            city=city or None,
            state=state_ or None,
            zip_code=zip_ or None,
        )

        normalized = None
        if usps.standardized_address:
            std = usps.standardized_address
            normalized = " ".join(filter(None, [
                std.get("street"),
                std.get("city"),
                std.get("state"),
                std.get("zip"),
            ]))

        # Cross-check against Purchase Contract address (street number match)
        mismatch = False
        if purchase_contract_address and street:
            los_num = street.strip().split()[0] if street.strip() else ""
            doc_num = purchase_contract_address.strip().split()[0] if purchase_contract_address.strip() else ""
            if los_num and doc_num and los_num != doc_num:
                mismatch = True

        result = {
            "valid": usps.success and usps.dpv_confirmation in ("Y", "S", "D"),
            "normalized": normalized,
            "dpv_confirmation": usps.dpv_confirmation,
            "error": usps.error,
            "warnings": usps.warnings or [],
            "mismatch_with_purchase_contract": mismatch,
            "purchase_contract_address": purchase_contract_address or None,
            "los_address": f"{street}, {city}, {state_} {zip_}".strip(", "),
        }

    except Exception as e:
        logger.error(f"[VALIDATE_ADDRESS] USPS call failed: {e}")
        result = {
            "valid": None,
            "error": str(e),
            "normalized": None,
            "mismatch_with_purchase_contract": None,
            "purchase_contract_address": purchase_contract_address or None,
            "los_address": f"{street}, {city}, {state_} {zip_}".strip(", "),
        }

    logger.info(f"[VALIDATE_ADDRESS] valid={result['valid']} mismatch={result['mismatch_with_purchase_contract']}")

    return Command(update={
        "address_validation": result,
        "messages": [ToolMessage(content=json.dumps(result), tool_call_id=tool_call_id)],
    })


@tool
def fetch_vod_data(
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict, InjectedState],
) -> Command:
    """Fetch Verification of Deposit (VOD) entries directly from the Encompass v3 API.

    Calls GET /encompass/v3/loans/{loanId}/applications/{applicationId}/vods and
    normalises the response into a flat list of account rows.

    Stores:
        state['vod_data']  — list of normalised account row dicts, each with:
            {
              vod_id, vod_index, institution_name, borrower_type,
              account_type, account_holder, account_number, balance
            }

    Run this after fetch_los_fields so that loan_id is available in state.
    Used by downstream tools (e.g. review_urla_assets) to cross-check extracted
    bank-statement / asset doc fields against the LOS-entered VOD amounts.
    """
    from shared.encompass_io import read_vods

    loan_id = state.get("loan_id")
    if not loan_id:
        msg = {"error": "No loan_id in state. Run find_loan first."}
        return Command(update={
            "messages": [ToolMessage(content=json.dumps(msg), tool_call_id=tool_call_id)],
        })

    vod_not_created = False
    try:
        rows = read_vods(loan_id, state=state)
    except LookupError as e:
        # "collection does not exist" — no VOD form rows in Encompass yet
        logger.warning(f"[FETCH_VOD] VOD collection missing: {e}")
        rows = []
        vod_not_created = True
    except Exception as e:
        logger.error(f"[FETCH_VOD] Failed to read VODs: {e}")
        msg = {"error": f"VOD API call failed: {e}"}
        return Command(update={
            "messages": [ToolMessage(content=json.dumps(msg), tool_call_id=tool_call_id)],
        })

    summary = {
        "vod_rows":        len(rows),
        "institutions":    list({r["institution_name"] for r in rows if r["institution_name"]}),
        "total_balance":   round(sum(r["balance"] for r in rows), 2),
        "account_types":   list({r["account_type"] for r in rows}),
        "vod_not_created": vod_not_created,
    }
    logger.info(f"[FETCH_VOD] {summary}")

    update: dict = {
        "vod_data": rows,
        "messages": [ToolMessage(
            content=json.dumps({"status": "ok", **summary}),
            tool_call_id=tool_call_id,
        )],
    }

    if vod_not_created:
        from datetime import datetime, timezone
        update["flags"] = [{
            "substep": "0.6",
            "title": "VOD Not Created in Encompass",
            "severity": "warning",
            "details": (
                "The Encompass VOD form has no rows yet — "
                "GET /applications/{id}/vods returned 'collection does not exist'. "
                "Asset balances cannot be cross-referenced until VOD entries are added."
            ),
            "suggestion": "Open the VOD form in Encompass and add entries for each depository account.",
            "resolved": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]

    return Command(update=update)
