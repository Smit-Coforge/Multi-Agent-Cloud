"""
This module scores how serious an auto insurance file is.

Responsibilities:
- Ask the model for a score, band, factors, and an underwriter note.
- Reject a score outside 0 to 1, a band that does not match that score, or empty text.
- Reject a factor that names a DUI when the file has none.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.llm import parse_json_object
from src.state import SEVERE_VIOLATIONS, canonical_violation

RISK_SYSTEM = (
    "You are an auto insurance underwriter. Judge this driver and car as a whole. "
    "Return JSON only, with exactly these keys: score, band, factors, underwriter_note. "
    "score is a number from 0 to 1 that matches how serious this file is. "
    "band is low when score < 0.4, medium when score <= 0.7, and high otherwise. "
    "Do not default to a low score. The note and the factors must agree with the score. "
    "factors is a non-empty list of short strings for facts that are actually on this file. "
    "Do not name a violation the file does not contain. "
    "underwriter_note is one short sentence. "
    "Weigh age, years licensed, mileage, vehicle, territory, violations, "
    "at-fault accidents, undisclosed record items, license status, and the rate assessment. "
    "A consistency flag is an example to consider, not an automatic debit. "
    "Use only the user JSON. Do not invent tickets or accidents."
)

_DUI = re.compile(r"\bdui\b", re.IGNORECASE)


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _labels(*groups: Any) -> set[str]:
    labels: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict):
                item = item.get("suggests")
            label = canonical_violation(item)
            if label:
                labels.add(label)
    return labels


def _violation_groups(applicant: dict[str, Any], enrichment: dict[str, Any]) -> set[str]:
    mvr = enrichment.get("mvr") or {}
    violations = mvr.get("violations") if isinstance(mvr, dict) else []
    return _labels(
        applicant.get("violations"),
        applicant.get("violations_disclosed"),
        violations,
        enrichment.get("nondisclosure_findings"),
    )


def compute_auto_score(
    applicant: dict[str, Any],
    enrichment: dict[str, Any],
) -> tuple[float, str, list[str]]:
    score = 0.10
    factors: list[str] = []
    labels = _violation_groups(applicant, enrichment)
    counted = labels - {"at_fault_accident"}

    age = _as_int(applicant.get("age"))
    if age and age < 25:
        score += 0.12
        factors.append(f"Age {age} (under 25)")

    years = _as_int(applicant.get("years_licensed"))
    if years < 3:
        score += 0.10
        factors.append(f"{years} year(s) licensed")

    mileage = _as_int(applicant.get("annual_mileage"))
    if mileage >= 20_000:
        score += 0.08
        factors.append(f"{mileage} miles a year")

    if counted:
        score += min(0.08 * len(counted), 0.24)
        factors.append(f"{len(counted)} violation(s) on the record")

    if any(label in labels for label in SEVERE_VIOLATIONS):
        score += 0.25
        factors.append("Severe violation on record")

    mvr = enrichment.get("mvr") or {}
    accidents = _as_int(mvr.get("at_fault_accidents")) if isinstance(mvr, dict) else 0
    if accidents >= 1:
        score += 0.15
        factors.append("At-fault accident on record")
    if accidents >= 2:
        score += 0.10
        factors.append("Repeat at-fault accidents")

    if isinstance(mvr, dict) and mvr.get("license_status") not in (None, "valid"):
        score += 0.20
        factors.append("License is not valid")

    territory = enrichment.get("territory") or {}
    band = territory.get("risk_band") if isinstance(territory, dict) else None
    if band == "high":
        score += 0.12
        factors.append("High-risk territory")
    elif band == "medium":
        score += 0.05
        factors.append("Medium-risk territory")

    findings = enrichment.get("nondisclosure_findings") or []
    if isinstance(findings, list) and findings:
        score += 0.12
        factors.append("Undisclosed driving record")

    assessment = enrichment.get("rate_assessment") or {}
    if isinstance(assessment, dict) and assessment.get("surcharge") is True:
        score += 0.10
        factors.append("Surcharge applies")

    flags = applicant.get("consistency_flags") or []
    if isinstance(flags, list) and flags:
        score += min(0.08 * len(flags), 0.16)
        factors.append(f"{len(flags)} consistency flag(s)")

    score = min(1.0, max(0.0, round(score, 2)))
    if not factors:
        factors.append("No extra driving or territory debits")
    return score, band_for_score(score), factors


def band_for_score(score: float) -> str:
    if score < 0.4:
        return "low"
    if score <= 0.7:
        return "medium"
    return "high"


def _file_has_dui(applicant: dict[str, Any], enrichment: dict[str, Any]) -> bool:
    return "dui" in _violation_groups(applicant, enrichment)


def _rejects_dui_when_absent(
    applicant: dict[str, Any],
    enrichment: dict[str, Any],
    factors: list[str],
) -> None:
    if _file_has_dui(applicant, enrichment):
        return
    for factor in factors:
        if _DUI.search(factor):
            raise ValueError("factor mentions dui when the file has no dui")


def _model_score(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("score must be a number")  # noqa: TRY004
    score = float(raw)
    if score < 0 or score > 1:
        raise ValueError("score must be from 0 to 1")
    return score


def _model_factors(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("factors must be a non-empty list")
    factors: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("factors must be non-empty strings")
        factors.append(item.strip())
    return factors


def risk_scoring_agent(
    applicant: dict[str, Any],
    enrichment: dict[str, Any],
    *,
    case_id: str,
    llm: Any,
) -> dict[str, Any]:
    content = llm.complete(
        [
            {"role": "system", "content": RISK_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"applicant": applicant, "enrichment": enrichment}
                ),
            },
        ],
        agent="risk",
    )
    data = parse_json_object(content)
    score = _model_score(data.get("score"))
    band = data.get("band")
    if band != band_for_score(score):
        raise ValueError("band does not match score")
    note = data.get("underwriter_note")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("underwriter_note must be a non-empty string")
    factors = _model_factors(data.get("factors"))
    _rejects_dui_when_absent(applicant, enrichment, factors)
    return {
        "score": score,
        "band": band,
        "factors": factors,
        "underwriter_note": note.strip(),
    }
