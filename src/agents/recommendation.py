"""
This module chooses an underwriting outcome for a finished file.

Responsibilities:
- Ask the model for approve, refer, or deny, plus a reason.
- Require the rate class from the assessment on approve, and a review checklist on refer.
- Reject an approve when the risk score is above 0.7, or any outcome but refer when a consistency flag requires review.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.llm import parse_json_object
from src.state import SEVERE_VIOLATIONS, canonical_violation

RECOMMENDATION_SYSTEM = (
    "You are an auto insurance underwriter. Choose the outcome from the whole file, "
    "not from a score cutoff. Read the driver, the risk note, consistency flags, "
    "nondisclosure findings, and the rate assessment. "
    "approve when the driver and car can be issued. "
    "deny when the file should not be issued. "
    "refer when a person should look at something specific. "
    "When referral_required is true, outcome must be refer. "
    "That means the file has a consistency flag and no severe violation, "
    "so a person has to check that flag. Put the flag on the review checklist. "
    "Do not approve a file whose risk score is above 0.7. Choose refer or deny for that file. "
    "The reason must agree with the risk score. Do not call a high score low. "
    "Do not say a score of 0.7 or below is above 0.7. Explain the file, not the cutoff. "
    "Return JSON only. "
    "approve has outcome, reason, and rate_class. "
    "rate_class must be exactly rate_class_required. Do not choose a different class. "
    "deny has outcome and reason, and no rate_class. "
    "refer has outcome, reason, and review_checklist, a short list of concrete items "
    "for the human underwriter, and no rate_class. "
    "reason is one sentence. Use only the user JSON. Do not invent tickets or accidents."
)

_OUTCOMES = ("approve", "refer", "deny")
_RATE_CLASSES = ("standard", "surcharge")
_CLAIMS_ABOVE_CUTOFF = re.compile(r"above (?:the )?0\.7|higher than (?:the )?0\.7|over (?:the )?0\.7")


def pick_outcome(score: float) -> str:
    if score < 0.4:
        return "approve"
    if score <= 0.7:
        return "refer"
    return "deny"


def _risk_score(risk: dict[str, Any]) -> float:
    score = risk.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("risk score must be a number")  # noqa: TRY004
    return float(score)


def _labels(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {label for item in values if (label := canonical_violation(item))}


def _referral_required(applicant: dict[str, Any], enrichment: dict[str, Any]) -> bool:
    flags = applicant.get("consistency_flags") or []
    if not isinstance(flags, list) or not flags:
        return False
    mvr = enrichment.get("mvr") or {}
    on_file = mvr.get("violations") if isinstance(mvr, dict) else []
    labels = _labels(applicant.get("violations")) | _labels(on_file)
    return not any(label in labels for label in SEVERE_VIOLATIONS)


def _expected_rate_class(enrichment: dict[str, Any]) -> str:
    assessment = enrichment.get("rate_assessment") or {}
    if isinstance(assessment, dict) and assessment.get("surcharge") is True:
        return "surcharge"
    return "standard"


def _checklist(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("review_checklist must be a non-empty list")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("review_checklist items must be non-empty strings")
        items.append(item.strip())
    return items


def recommendation_agent(
    applicant: dict[str, Any],
    enrichment: dict[str, Any],
    risk: dict[str, Any],
    *,
    case_id: str,
    llm: Any,
) -> dict[str, Any]:
    driver = {
        "name": applicant.get("name"),
        "age": applicant.get("age"),
        "zip_code": applicant.get("zip_code"),
        "years_licensed": applicant.get("years_licensed"),
        "vehicle_year": applicant.get("vehicle_year"),
        "vehicle_make": applicant.get("vehicle_make"),
        "vehicle_model": applicant.get("vehicle_model"),
        "annual_mileage": applicant.get("annual_mileage"),
        "coverage_level": applicant.get("coverage_level"),
        "violations": applicant.get("violations") or [],
        "accidents_disclosed": applicant.get("accidents_disclosed"),
    }
    if applicant.get("driving_record_text"):
        driver["driving_record_text"] = applicant.get("driving_record_text")
    referral_required = _referral_required(applicant, enrichment)
    content = llm.complete(
        [
            {"role": "system", "content": RECOMMENDATION_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "risk": risk,
                        "consistency_flags": applicant.get("consistency_flags") or [],
                        "nondisclosure_findings": enrichment.get("nondisclosure_findings")
                        or [],
                        "rate_assessment": enrichment.get("rate_assessment") or {},
                        "referral_required": referral_required,
                        "rate_class_required": _expected_rate_class(enrichment),
                        "driver": driver,
                    }
                ),
            },
        ],
        agent="recommendation",
    )
    data = parse_json_object(content)
    outcome = data.get("outcome")
    if outcome not in _OUTCOMES:
        raise ValueError("outcome must be approve, refer, or deny")
    if outcome == "approve" and _risk_score(risk) > 0.7:
        raise ValueError("cannot approve a high risk score")
    if referral_required and outcome != "refer":
        raise ValueError("consistency flags require refer")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    if _risk_score(risk) <= 0.7 and _CLAIMS_ABOVE_CUTOFF.search(reason.casefold()):
        raise ValueError("reason contradicts the risk score")
    decision: dict[str, Any] = {"outcome": outcome, "reason": reason.strip()}
    if outcome == "approve":
        rate_class = data.get("rate_class")
        if rate_class not in _RATE_CLASSES:
            raise ValueError("rate_class must be standard or surcharge")
        if rate_class != _expected_rate_class(enrichment):
            raise ValueError("rate_class does not match the rate assessment")
        decision["rate_class"] = rate_class
    if outcome == "refer":
        decision["review_checklist"] = _checklist(data.get("review_checklist"))
    return decision
