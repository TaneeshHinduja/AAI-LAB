"""Safe, unsent notification drafts for the recruitment workflow."""

def generate_notification(candidate: dict, status: str) -> dict:
    name = candidate.get("candidate_name", "Candidate")
    ref = candidate.get("candidate_id", "application")
    messages = {
        "SHORTLIST_REVIEW": ("Application update", f"Dear {name},\n\nYour application ({ref}) is moving to the next stage of review. The recruitment team will share any next steps after human review."),
        "MANUAL_REVIEW": ("Application under review", f"Dear {name},\n\nYour application ({ref}) is currently under review. The recruitment team will contact you if further information or an update is needed."),
        "NOT_SHORTLISTED_REVIEW": ("Application review update", f"Dear {name},\n\nYour application ({ref}) is being reviewed by the recruitment team. This is not a final decision; you will be informed after the review is complete."),
    }
    subject, message = messages.get(status, ("Application update", f"Dear {name},\n\nYour application ({ref}) requires additional review."))
    return {"candidate_id": ref, "subject": subject, "message": message, "status": "DRAFT", "sent": False}
