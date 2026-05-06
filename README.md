# Processor Assistant Agent

**ProcessorAgent** is a LangGraph-based mortgage loan submission agent that orchestrates the full workflow a processor follows to prepare a loan file for underwriting (UW) submission in Encompass — pre-checks, data review (1003/URLA, Borrower Summary), form updates, orders, eFolder prep, AUS, and final submission.

The agent runs Steps 0–15 autonomously, pauses at Step 16 for a human-in-the-loop flag review, then performs the submission writes in Step 17+ once the processor confirms.

---

## Architecture at a glance

```
definitions/*.yaml          ──►   factory   ──►   output/
  (single source of truth)        (codegen)        (generated runtime)
       │                              │                  │
       │                              │                  ├── tools/        ── per-substep tools
       │                              │                  ├── plans/        ── per-step plans (LLM context)
       │                              │                  ├── config/       ── workflow_config.json, fields_config.json
       │                              │                  ├── registry.py   ── STEP_ORDER, tool maps
       │                              │                  └── proc_agent.py ── LangGraph entrypoint (graph)
       │                              │
       └── _agent.yaml                └── factory/agent_generator.py
       └── step_NN_*.yaml                 factory/code_generator.py
                                          factory/validator.py
```

Workflow steps, fields, doc types, rules, and flags are all declared in YAML under `definitions/`. The `factory` package generates everything in `output/` from those definitions. **The `output/` directory is never edited in sync manually — the factory is the single source of truth.**

---

## Workflow

Defined in `definitions/_agent.yaml` and `definitions/step_NN_*.yaml`:

| Step | Name | Phase |
|------|------|-------|
| 01 | Pre-Checks | INTAKE |
| 02 | Borrower Summary — Origination | DATA_REVIEW |
| 03 | 1003 URLA Page 1 | DATA_REVIEW |
| 04 | 1003 URLA Page 2 | DATA_REVIEW |
| 05 | 1003 URLA Part 3 | DATA_REVIEW |
| 06 | 1003 URLA Part 4 | DATA_REVIEW |
| 07 | Cover Letter | FORM_UPDATES |
| 08 | Borrower Info & Vesting | FORM_UPDATES |
| 09 | Transmittal Summary | FORM_UPDATES |
| 10 | Orders | ORDERS |
| 11 | Prep eFolder | PREP |
| 12 | Additional Services | ORDERS |
| 13 | Print 1003 & Transmittal | PREP |
| 14 | Run Fresh AUS | PREP |
| 15 | Processor Workflow & Closing | PROCESSOR_UPDATE |
| 16 | Flag Review (HITL) | PROCESSOR_UPDATE |
| 17 | Submission | SUBMISSION |
| 18 | Notifications | SUBMISSION |

Critical sequencing constraints (also enforced by the validator):

- Step 11 (Prep eFolder) must run before Steps 12 and 14.
- Step 14 (Fresh AUS) must run after Step 11.
- Step 16 (Flag Review HITL) must pass before Step 17.
- Step 17 Required-Fields → Finished click auto-places the Cover Letter in its bucket — do not separately upload it.

Loan-type / state-specific behavior is handled per-substep via `rule_modifiers` against a loan profile detected in Step 0 (`loan_type`, `purpose`, `state`, `property_type`, `loan_locked`).

---

## Repo layout

```
processor-assistant-agent/
├── definitions/              # YAML — agent + per-step definitions (source of truth)
│   ├── _agent.yaml
│   └── step_NN_*.yaml
├── factory/                  # Codegen: YAML → output/
│   ├── __main__.py           # `python -m factory <command>`
│   ├── cli.py
│   ├── agent_generator.py
│   ├── code_generator.py
│   ├── config_generator.py
│   ├── field_registry.py
│   ├── llm_plan_maker.py
│   ├── llm_tool_maker.py
│   ├── schema.py
│   ├── shared_tools_catalog.py
│   ├── step0_generator.py
│   ├── validator.py
│   └── templates/
├── output/                   # GENERATED — do not hand-edit unless FACTORY-LOCK: true
│   ├── proc_agent.py         # LangGraph entrypoint (`graph`)
│   ├── registry.py           # STEP_ORDER, tool/substep maps
│   ├── step_loader.py
│   ├── config/
│   │   ├── workflow_config.json
│   │   ├── fields_config.json
│   │   └── recipients.json
│   ├── plans/                # Per-step plan markdown injected into the system prompt
│   ├── tools/                # Per-substep tool implementations
│   └── shared/
├── shared/                   # Hand-written helpers (Encompass IO, USPS, OCR, etc.)
│   ├── encompass_io.py
│   ├── docrepo.py
│   ├── efolder_client.py
│   ├── document_type_registry.py
│   ├── field_utils.py
│   ├── insurance_helpers.py
│   ├── llm_call.py
│   ├── plan_codes.py
│   ├── reporting.py
│   ├── tool_helpers.py
│   ├── usps_validator.py
│   ├── conditions.py
│   └── constants.py
├── encompass_client.py       # Encompass API client (Test + Prod)
├── scripts/                  # One-off scripts (eFolder inspection, etc.)
├── langgraph.json            # LangGraph deployment config (graph: ./output/proc_agent.py:graph)
├── requirements.txt
├── pyrightconfig.json
└── .env.example
```

---

## Setup

Requires Python **3.11** (constraint from `copilotagent`).

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in tokens — see "Environment" below
```

### Environment

`.env.example` lists the secrets the agent reads at runtime. Key groups:

- **Encompass** (Test + Prod) — base URLs, client id/secret, instance id, plus prod password/subject user id
- **LandingAI / Textract** — document extraction OCR
- **DocRepo** — S3-backed document storage for extracted artifacts
- **USPS** — address validation
- **LLMs** — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- **LangGraph / LangSmith** — `LANGGRAPH_API_KEY`, `LANGSMITH_API_KEY`, etc.
- **eFolder** — `EFOLDER_API_TOKEN`
- **Agent settings** — `LANGGRAPH_DEFAULT_RECURSION_LIMIT` (default 100000), `AI_DEBUG_MODE`, `USE_LOCAL_COPILOTAGENT`

---

## Running the agent

The LangGraph entrypoint is declared in `langgraph.json`:

```json
{
  "graphs": { "processor-agent": "./output/proc_agent.py:graph" },
  "env": ".env"
}
```

Locally with the LangGraph CLI:

```bash
langgraph dev
```

The compiled graph (`output/proc_agent.py:graph`) accepts state with at minimum:

- `loan_number` — Encompass loan number (required)
- `env` — `"Test"` or `"Prod"` (required)
- `almas_notes` — originator notes (required for Step 7 Cover Letter)
- `processor_name` — required for Step 17 milestone change

Step 0 hydrates `loan_id`, `los_fields`, `doc_fields`, `efolder_documents`, `loan_summary`, and `loan_profile` into state for downstream steps.

---

## The factory (YAML → code)

All day-to-day changes start in `definitions/`. Then run the factory:

```bash
python3.11 -m factory <command>
```

| Command | What it does |
|---|---|
| `update-agent` | Incremental sync of `output/config/*.json`, `registry.py`, `step_loader.py`, plus `output/tools/__init__.py`. Use after most YAML edits. |
| `factory-reset` | Full scaffold/regenerate. Tools without `# FACTORY-LOCK: true` are overwritten. |
| `factory-reset --force` | Overwrite **everything**, including FACTORY-LOCK files. Use with care. |
| `validate` | Schema + cross-reference check across YAML and the field registry. Must print `Validation: PASSED`. |
| `status` | Overview of agent, steps, substeps, fields. |
| `new-step STEP_NN "Name" --phase PHASE` | Scaffold a new step YAML. |
| `renumber-steps` | Close gaps in step numbering and regenerate. |
| `dashboard --port 8501` | Launch the optional web dashboard for editing definitions. |

### FACTORY-LOCK

Every generated tool file has `# FACTORY-LOCK: false` on line 9. Flip it to `true` the moment the tool gains real business logic — the factory will then leave it alone on `factory-reset`. If you add new YAML fields under a locked tool's substep, manually update the tool's `_los()` / `_doc()` reads.

### Workflow when editing YAML

1. Edit `definitions/step_NN_*.yaml` (or `_agent.yaml`).
2. Run `python3.11 -m factory factory-reset` (or `update-agent` for config-only changes).
3. Confirm `Validation: PASSED`.
4. Spot-check `output/config/fields_config.json` if you added new field IDs.
5. For any new field IDs, validate live against Encompass via `encompass_client.py` before relying on them.

---

## State schema

The agent state (`output/proc_agent.py:ProcessorAgentState`) tracks:

- **Inputs:** `loan_number`, `env`, `almas_notes`, `processor_name`, `additional_info`, `force_extract`
- **Core data:** `loan_id`, `los_fields`, `doc_fields`, `efolder_documents`, `loan_summary`, `loan_profile`
- **Issues:** `flags` (deduped by `(substep, title)`), `pending_field_updates`
- **Progress:** `current_step`, `current_substep`, `workflow_plan`, `dynamic_skipping`, `step_reports`, `step_fullReports`
- **Audit:** `field_writes_ledger`, `notes`, `conversation_summary`
- **Messages:** auto-truncated tool messages over `MAX_TOOL_RESULT_SIZE` (15000 chars)

Reducers (`merge_dicts`, `dedupe_flags`, `append_list`, `last_value_reducer`, `truncate_messages`) keep state coherent across concurrent tool calls.

### Middleware

- `ProcessorAgentMiddleware` — binds the state schema
- `SystemMessageNormalizerMiddleware` — moves all system messages to the front before each model call
- `WorkflowGuardMiddleware` — prevents premature termination on text-only responses (nudges with retries) and enforces a per-substep timeout (10 minutes default) via `interrupt`
- `CopilotKitMiddleware` — added when `copilotkit` is installed

---

## Conventions and rules

- **`loan_id` resolution** — Every tool that needs a loan GUID must guard against a missing `loan_id` in state and never trust an LLM-supplied parameter:

  ```python
  loan_id = state.get("loan_id")
  if not loan_id:
      return Command(update={"messages": [ToolMessage(
          content=json.dumps({"error": "No loan_id in state. Run data_gathering first."}),
          tool_call_id=tool_call_id,
      )]})
  ```

- **Adding a tool** — When adding or renaming a tool, the factory updates `output/tools/*.py`, `output/tools/__init__.py`, `output/config/workflow_config.json`, and `output/plans/*.md` automatically. For hand-written tools, also update `output/registry.py` (`STEP_XX.tools` and `SubstepConfig`).

- **Field IDs** — Adding/changing an Encompass field ID requires `factory validate` to pass and a live read against a Test loan via `encompass_client.py` before relying on it.

See `.cursor/rules/` for the full set of project conventions.

---

## License

MIT — see [LICENSE](LICENSE).
