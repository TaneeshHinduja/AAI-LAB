# Automated Recruitment Workflow System · Experiment 07

A local classroom simulation of an auditable HR workflow. The browser page uses your local Ollama `qwen2.5:7b-instruct` model for request interpretation and explanations. All validation, weighted scoring, routing, candidate lookup, review logging, and batch checks run through deterministic tools exposed by a real FastMCP server over stdio. There is no cloud model or external HR/email API.

## Start

Requirements: Python 3.10+, Ollama, and the local `qwen2.5:7b-instruct` model.

```powershell
ollama pull qwen2.5:7b-instruct
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python server.py
```

Open http://127.0.0.1:8001. The web server starts and manages the MCP server subprocess; do not start `mcp_server.py` separately. Configure `OLLAMA_URL`, `OLLAMA_MODEL`, or `PORT` if your local Ollama endpoint/model tag or workbench port differs. Deterministic MCP functions remain available when Ollama is offline, with the model explanation marked unavailable.

## Workbench pages

- **Guided workflow:** numbered, sequentially revealed workflow stages, observed MCP result, and local Qwen explanation.
- **Batch tests:** 22 reusable test queries grouped by dataset, validation, scoring, notifications, human review, audit, reliability, and security. The adversarial suite checks prompt injection attempts to trigger automatic approval or dispatch, disclose contact details, and use protected traits. Run the visible selection or whole suite; every query displays the actual MCP call result and timestamp.
- **Dataset & results:** actual row validity and routing counts returned through MCP.
- **Method & safeguards:** scoring, routing boundaries, workflow stages, tools, and lab limitations.

## Dataset

`candidates.csv` contains 1,000 fictional applications generated with a fixed seed. It includes exact overall-score boundaries of 75, 74.99, 50, and 49.99, high/low scores, missing and malformed email, out-of-range and nonnumeric values, a blank name, and duplicate IDs. Rows are not removed silently: the first application is evaluated and duplicate occurrences are flagged. Regenerate with `python scripts/generate_dataset.py`.

## Criteria and safeguards

`rules.json` holds weights (skills 40%, relevant experience 30%, technical assessment 30%) and review thresholds. The system returns criterion contributions and rule version. Invalid records are not scored. Every route remains a human review queue, including `NOT_SHORTLISTED_REVIEW`; no automated rejection occurs. The notification module creates DRAFT text only. A simulated reviewer action is appended to `audit_log.csv` with reviewer role, timestamp, notes, and `NOT_SENT` outcome. No live dispatch tool or HR integration is provided.

Candidate information is synthetic and uses reserved `example.test` emails. Prompt-injection checks exercise specific known patterns and do not prove resistance to every possible adversarial prompt. This is a software demonstration, not evidence that hiring criteria are valid or fair. Review performance metrics and score consistency do not establish fairness or hiring quality.

## Architecture

```text
Browser UI → local HTTP workbench → local Ollama Qwen (interpretation/explanation)
                            └── MCP ClientSession / stdio
                                  ↕ JSON-RPC
                                FastMCP server → CSV + rules.json
                                  ├── validate / score / route / draft
                                  ├── human review record → audit_log.csv
                                  └── deterministic batch checks
```
