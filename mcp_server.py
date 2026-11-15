"""Recruitment workflow MCP server. All tools operate on local synthetic data."""
from __future__ import annotations
import csv, json, re
from pathlib import Path
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP
from notifications import generate_notification

ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "candidates.csv"
RULES = ROOT / "rules.json"
mcp = FastMCP("Automated Recruitment Workflow Server")
REQUIRED = ("candidate_id", "candidate_name", "email", "skills_score", "experience_score", "assessment_score", "application_role")

def load_rules():
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    if sum(rules["criteria"].values()) != 100 or set(rules["criteria"]) != {"skills_score", "experience_score", "assessment_score"}:
        raise ValueError("Scoring weights must contain the three configured criteria and total 100")
    return rules

def load_candidates():
    with DATA.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not set(REQUIRED).issubset(reader.fieldnames or []):
            raise ValueError("Candidate CSV is missing required headers")
        return list(reader)

def evaluate_rows(rows=None):
    rows = load_candidates() if rows is None else rows
    rules = load_rules(); seen = set(); out = []
    for raw in rows:
        c = {str(k): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
        errors = []
        for key in REQUIRED:
            if not str(c.get(key, "")).strip(): errors.append(f"Missing field: {key}")
        cid = c.get("candidate_id", "")
        if cid and cid in seen: errors.append("Duplicate candidate_id")
        elif cid: seen.add(cid)
        scores = {}
        for key in rules["criteria"]:
            try: value = float(c.get(key, ""))
            except (ValueError, TypeError): errors.append(f"Invalid numeric value: {key}"); continue
            if not 0 <= value <= 100: errors.append(f"{key} must be between 0 and 100")
            else: scores[key] = value
        if c.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", c["email"]): errors.append("Invalid email format")
        valid = not errors; score = None; breakdown = {}; status = "MANUAL_REVIEW"
        if valid:
            raw_breakdown = {k: scores[k] * weight / 100 for k, weight in rules["criteria"].items()}
            breakdown = {k: round(value, 4) for k, value in raw_breakdown.items()}
            score = round(sum(raw_breakdown.values()), 2)
            status = "SHORTLIST_REVIEW" if score >= rules["thresholds"]["shortlist_review"] else "MANUAL_REVIEW" if score >= rules["thresholds"]["manual_review"] else "NOT_SHORTLISTED_REVIEW"
        out.append({"candidate_id": cid, "candidate_name": c.get("candidate_name", ""), "email": c.get("email", ""), "valid": valid, "errors": errors, "score": score, "breakdown": breakdown, "rule_version": rules["rule_version"], "proposed_status": status, "notification": generate_notification(c, status) if valid else None, "_source": c})
    return out

def persist_batch(results):
    """Refresh the quarantine snapshot and append immutable per-run evaluation events."""
    rejected_path = ROOT / "rejected_records.csv"
    rejects = [r for r in results if not r["valid"]]
    with rejected_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["candidate_id", "candidate_name", "email", "skills_score", "experience_score", "assessment_score", "application_role", "rejection_reason"]
        writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
        for item in rejects:
            original = item["_source"]
            writer.writerow({**{k: original.get(k, "") for k in fields[:-1]}, "rejection_reason": "; ".join(item["errors"])})
    path = ROOT / "audit_log.csv"; exists = path.exists()
    fields = ["timestamp", "candidate_id", "validation_status", "score", "score_breakdown", "rule_version", "proposed_status", "reviewer_action", "final_status", "notification_status", "error_details", "reviewer", "review_notes"]
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists or path.stat().st_size == 0: writer.writeheader()
        now = datetime.now(timezone.utc).isoformat()
        for item in results:
            writer.writerow({"timestamp":now,"candidate_id":item["candidate_id"],"validation_status":"VALID" if item["valid"] else "INVALID","score":item["score"],"score_breakdown":json.dumps(item["breakdown"],separators=(",",":")),"rule_version":item["rule_version"],"proposed_status":item["proposed_status"],"reviewer_action":"PENDING","final_status":"PENDING_HUMAN_REVIEW","notification_status":"DRAFT" if item["valid"] else "NOT_SENT","error_details":"; ".join(item["errors"]),"reviewer":"","review_notes":""})

@mcp.tool()
def get_dataset_summary() -> dict:
    """Return dataset size and validation/routing counts without exposing contact details."""
    results = evaluate_rows(); return {"total": len(results), "valid": sum(x["valid"] for x in results), "invalid": sum(not x["valid"] for x in results), "routing": {s: sum(x["valid"] and x["proposed_status"] == s for x in results) for s in ("SHORTLIST_REVIEW", "MANUAL_REVIEW", "NOT_SHORTLISTED_REVIEW")}}

@mcp.tool()
def process_candidate_batch() -> dict:
    """Process the full CSV, save rejected records with reasons, and append audit events."""
    results = evaluate_rows(); persist_batch(results)
    return {"total":len(results),"valid":sum(x["valid"] for x in results),"invalid":sum(not x["valid"] for x in results),"routing":{s:sum(x["valid"] and x["proposed_status"]==s for x in results) for s in ("SHORTLIST_REVIEW","MANUAL_REVIEW","NOT_SHORTLISTED_REVIEW")},"rejected_records":"rejected_records.csv","audit_log":"audit_log.csv","audit_events_appended":len(results)}

@mcp.tool()
def run_batch_checks() -> dict:
    """Run deterministic workflow checks for validation, thresholds, drafts, and human-review safeguards."""
    checks = []
    def add(name, ok, observed, expected): checks.append({"name": name, "passed": bool(ok), "observed": observed, "expected": expected})
    rules = load_rules()
    for score, expected in [(75, "SHORTLIST_REVIEW"), (74.99, "MANUAL_REVIEW"), (50, "MANUAL_REVIEW"), (49.99, "NOT_SHORTLISTED_REVIEW")]:
        boundary = {"candidate_id":"BND","candidate_name":"Boundary Example","email":"boundary@example.test","skills_score":score,"experience_score":score,"assessment_score":score,"application_role":rules["job_role"]}
        evaluated = evaluate_rows([boundary])[0]
        observed = {"score":evaluated["score"],"status":evaluated["proposed_status"]}
        add(f"Boundary score {score}", observed["status"] == expected, observed, {"score":score,"status":expected})
    sample = {"candidate_id":"T001","candidate_name":"Test Candidate","email":"test@example.org","skills_score":"85","experience_score":"70","assessment_score":"90","application_role":rules["job_role"]}
    add("Weighted score example", evaluate_rows([sample])[0]["score"] == 82, evaluate_rows([sample])[0]["score"], 82)
    missing = {**sample, "email":""}; add("Missing email quarantined", not evaluate_rows([missing])[0]["valid"], evaluate_rows([missing])[0]["errors"], "invalid with reason")
    duplicate = evaluate_rows([sample, sample]); add("Duplicate ID flagged", duplicate[0]["valid"] and not duplicate[1]["valid"], duplicate[1]["errors"], "Duplicate candidate_id")
    draft = generate_notification(sample, "NOT_SHORTLISTED_REVIEW"); add("Notification is draft only", draft["status"] == "DRAFT" and not draft["sent"] and "not a final decision" in draft["message"], draft["status"], "DRAFT; not sent")
    add("Human review required before dispatch", True, "No sending tool is exposed", "No automatic external communication")
    try: review_candidate("C0001", "APPROVE", "CANDIDATE", "unauthorized test")
    except ValueError as error: denied = "Unauthorized" in str(error)
    else: denied = False
    add("Unauthorized reviewer denied", denied, "Unauthorized role check", "Action rejected before audit mutation")
    return {"total": len(checks), "passed": sum(c["passed"] for c in checks), "failed": sum(not c["passed"] for c in checks), "checks": checks, "timestamp": datetime.now(timezone.utc).isoformat()}

@mcp.tool()
def evaluate_candidate(candidate_id: str) -> dict:
    """Evaluate one synthetic candidate by ID and return score explanation plus a DRAFT notification."""
    if not candidate_id or len(candidate_id) > 40: raise ValueError("candidate_id must be 1–40 characters")
    found = [x for x in evaluate_rows() if x["candidate_id"] == candidate_id]
    if not found: raise ValueError("Candidate not found")
    item = dict(found[0]); item.pop("_source", None)
    return item

@mcp.tool()
def review_candidate(candidate_id: str, action: str, reviewer: str = "HR_REVIEWER", notes: str = "") -> dict:
    """Record a simulated human review decision. Does not send communication."""
    if reviewer not in {"HR_REVIEWER", "HR_ADMIN"}: raise ValueError("Unauthorized reviewer role")
    if action not in {"APPROVE", "EDIT", "HOLD", "REQUEST_INFO"}: raise ValueError("Invalid review action")
    item = evaluate_candidate(candidate_id)
    final = item["proposed_status"] if action == "APPROVE" else {"EDIT":"REQUIRES_REVIEW", "HOLD":"ON_HOLD", "REQUEST_INFO":"AWAITING_INFORMATION"}[action]
    row = {"timestamp":datetime.now(timezone.utc).isoformat(), "candidate_id":candidate_id, "validation_status":"VALID", "score":item["score"], "score_breakdown":json.dumps(item["breakdown"],separators=(",",":")), "rule_version":item["rule_version"], "proposed_status":item["proposed_status"], "reviewer_action":action, "final_status":final, "notification_status":"NOT_SENT", "error_details":"", "reviewer":reviewer, "review_notes":notes[:500]}
    path = ROOT / "audit_log.csv"; exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        fields = ["timestamp", "candidate_id", "validation_status", "score", "score_breakdown", "rule_version", "proposed_status", "reviewer_action", "final_status", "notification_status", "error_details", "reviewer", "review_notes"]
        writer = csv.DictWriter(f, fieldnames=fields);
        if not exists: writer.writeheader()
        writer.writerow(row)
    return row

if __name__ == "__main__": mcp.run(transport="stdio")
