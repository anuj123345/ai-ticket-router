"""
Ticket Classifier — uses the LLM to classify a ticket into a department
and assign a priority level. Returns structured data.
"""

import json
import re
from services.llm_client import chat

DEPARTMENTS = ["IT", "HR", "Finance", "Engineering", "General"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]

SYSTEM_PROMPT = """You are an intelligent ticket routing system for a tech company.
Your job is to classify incoming support tickets and route them to the correct department.

Departments:
- IT: Password resets, VPN, hardware, software installation, email, access issues, security incidents
- HR: Time off, payroll, benefits, onboarding, performance reviews, policies, conduct
- Finance: Invoices, expense reports, purchase orders, budgets, reimbursements, contractor payments
- Engineering: Code issues, deployment problems, on-call incidents, access to repos/infra, bugs
- General: Anything that doesn't clearly fit the above

Priority levels:
- Critical: System is down, security breach, blocks many people, payroll error
- High: Blocks a single person's work, time-sensitive (due today/tomorrow)
- Medium: Important but not urgent, needed this week
- Low: General questions, non-urgent requests

Respond ONLY with valid JSON in this exact format:
{
  "department": "<one of: IT, HR, Finance, Engineering, General>",
  "priority": "<one of: Low, Medium, High, Critical>",
  "confidence": <number 0.0–1.0>,
  "reasoning": "<one sentence explaining the classification>"
}"""


def classify_ticket(subject: str, description: str) -> dict:
    """
    Classify a ticket by department and priority.

    Returns:
        {
            "department": str,
            "priority": str,
            "confidence": float,
            "reasoning": str
        }
    """
    user_message = f"Subject: {subject}\n\nDescription: {description}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    raw = chat(messages, temperature=0.1, max_tokens=256)

    # Extract JSON even if the model wraps it in markdown code blocks
    json_match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group())
            # Validate fields
            if result.get("department") not in DEPARTMENTS:
                result["department"] = "General"
            if result.get("priority") not in PRIORITIES:
                result["priority"] = "Medium"
            result["confidence"] = float(result.get("confidence", 0.8))
            return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback if parsing fails
    return {
        "department": "General",
        "priority": "Medium",
        "confidence": 0.5,
        "reasoning": "Could not parse classification. Defaulted to General/Medium.",
    }


def get_assignee(department: str) -> dict:
    """Return the team contact info for a department."""
    assignees = {
        "IT": {"team": "IT Support", "email": "it-support@company.com", "slack": "#it-help"},
        "HR": {"team": "People Ops", "email": "hr@company.com", "slack": "#hr-questions"},
        "Finance": {"team": "Finance", "email": "finance@company.com", "slack": "#finance-help"},
        "Engineering": {"team": "Engineering On-Call", "email": "eng-oncall@company.com", "slack": "#eng-support"},
        "General": {"team": "General Support", "email": "support@company.com", "slack": "#general-help"},
    }
    return assignees.get(department, assignees["General"])
