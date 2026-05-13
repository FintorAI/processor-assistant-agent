"""Encompass I/O operations using EncompassConnect.

This module provides read/write operations for Encompass loan fields
using the EncompassConnect client which handles authentication and
automatic token refresh.

"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# ── Field-writes ledger ──────────────────────────────────────────────
# Module-level accumulator that write_fields() appends to on every
# successful call (real or dry-run).  Tools drain it via
# flush_field_writes_ledger() before returning their Command(update=...).
_FIELD_WRITES_LEDGER: List[Dict[str, Any]] = []


def flush_field_writes_ledger() -> List[Dict[str, Any]]:
    """Drain and return all accumulated field-write entries since the last flush."""
    global _FIELD_WRITES_LEDGER
    entries = _FIELD_WRITES_LEDGER[:]
    _FIELD_WRITES_LEDGER = []
    return entries


def get_field_writes_count() -> int:
    """Return the number of writes accumulated since the last flush."""
    return len(_FIELD_WRITES_LEDGER)


def _sync_state_cache(updates: Dict[str, Any], state: Optional[dict]) -> None:
    """Propagate written values back into state['los_fields'] so downstream
    tools see the updated value instead of the stale pre-write cache."""
    if not state or not updates:
        return
    los = state.get("los_fields")
    if not isinstance(los, dict):
        return

    fid_to_key: Dict[str, str] = {}
    for key, entry in los.items():
        if isinstance(entry, dict) and "field_id" in entry:
            fid_to_key[str(entry["field_id"]).strip()] = key

    for fid, val in updates.items():
        key = fid_to_key.get(str(fid).strip())
        if key is not None:
            los[key]["value"] = val
            logger.debug(f"State cache synced: los_fields[{key!r}] (field {fid}) = {val!r}")


def _record_writes(updates: Dict[str, Any], state: Optional[dict], dry_run: bool) -> None:
    """Append each field in *updates* to the module-level ledger."""
    substep = (state or {}).get("current_substep", "?")
    ts = datetime.now(timezone.utc).isoformat()
    for fid, val in updates.items():
        _FIELD_WRITES_LEDGER.append({
            "field_id": fid,
            "value": val,
            "substep": substep,
            "dry_run": dry_run,
            "timestamp": ts,
        })
    _sync_state_cache(updates, state)

try:
    from encompass_client import get_encompass_client
except ImportError:
    get_encompass_client = None
    logger.warning("encompass_client not available — Encompass I/O will fail at runtime")


def read_field(loan_id: str, field_id: str, context: str = "", state: dict = None) -> Optional[Any]:
    """Read a single field from a loan.
    
    Args:
        loan_id: Encompass loan GUID
        field_id: Field ID to read
        context: Optional logging context
        state: Optional state dict to determine environment
        
    Returns:
        Field value, or None if empty/not found
    """
    result = read_fields(loan_id, [field_id], context, state=state)
    return result.get(field_id)


def read_fields(loan_id: str, field_ids: List[str], context: str = "", state: dict = None) -> Dict[str, Any]:
    """Read multiple fields from a loan using EncompassConnect.
    
    Uses EncompassConnect.get_field() which handles:
    - Automatic token refresh on 401 errors
    - Credential-based re-authentication
    
    Args:
        loan_id: Encompass loan GUID
        field_ids: List of field IDs to read
        context: Optional logging context
        state: Optional state dict to determine environment
        
    Returns:
        Dictionary mapping field_id to value (None if empty)
    """
    if not field_ids:
        return {}
    
    ctx = f"{context} " if context else ""
    logger.debug(f"{ctx}Reading {len(field_ids)} fields from loan {loan_id[:8]}...")
    
    try:
        client = get_encompass_client(state=state)
        result = client.get_field(loan_id, field_ids)
        
        # Normalize empty strings to None
        normalized = {}
        for field_id in field_ids:
            value = result.get(field_id)
            if value is not None and str(value).strip() != "":
                normalized[field_id] = value
            else:
                normalized[field_id] = None
        
        return normalized
        
    except Exception as e:
        logger.error(f"{ctx}Error reading fields: {e}")
        raise


def write_field(loan_id: str, field_id: str, value: Any, state: dict = None) -> bool:
    """Write a single field to a loan using the fieldWriter API.
    
    Delegates to write_fields() which uses the Encompass v3 fieldWriter POST
    endpoint (/encompass/v3/loans/{loan_id}/fieldWriter). This endpoint
    properly translates field IDs (including virtual fields like L427,
    vendor fields like VEND.X*, and custom fields like CX.*) to their
    correct model paths.
    
    NOTE: Do NOT use the PATCH API (client.write_field) for field IDs.
    The PATCH endpoint expects JSON schema property names, not field IDs,
    and will reject virtual/vendor/custom field IDs with "Invalid field name".
    
    Args:
        loan_id: Encompass loan GUID
        field_id: Field ID to write
        value: Value to write
        state: Optional state dict to determine environment
        
    Returns:
        True if successful
    """
    return write_fields(loan_id, {field_id: value}, state=state)


def write_fields(loan_id: str, updates: Dict[str, Any], state: dict = None) -> bool:
    """Write multiple fields to a loan using EncompassConnect.
    
    Uses EncompassConnect.write_fields() which handles:
    - Automatic token refresh on 401 errors
    - Credential-based re-authentication
    
    When dry_run is enabled (via registry), logs what would be written
    and returns True without touching Encompass.
    
    Args:
        loan_id: Encompass loan GUID
        updates: Dictionary mapping field IDs to values
        state: Optional state dict to determine environment
        
    Returns:
        True if successful
    """
    if not updates:
        return True
    
    # Check dry_run from registry singleton
    dry_run = False
    try:
        from output.registry import DEV_MODE
        dry_run = getattr(DEV_MODE, "dry_run", False)
    except Exception:
        pass
    
    if dry_run:
        substep = (state or {}).get("current_substep", "?")
        logger.info(f"[DRY-RUN:{substep}] Would write {len(updates)} fields to {loan_id[:8]}: {updates}")
        _record_writes(updates, state, dry_run=True)
        return True
    
    try:
        client = get_encompass_client(state=state)
        result = client.write_fields(loan_id, updates)
        logger.info(f"Wrote {len(updates)} fields to loan {loan_id[:8]}")
        _record_writes(updates, state, dry_run=False)
        return result
        
    except Exception as e:
        logger.error(f"Error writing fields: {e}")
        raise


def get_loan_summary(loan_id: str) -> Dict[str, Any]:
    """Get a summary of key loan information.
    
    Args:
        loan_id: Encompass loan GUID
        
    Returns:
        Dictionary with key loan fields
    """
    from .constants import FieldIds
    
    field_ids = [
        FieldIds.BORROWER_FIRST_NAME,
        FieldIds.BORROWER_LAST_NAME,
        FieldIds.LOAN_TYPE,
        FieldIds.LOAN_PURPOSE,
        FieldIds.LOAN_AMOUNT,
        FieldIds.APPRAISED_VALUE,
        FieldIds.PURCHASE_PRICE,
        FieldIds.LTV,
        FieldIds.CLTV,
        FieldIds.INTEREST_RATE,
        FieldIds.LOAN_TERM,
        FieldIds.PROPERTY_STATE,
        FieldIds.PROPERTY_TYPE,
        FieldIds.OCCUPANCY_TYPE,
        FieldIds.APPLICATION_DATE,
        FieldIds.LOAN_STATUS,
    ]
    
    values = read_fields(loan_id, field_ids)
    
    def parse_float(val):
        if val is None:
            return None
        try:
            return float(str(val).replace(",", "").replace("$", "").replace("%", ""))
        except (ValueError, TypeError):
            return None
    
    def parse_int(val):
        if val is None:
            return None
        try:
            return int(float(str(val).replace(",", "")))
        except (ValueError, TypeError):
            return None
    
    return {
        "loan_id": loan_id,
        "borrower_first_name": values.get(FieldIds.BORROWER_FIRST_NAME),
        "borrower_last_name": values.get(FieldIds.BORROWER_LAST_NAME),
        "loan_type": values.get(FieldIds.LOAN_TYPE),
        "loan_purpose": values.get(FieldIds.LOAN_PURPOSE),
        "loan_amount": parse_float(values.get(FieldIds.LOAN_AMOUNT)),
        "appraised_value": parse_float(values.get(FieldIds.APPRAISED_VALUE)),
        "purchase_price": parse_float(values.get(FieldIds.PURCHASE_PRICE)),
        "ltv": parse_float(values.get(FieldIds.LTV)),
        "cltv": parse_float(values.get(FieldIds.CLTV)),
        "interest_rate": parse_float(values.get(FieldIds.INTEREST_RATE)),
        "loan_term": parse_int(values.get(FieldIds.LOAN_TERM)),
        "property_state": values.get(FieldIds.PROPERTY_STATE),
        "property_type": values.get(FieldIds.PROPERTY_TYPE),
        "occupancy_type": values.get(FieldIds.OCCUPANCY_TYPE),
        "application_date": values.get(FieldIds.APPLICATION_DATE),
        "loan_status": values.get(FieldIds.LOAN_STATUS),
    }


def get_loan_type(loan_id: str) -> Optional[str]:
    """Get loan type for a loan.
    
    Args:
        loan_id: Encompass loan GUID
        
    Returns:
        Loan type string, or None if not set
    """
    from .constants import FieldIds
    return read_field(loan_id, FieldIds.LOAN_TYPE)


def read_employment(
    loan_id: str,
    application_id: str = None,
    applicant_type: str = "borrower",
    state: dict = None,
) -> List[Dict[str, Any]]:
    """Fetch and normalise employment records from the Encompass v3 API.

    Calls:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/{applicantType}/employment

    Normalises each record into a consistent shape aligned with the existing
    BE01xx/BE02xx field key names used throughout the tools layer::

        {
          "id":                   str,
          "current":              bool,   # currentEmploymentIndicator
          "employer_name":        str,
          "employer_phone":       str,    # phoneNumber
          "employer_street":      str,    # urla2020StreetAddress or addressStreetLine1
          "employer_unit_type":   str,
          "employer_unit_number": str,
          "employer_city":        str,    # addressCity
          "employer_state":       str,    # addressState
          "employer_zip":         str,    # addressPostalCode
          "position_title":       str,    # positionDescription
          "date_hired":           str,    # employmentStartDate
          "date_terminated":      str,    # endDate
          "years_in_job":         int,    # timeOnJobTermYears
          "months_in_job":        int,    # timeOnJobTermMonths
          "years_in_line_of_work":int,    # timeInLineOfWorkYears
          "monthly_base_pay":     float,  # basePayAmount
          "monthly_income":       float,  # monthlyIncomeAmount
          "self_employed":        bool,   # selfEmployedIndicator
          "_raw":                 dict,   # original API record
        }

    Raises:
        LookupError: if the API returns "collection does not exist"
            (no employment rows created yet in Encompass).

    Args:
        loan_id: Encompass loan GUID
        application_id: Auto-resolved if omitted
        applicant_type: "borrower" (default) or "coborrower"
        state: Optional state dict (passed to the HTTP client)
    """
    try:
        from encompass_client import get_employment
    except ImportError:
        logger.warning("encompass_client not available — read_employment will fail at runtime")
        return []

    records = get_employment(
        loan_id,
        application_id=application_id,
        applicant_type=applicant_type,
        state=state,
    )

    normalised = []
    for r in records:
        street = r.get("urla2020StreetAddress") or r.get("addressStreetLine1") or ""
        normalised.append({
            "id":                    r.get("id", ""),
            "current":               bool(r.get("currentEmploymentIndicator", False)),
            "employer_name":         (r.get("employerName") or "").strip(),
            "employer_phone":        (r.get("phoneNumber") or "").strip(),
            "employer_street":       street.strip(),
            "employer_unit_type":    (r.get("unitType") or "").strip(),
            "employer_unit_number":  (r.get("unitNumber") or "").strip(),
            "employer_city":         (r.get("addressCity") or "").strip(),
            "employer_state":        (r.get("addressState") or "").strip(),
            "employer_zip":          (r.get("addressPostalCode") or "").strip(),
            "position_title":        (r.get("positionDescription") or "").strip(),
            "date_hired":            (r.get("employmentStartDate") or "").strip(),
            "date_terminated":       (r.get("endDate") or "").strip(),
            "years_in_job":          r.get("timeOnJobTermYears"),
            "months_in_job":         r.get("timeOnJobTermMonths"),
            "years_in_line_of_work": r.get("timeInLineOfWorkYears"),
            "monthly_base_pay":      r.get("basePayAmount"),
            "monthly_income":        r.get("monthlyIncomeAmount"),
            "self_employed":         bool(r.get("selfEmployedIndicator", False)),
            "_raw":                  r,
        })

    logger.info(
        f"[ENCOMPASS] read_employment: {len(normalised)} {applicant_type} record(s) for loan {loan_id[:8]}"
    )
    return normalised


def read_vods(loan_id: str, state: dict = None) -> List[Dict[str, Any]]:
    """Fetch and normalise all VOD entries for a loan from the Encompass v3 API.

    Each element in the returned list represents one **account row** (a single
    account type / account number) rather than one depository institution.
    This makes downstream comparison with per-copy bank-statement extractions
    straightforward.

    Returned item shape::

        {
          "vod_id":           str,   # Encompass VOD object ID
          "vod_index":        int,   # 1-based index within the loan
          "institution_name": str,   # depository name  (e.g. "PNC Bank")
          "borrower_type":    str,   # "BorrowerOnly" | "CoBorrowerOnly" | ...
          "account_type":     str,   # "CheckingAccount" | "SavingsAccount" | ...
          "account_holder":   str,   # name on account
          "account_number":   str,   # may be masked (e.g. "****2286")
          "balance":          float, # Cash or market value
        }

    Args:
        loan_id: Encompass loan GUID
        state: Optional state dict (passed to the underlying HTTP client)

    Returns:
        List of normalised account-row dicts (empty list if no VODs)
    """
    try:
        from encompass_client import get_vods
    except ImportError:
        logger.warning("encompass_client not available — read_vods will fail at runtime")
        return []

    raw_vods = get_vods(loan_id, state=state)
    rows: List[Dict[str, Any]] = []

    for vod in raw_vods:
        vod_id    = vod.get("id", "")
        vod_index = vod.get("vodIndex", 0)
        institution = (vod.get("depInstitution") or "").strip()
        borrower_type = vod.get("for", "")

        for acct in vod.get("accountInformation") or []:
            acct_type   = acct.get("accountType", "")
            acct_holder = (acct.get("accountInNameOf") or "").strip()
            acct_num    = (acct.get("accountNumber") or "").strip()
            try:
                balance = float(acct.get("cashOrMarketValue") or 0)
            except (TypeError, ValueError):
                balance = 0.0

            rows.append({
                "vod_id":           vod_id,
                "vod_index":        vod_index,
                "institution_name": institution,
                "borrower_type":    borrower_type,
                "account_type":     acct_type,
                "account_holder":   acct_holder,
                "account_number":   acct_num,
                "balance":          balance,
            })

    logger.info(f"[ENCOMPASS] read_vods: {len(raw_vods)} VOD object(s) → {len(rows)} account row(s)")
    return rows

