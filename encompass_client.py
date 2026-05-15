"""Common Encompass client helper for Disclosure Orchestrator tools.

This module provides a centralized way to get an EncompassConnect client
with proper credentials and automatic token refresh.

ALL Encompass API operations should go through EncompassConnect - no direct
requests calls should be made in tools or shared packages.

CACHING: Clients are cached per environment to avoid repeated authentication.
The cache is stored in the state dict under "_encompass_clients" key, ensuring
one client per workflow run per environment.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Cache key for storing clients in state
_STATE_CACHE_KEY = "_encompass_clients"

# Module-level cache for clients (survives parallel tool calls)
# This is needed because LangGraph executes parallel tool calls with separate state copies
_MODULE_CLIENT_CACHE: dict = {}

# Cached workflow environment - set on first state-aware call, used as fallback
# when state is not passed (e.g., from helper functions). Safe because each
# workflow run is a single process and env doesn't change mid-workflow.
_workflow_env: Optional[str] = None


def reset_encompass_state():
    """Reset cached workflow env and client cache.
    
    Call this at the start of a new workflow run to ensure fresh state.
    Each workflow run should resolve env from state["env"] independently.
    """
    global _workflow_env
    _workflow_env = None
    _MODULE_CLIENT_CACHE.clear()
    logger.debug("[ENCOMPASS] Reset workflow env and client cache")


def _get_env_var(name: str, env_prefix: Optional[str] = None) -> Optional[str]:
    """Get environment variable with optional prefix.
    
    Args:
        name: Base variable name (e.g., "ENCOMPASS_CLIENT_ID")
        env_prefix: Optional prefix ("PROD_" or "TEST_")
        
    Returns:
        Environment variable value or None
    """
    if env_prefix:
        # Try prefixed version first (e.g., PROD_ENCOMPASS_CLIENT_ID)
        prefixed = f"{env_prefix}{name}"
        value = os.getenv(prefixed)
        if value:
            return value
    # Fall back to unprefixed version
    return os.getenv(name)


def get_encompass_client(env: str = None, state: dict = None, use_cache: bool = True):
    """Get an initialized EncompassConnect client with credentials from environment.
    
    CACHING: By default, returns a cached client instance per environment to avoid
    repeated authentication calls. Clients are cached in state dict under "_encompass_clients"
    key, ensuring one client per workflow run per environment. Set use_cache=False to force new client.
    
    Environment selection priority (highest to lowest):
    1. Explicit `env` parameter ("Test" or "Prod")
    2. State's `env` field (from workflow input)
    3. ENCOMPASS_ENV environment variable
    4. Default: "TEST"
    
    Uses ENCOMPASS_ENV to select between TEST and PROD environments:
    - TEST: Uses TEST_ENCOMPASS_* variables (client credentials flow)
    - PROD: Uses PROD_ENCOMPASS_* variables (password + impersonation flow)
    
    The EncompassConnect client handles:
    - Automatic token refresh on 401 errors
    - Credential-based authentication (supports two flows):
      * Client Credentials flow (test env): client_id, client_secret, instance_id only
      * Password + Impersonation flow (prod): all 6 credential fields
    - All Encompass API operations including:
      - get_field / write_field / write_fields - Field I/O
      - get_loan_entity - Full loan data
      - search_loans_pipeline - Loan search
      - get_loan_documents / download_attachment - Document operations
      - get_disclosure_tracking - Disclosure status
      - get_milestones - Loan milestones
      - run_mavent / get_mavent_results - Mavent compliance
      - order_disclosure - Disclosure ordering
    
    Args:
        env: Optional environment override ("Test" or "Prod")
        state: Optional state dict with "env" field from workflow input
        use_cache: Whether to use cached client (default: True)
    
    Returns:
        EncompassConnect instance configured with env credentials
    """
    from copilotagent import EncompassConnect
    
    global _workflow_env
    
    # Determine environment: explicit param > state > cached workflow env > default
    # NOTE: ENCOMPASS_ENV env var is NOT used. Environment MUST come from state["env"].
    env_mode = None
    if env:
        env_mode = env.upper()
    elif state and state.get("env"):
        env_mode = state.get("env").upper()
    elif _workflow_env:
        # Use cached env from a previous state-aware call in this workflow
        env_mode = _workflow_env
        logger.debug(f"[ENCOMPASS] Using cached workflow env: {env_mode}")
    else:
        env_mode = "TEST"
        logger.warning(
            "[ENCOMPASS] No env specified via state or cache - defaulting to TEST. "
            "Ensure state is passed to get_encompass_client() or write_fields()/read_fields()."
        )
    
    if env_mode not in ("TEST", "PROD"):
        logger.warning(f"[ENCOMPASS] Invalid env='{env_mode}', defaulting to TEST")
        env_mode = "TEST"
    
    # Cache the resolved env for future calls that may not have state
    if _workflow_env is None:
        _workflow_env = env_mode
        logger.info(f"[ENCOMPASS] Cached workflow env: {env_mode}")
    
    env_prefix = f"{env_mode}_"
    
    # Get instance_id for cache key
    instance_id = _get_env_var("ENCOMPASS_INSTANCE_ID", env_prefix)
    cache_key = f"{env_mode}_{instance_id}" if instance_id else env_mode
    
    # Check MODULE-LEVEL cache FIRST (survives parallel tool calls in same workflow)
    # This is critical because LangGraph runs parallel tools with separate state copies
    if use_cache and cache_key in _MODULE_CLIENT_CACHE:
        logger.debug(f"[ENCOMPASS] Using cached client from MODULE cache for {env_mode} environment")
        return _MODULE_CLIENT_CACHE[cache_key]
    
    # Check state-based cache as backup
    if use_cache and state is not None:
        state_cache = state.get(_STATE_CACHE_KEY, {})
        if cache_key in state_cache:
            logger.debug(f"[ENCOMPASS] Using cached client from state for {env_mode} environment")
            return state_cache[cache_key]
    
    logger.info(f"[ENCOMPASS] Creating new client for {env_mode} environment")
    
    # Build credentials dict with only non-empty values
    credentials = {}
    
    # Required for both flows
    client_id = _get_env_var("ENCOMPASS_CLIENT_ID", env_prefix)
    if client_id:
        credentials["client_id"] = client_id
    
    client_secret = _get_env_var("ENCOMPASS_CLIENT_SECRET", env_prefix)
    if client_secret:
        credentials["client_secret"] = client_secret
    
    if instance_id:
        credentials["instance_id"] = instance_id
    
    # Only for password flow (production)
    username = _get_env_var("ENCOMPASS_USERNAME", env_prefix)
    if username:
        credentials["username"] = username
    
    password = _get_env_var("ENCOMPASS_PASSWORD", env_prefix)
    if password:
        credentials["password"] = password
    
    subject_user_id = _get_env_var("ENCOMPASS_SUBJECT_USER_ID", env_prefix)
    if subject_user_id:
        credentials["subject_user_id"] = subject_user_id
    
    # Get API base URL (with prefix support)
    api_base_url = _get_env_var("ENCOMPASS_API_BASE_URL", env_prefix) or "https://api.elliemae.com"
    
    client = EncompassConnect(
        access_token=os.getenv("ENCOMPASS_ACCESS_TOKEN") or None,
        api_base_url=api_base_url,
        credentials=credentials if credentials else None,
        landingai_api_key=os.getenv("LANDINGAI_API_KEY") or None,
    )
    
    logger.info(f"[ENCOMPASS] Client initialized - env: {env_mode}, auth_flow: {client._auth_flow}")
    
    # Cache in MODULE-LEVEL cache (survives parallel tool calls)
    if use_cache:
        _MODULE_CLIENT_CACHE[cache_key] = client
        logger.debug(f"[ENCOMPASS] Cached client in MODULE cache with key: {cache_key}")
    
    # Also cache in state for persistence across workflow checkpoints
    if use_cache and state is not None:
        if _STATE_CACHE_KEY not in state:
            state[_STATE_CACHE_KEY] = {}
        state[_STATE_CACHE_KEY][cache_key] = client
        logger.debug(f"[ENCOMPASS] Cached client in state with key: {cache_key}")
    
    return client


# Field ID constants for common loan data
class FieldIds:
    """Encompass field IDs for common loan data."""
    # Borrower info
    BORROWER_FIRST_NAME = "4000"
    BORROWER_LAST_NAME = "4002"
    BORROWER_EMAIL = "1240"
    BORROWER_PHONE = "66"
    
    # Loan info
    LOAN_NUMBER = "364"
    LOAN_TYPE = "1172"
    LOAN_PURPOSE = "19"
    LOAN_AMOUNT = "1109"
    
    # Property info
    PROPERTY_STATE = "14"
    PROPERTY_TYPE = "1041"
    PROPERTY_ADDRESS = "11"
    
    # Values
    APPRAISED_VALUE = "356"
    PURCHASE_PRICE = "136"
    LTV = "353"
    CLTV = "976"
    
    # Rate info
    INTEREST_RATE = "3"
    LOAN_TERM = "4"
    
    # Dates
    APPLICATION_DATE = "745"
    ESTIMATED_CLOSING_DATE = "763"
    LOCK_DATE = "761"


def read_loan_fields(loan_id: str, field_ids: list[str], state: dict = None) -> dict[str, any]:
    """Read multiple fields from a loan using EncompassConnect.
    
    Uses client.get_field() which handles token refresh automatically.
    
    Args:
        loan_id: Encompass loan GUID
        field_ids: List of field IDs to read
        state: Optional state dict to determine environment
        
    Returns:
        Dictionary mapping field_id to value (None if empty)
    """
    client = get_encompass_client(state=state)
    
    try:
        # Use get_field which supports reading multiple fields at once
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
        logger.error(f"[ENCOMPASS] Error reading fields: {e}")
        raise


def write_loan_fields(loan_id: str, updates: dict[str, any], state: dict = None) -> bool:
    """Write multiple fields to a loan using EncompassConnect.
    
    Uses client.write_fields() which handles token refresh automatically.
    
    Note: If you need to skip writing to Encompass, skip the entire step using
    DOCSORCH_DEV_SKIPPED_STEPS environment variable instead.
    
    Args:
        loan_id: Encompass loan GUID
        updates: Dictionary mapping field IDs to values
        state: Optional state dict to determine environment
        
    Returns:
        True if successful
    """
    if not updates:
        return True
    
    client = get_encompass_client(state=state)
    
    try:
        result = client.write_fields(loan_id, updates)
        logger.info(f"[ENCOMPASS] Wrote {len(updates)} fields to loan {loan_id[:8]}")
        return result
        
    except Exception as e:
        logger.error(f"[ENCOMPASS] Error writing fields: {e}")
        raise


def get_disclosure_tracking(loan_id: str, state: dict = None) -> dict[str, any]:
    """Get disclosure tracking info using EncompassConnect.
    
    Args:
        loan_id: Encompass loan GUID
        state: Optional state dict to determine environment
        
    Returns:
        Dictionary with disclosure tracking data
    """
    client = get_encompass_client(state=state)
    
    try:
        return client.get_disclosure_tracking(loan_id)
    except Exception as e:
        logger.error(f"[ENCOMPASS] Error getting disclosure tracking: {e}")
        raise


def get_milestones(loan_id: str, state: dict = None) -> list[dict[str, any]]:
    """Get loan milestones using EncompassConnect.
    
    Args:
        loan_id: Encompass loan GUID
        state: Optional state dict to determine environment
        
    Returns:
        List of milestone dictionaries
    """
    client = get_encompass_client(state=state)
    
    try:
        return client.get_milestones(loan_id)
    except Exception as e:
        logger.error(f"[ENCOMPASS] Error getting milestones: {e}")
        raise


def get_mavent_results(loan_id: str, state: dict = None) -> dict[str, any]:
    """Get Mavent/ECS compliance report for a loan.

    Uses the Encompass Compliance Service (ECS) API:
    GET /ecs/v1/complianceReports?entityType=urn:elli:encompass:loan&entityId={loanId}

    Args:
        loan_id: Encompass loan GUID
        state: Optional state dict to determine environment

    Returns:
        Dictionary with compliance report results
    """
    import requests as _requests

    client = get_encompass_client(state=state)

    url = f"{client.api_base_url}/ecs/v1/complianceReports"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }
    params = {
        "entityType": "urn:elli:encompass:loan",
        "entityId": loan_id,
    }

    try:
        response = _requests.get(url, headers=headers, params=params, timeout=60)
        if response.status_code == 404:
            return {"found": False, "message": "No compliance report found"}
        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] ECS GET failed (status {response.status_code}): {response.text[:300]}")
            raise Exception(f"ECS compliance report retrieval failed (status {response.status_code}): {response.text[:200]}")
        return response.json()
    except _requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting ECS report: {e}")
        raise


def run_mavent(loan_id: str, run_type: str = "FULL", state: dict = None) -> dict[str, any]:
    """Order an ECS (Mavent) compliance report for a loan.

    Uses the Encompass Compliance Service (ECS) API:
    POST /ecs/v1/complianceReports

    Args:
        loan_id: Encompass loan GUID
        run_type: "Review" for full report or "Preview" for preview
        state: Optional state dict to determine environment

    Returns:
        Dictionary with compliance report results
    """
    import requests as _requests

    client = get_encompass_client(state=state)

    report_type = "Review" if run_type.upper() == "FULL" else "Preview"

    url = f"{client.api_base_url}/ecs/v1/complianceReports"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
        "content-type": "application/json",
    }
    payload = {
        "entity": {
            "entityType": "urn:elli:encompass:loan",
            "entityId": loan_id,
        },
        "reportType": report_type,
    }

    try:
        response = _requests.post(url, json=payload, headers=headers, timeout=120)
        if response.status_code not in (200, 201, 202):
            logger.error(f"[ENCOMPASS] ECS POST failed (status {response.status_code}): {response.text[:300]}")
            raise Exception(f"ECS compliance report order failed (status {response.status_code}): {response.text[:200]}")
        return response.json()
    except _requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error ordering ECS report: {e}")
        raise


def order_disclosure(loan_id: str, disclosure_type: str = "LE", delivery_method: str = "eDisclosure", state: dict = None) -> dict[str, any]:
    """Order a disclosure using EncompassConnect.
    
    Args:
        loan_id: Encompass loan GUID
        disclosure_type: "LE", "CD", etc.
        delivery_method: "eDisclosure", "Paper", etc.
        state: Optional state dict to determine environment
        
    Returns:
        Dictionary with order result
    """
    client = get_encompass_client(state=state)
    
    try:
        return client.order_disclosure(loan_id, disclosure_type, delivery_method)
    except Exception as e:
        logger.error(f"[ENCOMPASS] Error ordering disclosure: {e}")
        raise


def get_loan_conditions(loan_id: str, condition_type: str = "underwriting", state: dict = None) -> list[dict[str, any]]:
    """Get all conditions of a specific type for a loan.
    
    Uses Encompass API v1 endpoint: GET /encompass/v1/loans/{LoanGuid}/conditions/{type}
    
    Args:
        loan_id: Encompass loan GUID
        condition_type: Type of conditions to retrieve:
            - "underwriting" (default)
            - "preliminary"
            - "postClosing" 
            - "lockDenial"
        state: Optional state dict to determine environment
            
    Returns:
        List of condition dictionaries
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    url = f"{client.api_base_url}/encompass/v1/loans/{loan_id}/conditions/{condition_type}"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 404:
            logger.info(f"[ENCOMPASS] No {condition_type} conditions found for loan {loan_id[:8]}")
            return []
        
        if response.status_code == 403:
            logger.warning(f"[ENCOMPASS] 403 Forbidden - check API permissions for {condition_type} conditions")
            raise PermissionError(f"403 Forbidden - {condition_type} conditions API requires specific permissions")
        
        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] Error getting {condition_type} conditions: {response.status_code}")
            raise Exception(f"API error: {response.status_code} - {response.text[:200]}")
        
        conditions = response.json()
        logger.info(f"[ENCOMPASS] Retrieved {len(conditions)} {condition_type} conditions for loan {loan_id[:8]}")
        return conditions
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting {condition_type} conditions: {e}")
        raise


def get_underwriting_conditions(loan_id: str, state: dict = None) -> list[dict[str, any]]:
    """Get all underwriting conditions for a loan.
    
    Uses Encompass API v1 endpoint: GET /encompass/v1/loans/{LoanGuid}/conditions/underwriting
    
    Args:
        loan_id: Encompass loan GUID
        state: Optional state dict to determine environment
        
    Returns:
        List of condition dictionaries, each containing:
        - id: Condition ID
        - title: Condition title
        - description: Condition description
        - priorTo: When condition must be satisfied (Funding, ClearToClose, etc.)
        - statusDescription: Current status (Pending, Cleared, etc.)
        - category: Condition category
        - addedDate: Date condition was created
        - source: Source of condition
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    # Use v1 endpoint for underwriting conditions
    url = f"{client.api_base_url}/encompass/v1/loans/{loan_id}/conditions/underwriting"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        
        # Handle token refresh on 401
        if response.status_code == 401:
            logger.info("[ENCOMPASS] Token expired, refreshing...")
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 404:
            logger.info(f"[ENCOMPASS] No underwriting conditions found for loan {loan_id[:8]}")
            return []
        
        if response.status_code == 403:
            logger.warning(f"[ENCOMPASS] 403 Forbidden - check API permissions for underwriting conditions")
            raise PermissionError("403 Forbidden - Underwriting conditions API requires specific permissions")
        
        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] Error getting conditions: {response.status_code} - {response.text}")
            raise Exception(f"API error: {response.status_code} - {response.text[:200]}")
        
        conditions = response.json()
        logger.info(f"[ENCOMPASS] Retrieved {len(conditions)} underwriting conditions for loan {loan_id[:8]}")
        return conditions
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting conditions: {e}")
        raise


def create_loan_condition(
    loan_id: str, 
    condition_data: dict,
    condition_type: str = "underwriting",
    state: dict = None
) -> dict[str, any]:
    """Create a condition of any type on a loan.
    
    Uses Encompass API v1 endpoint: POST /encompass/v1/loans/{LoanGuid}/conditions/{type}
    
    Args:
        loan_id: Encompass loan GUID
        condition_data: Condition data dictionary with:
            - title: Condition title (required)
            - description: Condition description
            - priorTo: When condition must be satisfied (Funding, ClearToClose, etc.)
            - category: Condition category
            - source: Source of condition
        condition_type: Type of condition to create:
            - "underwriting" (default)
            - "preliminary"
            - "postClosing"
        state: Optional state dict to determine environment
            
    Returns:
        Dictionary with created condition info including ID
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    url = f"{client.api_base_url}/encompass/v1/loans/{loan_id}/conditions/{condition_type}"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
        "content-type": "application/json",
    }
    
    # Map categories to valid values
    category = condition_data.get("category", "Credit")
    category_map = {
        "Other": "Credit",
        "Title": "Legal",
        "Appraisal": "Property",
        "Insurance": "Assets",
        "Compliance": "Legal",
        "Miscellaneous": "Credit",
        "Employment": "Income",
    }
    category = category_map.get(category, category)
    
    payload = {
        "title": condition_data.get("title", ""),
        "description": condition_data.get("description", ""),
        "priorTo": condition_data.get("priorTo", "Funding"),
        "category": category,
        "source": "Manual",
        "forAllApplications": condition_data.get("forAllApplications", True),
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        if response.status_code == 403:
            raise PermissionError(f"403 Forbidden - {condition_type} conditions API requires specific permissions")
        
        if response.status_code not in (200, 201):
            raise Exception(f"API error: {response.status_code} - {response.text[:200]}")
        
        result = response.json() if response.text else {}
        condition_id = result.get("id", "")
        
        if not condition_id and "Location" in response.headers:
            location = response.headers.get("Location", "")
            condition_id = location.split("/")[-1] if location else ""
            result["id"] = condition_id
        
        logger.info(f"[ENCOMPASS] Created {condition_type} condition: {condition_id}")
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error creating {condition_type} condition: {e}")
        raise


def get_closing_conditions(loan_id: str, state: dict = None) -> list[dict[str, any]]:
    """Get all post-closing conditions for a loan.
    
    Convenience wrapper for get_loan_conditions(loan_id, "postClosing")
    """
    return get_loan_conditions(loan_id, "postClosing", state=state)


def create_closing_condition(loan_id: str, condition_data: dict, state: dict = None) -> dict[str, any]:
    """Create a post-closing condition on a loan.
    
    Convenience wrapper for create_loan_condition with condition_type="postClosing"
    """
    return create_loan_condition(loan_id, condition_data, "postClosing", state=state)


def create_underwriting_condition(loan_id: str, condition_data: dict, state: dict = None) -> dict[str, any]:
    """Create a single underwriting condition on a loan.
    
    Uses Encompass API v1 endpoint: POST /encompass/v1/loans/{LoanGuid}/conditions/underwriting
    
    Args:
        loan_id: Encompass loan GUID
        condition_data: Condition data dictionary with:
            - title: Condition title (required)
            - description: Condition description
            - priorTo: When condition must be satisfied (Funding, ClearToClose, etc.)
            - category: Condition category
            - source: Source of condition
        state: Optional state dict to determine environment
            
    Returns:
        Dictionary with created condition info including ID
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    # Use v1 endpoint for underwriting conditions
    url = f"{client.api_base_url}/encompass/v1/loans/{loan_id}/conditions/underwriting"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
        "content-type": "application/json",
    }
    
    # Ensure required fields with valid Encompass values
    # Valid categories: Credit, Income, Assets, Property, Legal
    # Valid source: "Manual" (required for API-created conditions)
    category = condition_data.get("category", "Credit")
    # Map common aliases to valid categories
    category_map = {
        "Other": "Credit",
        "Title": "Legal",
        "Appraisal": "Property",
        "Insurance": "Assets",
        "Compliance": "Legal",
        "Miscellaneous": "Credit",
        "Employment": "Income",
    }
    category = category_map.get(category, category)
    
    payload = {
        "title": condition_data.get("title", ""),
        "description": condition_data.get("description", ""),
        "priorTo": condition_data.get("priorTo", "Funding"),
        "category": category,
        "source": "Manual",  # API-created conditions must use "Manual"
        "forAllApplications": condition_data.get("forAllApplications", True),
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        # Handle token refresh on 401
        if response.status_code == 401:
            logger.info("[ENCOMPASS] Token expired, refreshing...")
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        if response.status_code == 403:
            logger.warning(f"[ENCOMPASS] 403 Forbidden - check API permissions for creating conditions")
            raise PermissionError("403 Forbidden - Underwriting conditions API requires specific permissions")
        
        if response.status_code not in (200, 201):
            logger.error(f"[ENCOMPASS] Error creating condition: {response.status_code} - {response.text}")
            raise Exception(f"API error: {response.status_code} - {response.text[:200]}")
        
        # Get ID from response or Location header
        result = response.json() if response.text else {}
        condition_id = result.get("id", "")
        
        if not condition_id and "Location" in response.headers:
            location = response.headers.get("Location", "")
            condition_id = location.split("/")[-1] if location else ""
            result["id"] = condition_id
        
        logger.info(f"[ENCOMPASS] Created underwriting condition: {condition_id}")
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error creating condition: {e}")
        raise


def get_condition_templates(condition_type: str = None, state: dict = None) -> list[dict[str, any]]:
    """Get all available condition templates from Encompass.
    
    Uses Enhanced Conditions API: GET /encompass/v3/settings/loan/conditions/templates
    
    Args:
        condition_type: Optional filter by type (Underwriting, Preliminary, PostClosing)
        state: Optional state dict to determine environment
        
    Returns:
        List of template dictionaries with:
        - id: Template ID (use this when creating conditions)
        - title: Template title/name
        - description: Template description
        - category: Category
        - priorTo: When due
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    # Try multiple endpoints
    endpoints_to_try = [
        "/encompass/v3/settings/loan/conditions/templates",
        "/encompass/v3/settings/loan/enhancedConditions/templates",
    ]
    
    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "Accept": "application/json",
    }
    
    for endpoint in endpoints_to_try:
        url = f"{client.api_base_url}{endpoint}"
        
        try:
            response = requests.get(url, headers=headers, timeout=60)
            
            # Handle token refresh
            if response.status_code == 401:
                client.refresh_token()
                headers["Authorization"] = f"Bearer {client.access_token}"
                response = requests.get(url, headers=headers, timeout=60)
            
            if response.status_code == 200:
                templates = response.json()
                
                # Filter by condition type if specified
                if condition_type and templates:
                    templates = [t for t in templates 
                                if t.get("conditionType", "").lower() == condition_type.lower()]
                
                logger.info(f"[ENCOMPASS] Retrieved {len(templates)} condition templates")
                return templates
                
            elif response.status_code == 403:
                logger.debug(f"[ENCOMPASS] 403 Forbidden for {endpoint}")
                continue
            elif response.status_code == 404:
                continue
                
        except Exception as e:
            logger.debug(f"[ENCOMPASS] Error with {endpoint}: {e}")
            continue
    
    logger.warning("[ENCOMPASS] No condition templates endpoint returned success")
    return []


def create_condition_from_template(
    loan_id: str, 
    template_id: str,
    condition_type: str = "Underwriting",
    overrides: dict = None,
    state: dict = None
) -> dict[str, any]:
    """Create an underwriting condition from a template.
    
    Uses Enhanced Conditions API v3 endpoint: POST /encompass/v3/loans/{loanId}/conditions
    
    This allows creating conditions based on pre-defined company templates,
    ensuring consistency with company standards.
    
    Args:
        loan_id: Encompass loan GUID
        template_id: The condition template ID to use
        condition_type: Type of condition (Underwriting, Preliminary, PostClosing)
        overrides: Optional dict of fields to override from template
            - description: Override description
            - priorTo: Override when due (Funding, ClearToClose, etc.)
        state: Optional state dict to determine environment
            
    Returns:
        Dictionary with created condition info including ID
        
    Example:
        >>> result = create_condition_from_template(
        ...     loan_id="ae9dd6e2-...",
        ...     template_id="template-guid-123",
        ...     condition_type="Underwriting",
        ...     overrides={"description": "Custom description for this loan"}
        ... )
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    # Use v3 Enhanced Conditions endpoint
    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/conditions"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
        "content-type": "application/json",
    }
    
    # Build payload with template reference
    payload = {
        "templateId": template_id,
        "conditionType": condition_type,
    }
    
    # Apply any overrides
    if overrides:
        payload.update(overrides)
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        # Handle token refresh on 401
        if response.status_code == 401:
            logger.info("[ENCOMPASS] Token expired, refreshing...")
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        if response.status_code == 403:
            logger.warning("[ENCOMPASS] 403 Forbidden - Enhanced Conditions API requires specific permissions")
            raise PermissionError("403 Forbidden - Enhanced Conditions API requires specific permissions")
        
        if response.status_code == 404:
            logger.error(f"[ENCOMPASS] Template not found: {template_id}")
            raise ValueError(f"Template not found: {template_id}")
        
        if response.status_code not in (200, 201):
            logger.error(f"[ENCOMPASS] Error creating condition from template: {response.status_code} - {response.text}")
            raise Exception(f"API error: {response.status_code} - {response.text[:200]}")
        
        result = response.json() if response.text else {}
        condition_id = result.get("id", "")
        
        if not condition_id and "Location" in response.headers:
            location = response.headers.get("Location", "")
            condition_id = location.split("/")[-1] if location else ""
            result["id"] = condition_id
        
        logger.info(f"[ENCOMPASS] Created condition from template {template_id[:8]}: {condition_id}")
        return result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error creating condition from template: {e}")
        raise


def get_document_with_comments(loan_id: str, document_id: str, state: dict = None) -> dict[str, any]:
    """Get detailed document information including comments and status.
    
    Uses Encompass API V1 endpoint which includes:
    - Document comments with metadata (author, date, role)
    - Document status and status history
    - Request/receive/review tracking
    - Attachment details
    
    Reference: https://developer.icemortgagetechnology.com/developer-connect/reference/get-document
    
    Args:
        loan_id: Encompass loan GUID
        document_id: Document ID from get_loan_documents()
        state: Optional state dict to determine environment
        
    Returns:
        Dictionary with document details including:
        - comments: List of comment objects with text, author, date, role
        - status: Current document status
        - description: Document description
        - attachments: List of attachments
        - statusDate, dateRequested, dateReceived, etc.
        
    Example:
        >>> doc = get_document_with_comments(loan_id, doc_id)
        >>> for comment in doc.get("comments", []):
        ...     print(f"{comment['createdByName']}: {comment['comments']}")
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    # Use V1 endpoint which includes comments (V3 doesn't have comments)
    url = f"{client.api_base_url}/encompass/v1/loans/{loan_id}/documents/{document_id}"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        
        # Handle token refresh on 401
        if response.status_code == 401:
            logger.info("[ENCOMPASS] Token expired, refreshing...")
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 404:
            logger.info(f"[ENCOMPASS] Document not found: {document_id[:8]}")
            return {}
        
        if response.status_code == 403:
            logger.warning(f"[ENCOMPASS] 403 Forbidden - check API permissions for document details")
            raise PermissionError("403 Forbidden - Document details API requires specific permissions")
        
        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] Error getting document: {response.status_code}")
            raise Exception(f"API error: {response.status_code} - {response.text[:200]}")
        
        document = response.json()
        
        # Log if comments found
        comments = document.get("comments", [])
        if comments:
            logger.info(f"[ENCOMPASS] Retrieved document with {len(comments)} comment(s)")
        
        return document
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting document: {e}")
        raise


def get_loan_summary_from_api(loan_id: str) -> dict[str, any]:
    """Get loan summary using EncompassConnect.
    
    Args:
        loan_id: Encompass loan GUID
        
    Returns:
        Dictionary with loan summary:
        - loan_type: Conventional, FHA, VA, USDA
        - loan_purpose: Purchase, Refinance, etc.
        - loan_amount: Loan amount
        - property_state: State code
        - ltv: LTV percentage
        - etc.
    """
    field_ids = [
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
    ]
    
    values = read_loan_fields(loan_id, field_ids)
    
    # Parse numeric values
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
    
    # Normalize loan type
    loan_type_raw = values.get(FieldIds.LOAN_TYPE)
    loan_type = "Unknown"
    if loan_type_raw:
        lt = str(loan_type_raw).lower()
        if "conventional" in lt or "conv" in lt:
            loan_type = "Conventional"
        elif "fha" in lt:
            loan_type = "FHA"
        elif "va" in lt:
            loan_type = "VA"
        elif "usda" in lt or "rural" in lt:
            loan_type = "USDA"
        else:
            loan_type = lt.title()
    
    return {
        "loan_id": loan_id,
        "loan_type": loan_type,
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
    }


def update_trustee_entity(
    loan_id: str,
    trustee_name: str,
    address: str,
    city: str,
    property_state: str,
    zip_code: str,
    county: str = None,
    phone: str = None,
    state: dict = None,
) -> dict[str, any]:
    """Update the Trustee in the loan's ClosingEntities via PATCH API.
    
    This uses the loan entity PATCH endpoint to update the ClosingEntities
    array, specifically targeting the Trustee entry.
    
    Args:
        loan_id: Encompass loan GUID
        trustee_name: Full name of the trustee
        address: Street address
        city: City
        property_state: State (2-letter code)
        zip_code: ZIP code
        county: County (optional)
        phone: Phone number (optional)
        state: Optional state dict to determine environment
        
    Returns:
        dict with status and any error message
    """
    import requests
    
    client = get_encompass_client(state=state)
    
    # Build the Trustee entity object using Encompass v3 PATCH schema property names.
    # These were verified by local API testing:
    #   unparsedName  -> L427  (Trustee Name)
    #   streetAddress -> 1909  (Trustee Address)
    #   city          -> 1910  (Trustee City)
    #   state         -> 1911  (Trustee State)
    #   postalCode    -> 1912  (Trustee Zip)
    #   phone         -> 3552  (Trustee Phone)
    trustee_entity = {
        "closingEntityType": "RecordableDocumentTrustee",
        "unparsedName": trustee_name,
    }
    
    if address:
        trustee_entity["streetAddress"] = address
    if city:
        trustee_entity["city"] = city
    if property_state:
        trustee_entity["state"] = property_state
    if zip_code:
        trustee_entity["postalCode"] = zip_code
    if county:
        trustee_entity["county"] = county
    if phone:
        trustee_entity["phone"] = phone
    
    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
        "content-type": "application/json",
    }
    
    # PATCH directly with the trustee entity — no need to read/merge existing entities.
    # The API handles upsert by closingEntityType.
    patch_payload = {
        "closingDocument": {
            "closingEntities": [trustee_entity]
        }
    }
    
    try:
        response = requests.patch(url, json=patch_payload, headers=headers, timeout=30)
        
        if response.status_code in (200, 204):
            logger.info(f"[ENCOMPASS] Trustee updated successfully for loan {loan_id[:8]}")
            return {"success": True, "message": "Trustee updated successfully"}
        else:
            error_msg = f"PATCH failed (status {response.status_code}): {response.text}"
            logger.error(f"[ENCOMPASS] {error_msg}")
            return {"success": False, "error": error_msg}
            
    except Exception as e:
        error_msg = str(e)
        logger.error(f"[ENCOMPASS] Error updating Trustee: {error_msg}")
        return {"success": False, "error": error_msg}


def get_employment(
    loan_id: str,
    application_id: str = None,
    applicant_type: str = "borrower",
    state: dict = None,
) -> list[dict[str, any]]:
    """Get employment records for a borrower or co-borrower from the Encompass v3 API.

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/{applicantType}/employment

    Args:
        loan_id: Encompass loan GUID
        application_id: Application ID (auto-resolved via get_loan_applications if omitted)
        applicant_type: "borrower" (default) or "coborrower"
        state: Optional state dict to determine environment

    Returns:
        List of employment record dicts. Each record typically contains::

            {
              "id": str,
              "currentIndicator": bool,
              "employerName": str,
              "employerPhone": str,
              "employerAddress": {
                "street1": str, "city": str, "state": str, "postalCode": str
              },
              "positionDescription": str,
              "startDate": str,        # ISO date or MM/DD/YYYY
              "endDate": str | None,
              "employmentMonthlyIncomeAmount": float,
              "timeInLineOfWorkYears": int,
              "timeInLineOfWorkMonths": int,
              "selfEmployedIndicator": bool,
            }

    Raises:
        LookupError: if the employment collection does not exist (no rows created yet).
        Exception: on any other API error.
    """
    import requests

    client = get_encompass_client(state=state)

    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = (
        f"{client.api_base_url}/encompass/v3/loans/{loan_id}"
        f"/applications/{application_id}/{applicant_type}/employment"
    )
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError(
                    f"Employment collection does not exist for {applicant_type} — no rows created yet"
                )
            logger.info(f"[ENCOMPASS] No employment records found for {applicant_type} on loan {loan_id[:8]}")
            return []

        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] Employment GET failed ({response.status_code}): {response.text[:200]}")
            raise Exception(f"Employment API error {response.status_code}: {response.text[:200]}")

        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(
            f"[ENCOMPASS] Retrieved {len(records)} employment record(s) "
            f"for {applicant_type} on loan {loan_id[:8]}"
        )
        return records

    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting employment: {e}")
        raise


def get_vols(
    loan_id: str,
    application_id: str = None,
    state: dict = None,
) -> list[dict[str, any]]:
    """Get all VOL (Verification of Liabilities) records for a loan application.

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/vols

    Key fields in each record (confirmed from test loan 2604964148):
        holderName                            — creditor name
        liabilityType                         — e.g. "Revolving", "Installment"
        monthlyPaymentAmount                  — monthly payment
        unpaidBalanceAmount                   — current balance
        creditLimit                           — credit limit (revolving only)
        owner                                 — "Borrower" | "CoBorrower" | "Both"
        accountIdentifier                     — account number (may be masked)
        nameInAccount                         — account holder name
        payoffIncludedIndicator               — "To Be Paid Off" column (bool)
        excludedFromTotalMonthlyPaymentIndicator — "Exclude Monthly Payment" column (bool)

    Raises LookupError if the collection does not exist yet.
    """
    import requests

    client = get_encompass_client(state=state)
    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/vols"
    headers = {"accept": "application/json", "Authorization": f"Bearer {client.access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError("VOL collection does not exist — no rows created yet")
            return []
        if response.status_code != 200:
            raise Exception(f"VOL API error {response.status_code}: {response.text[:200]}")
        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(f"[ENCOMPASS] get_vols: {len(records)} record(s) for loan {loan_id[:8]}")
        return records
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting VOLs: {e}")
        raise


def get_reo_properties(
    loan_id: str,
    application_id: str = None,
    state: dict = None,
) -> list[dict[str, any]]:
    """Get all REO (Real Estate Owned) properties for a loan application (Section 3).

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/reoProperties

    Key fields confirmed from test loan:
        streetAddress, city, dispositionStatusType (e.g. "Retain", "Sold", "PendingSale")
        owner ("Borrower" | "CoBorrower" | "Both")
        includeInAusExport, printAttachIndicator

    Returns empty list if no REO rows exist.
    Raises LookupError if the collection does not exist.
    """
    import requests

    client = get_encompass_client(state=state)
    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/reoProperties"
    headers = {"accept": "application/json", "Authorization": f"Bearer {client.access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError("reoProperties collection does not exist")
            return []
        if response.status_code != 200:
            raise Exception(f"reoProperties API error {response.status_code}: {response.text[:200]}")
        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(f"[ENCOMPASS] get_reo_properties: {len(records)} record(s) for loan {loan_id[:8]}")
        return records
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting reoProperties: {e}")
        raise


def get_other_liabilities(
    loan_id: str,
    application_id: str = None,
    state: dict = None,
) -> list[dict[str, any]]:
    """Get all Other Liabilities records for a loan application (Section 2d).

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/otherLiabilities

    Returns an empty list if the collection exists but has no rows.
    Raises LookupError if the collection does not exist.
    """
    import requests

    client = get_encompass_client(state=state)
    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/otherLiabilities"
    headers = {"accept": "application/json", "Authorization": f"Bearer {client.access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError("otherLiabilities collection does not exist")
            return []
        if response.status_code != 200:
            raise Exception(f"otherLiabilities API error {response.status_code}: {response.text[:200]}")
        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(f"[ENCOMPASS] get_other_liabilities: {len(records)} record(s) for loan {loan_id[:8]}")
        return records
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting otherLiabilities: {e}")
        raise


def get_other_income_sources(
    loan_id: str,
    application_id: str = None,
    state: dict = None,
) -> list[dict[str, any]]:
    """Get other income sources for a loan application.

    GET /encompass/v3/loans/{loanId}/applications/{applicationId}/otherIncomeSources

    Raises LookupError if the collection does not exist yet.
    """
    import requests

    client = get_encompass_client(state=state)
    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/otherIncomeSources"
    headers = {"accept": "application/json", "Authorization": f"Bearer {client.access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError("otherIncomeSources collection does not exist — no rows created yet")
            return []
        if response.status_code != 200:
            raise Exception(f"otherIncomeSources API error {response.status_code}: {response.text[:200]}")
        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(f"[ENCOMPASS] get_other_income_sources: {len(records)} record(s) for loan {loan_id[:8]}")
        return records
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting otherIncomeSources: {e}")
        raise


def get_other_assets(
    loan_id: str,
    application_id: str = None,
    state: dict = None,
) -> list[dict[str, any]]:
    """Get other assets for a loan application.

    GET /encompass/v3/loans/{loanId}/applications/{applicationId}/otherAssets

    Raises LookupError if the collection does not exist yet.
    """
    import requests

    client = get_encompass_client(state=state)
    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/otherAssets"
    headers = {"accept": "application/json", "Authorization": f"Bearer {client.access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError("otherAssets collection does not exist — no rows created yet")
            return []
        if response.status_code != 200:
            raise Exception(f"otherAssets API error {response.status_code}: {response.text[:200]}")
        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(f"[ENCOMPASS] get_other_assets: {len(records)} record(s) for loan {loan_id[:8]}")
        return records
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting otherAssets: {e}")
        raise


def get_gifts_grants(
    loan_id: str,
    application_id: str = None,
    state: dict = None,
) -> list[dict[str, any]]:
    """Get all gifts and grants for a loan application.

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/giftsGrants

    Key fields in each record (confirmed from test loan 2604964148):
        assetType           — "Grant", "GiftOfCash", "GiftOfEquity", etc.
        source              — "FederalAgency", "Relative", "Employer", etc.
        amount              — dollar amount (float)
        owner               — "Borrower" | "CoBorrower" | "Both"
        depositedIndicator  — True if already deposited into borrower account

    Raises LookupError if the collection does not exist yet.
    """
    import requests

    client = get_encompass_client(state=state)
    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/giftsGrants"
    headers = {"accept": "application/json", "Authorization": f"Bearer {client.access_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 404:
            body_lc = (response.text or "").lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                raise LookupError("giftsGrants collection does not exist — no rows created yet")
            return []
        if response.status_code != 200:
            raise Exception(f"giftsGrants API error {response.status_code}: {response.text[:200]}")
        records = response.json()
        if not isinstance(records, list):
            records = [records]
        logger.info(f"[ENCOMPASS] get_gifts_grants: {len(records)} record(s) for loan {loan_id[:8]}")
        return records
    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting giftsGrants: {e}")
        raise


def get_loan_applications(loan_id: str, state: dict = None) -> list[dict[str, any]]:
    """Get all applications (borrower pairs) for a loan.

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications

    Returns a list of application objects. The first item is the primary
    application and its ``id`` field is used as ``applicationId`` when
    calling sub-resource endpoints such as /vods.

    Args:
        loan_id: Encompass loan GUID
        state: Optional state dict to determine environment

    Returns:
        List of application dicts (may be empty on error)
    """
    import requests

    client = get_encompass_client(state=state)

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 404:
            logger.info(f"[ENCOMPASS] No applications found for loan {loan_id[:8]}")
            return []

        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] Error getting applications: {response.status_code} {response.text[:200]}")
            raise Exception(f"API error {response.status_code}: {response.text[:200]}")

        apps = response.json()
        logger.info(f"[ENCOMPASS] Retrieved {len(apps)} application(s) for loan {loan_id[:8]}")
        return apps

    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting applications: {e}")
        raise


def get_vods(loan_id: str, application_id: str = None, state: dict = None) -> list[dict[str, any]]:
    """Get all Verifications of Deposit (VODs) for a loan application.

    Uses Encompass v3 API:
        GET /encompass/v3/loans/{loanId}/applications/{applicationId}/vods

    The API returns one VOD object per depository institution. Each VOD
    contains a list of ``accountInformation`` entries (one row per account
    type/account number).

    Typical VOD object shape::

        {
          "id": "abc123",
          "vodIndex": 1,
          "depInstitution": "PNC Bank",
          "for": "BorrowerOnly",           # or "CoBorrowerOnly"
          "accountInformation": [
            {
              "accountType": "CheckingAccount",
              "accountInNameOf": "Cyndy Appell Jermain",
              "accountNumber": "****2286",
              "cashOrMarketValue": 4279.51
            },
            {
              "accountType": "SavingsAccount",
              "accountInNameOf": "Cyndy Appell Jermain",
              "accountNumber": "****0104",
              "cashOrMarketValue": 81473.81
            }
          ]
        }

    Args:
        loan_id: Encompass loan GUID
        application_id: Application ID string (default: auto-resolved via
            ``get_loan_applications()``; falls back to ``"1"``).
        state: Optional state dict to determine environment

    Returns:
        List of VOD dicts (empty list if none found)
    """
    import requests

    client = get_encompass_client(state=state)

    if not application_id:
        try:
            apps = get_loan_applications(loan_id, state=state)
            application_id = apps[0].get("id", "1") if apps else "1"
        except Exception:
            application_id = "1"

    url = f"{client.api_base_url}/encompass/v3/loans/{loan_id}/applications/{application_id}/vods"
    headers = {
        "accept": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 401:
            client.refresh_token()
            headers["Authorization"] = f"Bearer {client.access_token}"
            response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 404:
            # Encompass returns 404 both for "no rows yet" and "resource truly missing".
            # We distinguish by checking the error body for collection/application language.
            body = response.text or ""
            body_lc = body.lower()
            if any(kw in body_lc for kw in ("collection", "application", "does not exist", "not found")):
                logger.info(f"[ENCOMPASS] VOD collection does not exist for loan {loan_id[:8]} — no rows created yet")
                raise LookupError("VOD collection does not exist — no VOD rows have been created in Encompass")
            logger.info(f"[ENCOMPASS] No VODs found for loan {loan_id[:8]} application {application_id}")
            return []

        if response.status_code != 200:
            logger.error(f"[ENCOMPASS] Error getting VODs: {response.status_code} {response.text[:200]}")
            raise Exception(f"VOD API error {response.status_code}: {response.text[:200]}")

        vods = response.json()
        if not isinstance(vods, list):
            vods = [vods]
        logger.info(f"[ENCOMPASS] Retrieved {len(vods)} VOD(s) for loan {loan_id[:8]}")
        return vods

    except requests.exceptions.RequestException as e:
        logger.error(f"[ENCOMPASS] Network error getting VODs: {e}")
        raise

