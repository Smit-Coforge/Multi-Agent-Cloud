"""
This module compares an applicant to outside driving records.

Responsibilities:
- Load the motor vehicle record, claims, vehicle record, and territory.
- Ask the model for nondisclosure findings and a rate assessment.
- Reject a finding or a surcharge flag that does not match the loaded record.
"""

from __future__ import annotations

import json
from typing import Any

from src.llm import parse_json_object
from src.state import ALLOWED_VIOLATIONS, SEVERE_VIOLATIONS, canonical_violation
from src.tools.enrichment_lookup import lookup_enrichment

_LABELS = ", ".join(ALLOWED_VIOLATIONS)
_ACCIDENT_EVIDENCE = "at_fault_accidents"

ENRICH_SYSTEM = f"""You compare the motor vehicle record to what the driver already disclosed. Use only the user JSON.
Return JSON only, with exactly two keys and no others:
nondisclosure_findings (array of objects) and rate_assessment (object).
Do not return the MVR, claims, vehicle, or territory. Code copies those from the file.
disclosed_violations is already a list of standardized labels. Do not remap the application.
Claims are context. Do not cite a claim as evidence.

nondisclosure_findings:
Emit one finding for each label in undisclosed_violations, and no other labels.
Each object has evidence_type, evidence, suggests, and explanation.
evidence_type is mvr.
evidence must be copied from the MVR. Do not invent a violation.
- For a label that appears in mvr.violations, evidence is that string copied as written.
- For at_fault_accident, evidence is the string at_fault_accidents.
suggests is that same label. It must be one of:
{_LABELS}
explanation is one sentence tying that evidence to the undisclosed violation.
If undisclosed_violations is empty, nondisclosure_findings is an empty array.

rate_assessment:
surcharge must be exactly the boolean surcharge_required. Copy it. Do not recompute it and do not write the other value.
reason is one sentence about the price.
A true surcharge means the policy can still be issued at a higher price. It is not by itself a decline."""


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _labels(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {
        label
        for item in values
        if (label := canonical_violation(item))
    }


def _mvr(looked_up: dict[str, Any]) -> dict[str, Any]:
    mvr = looked_up.get("mvr") or {}
    return mvr if isinstance(mvr, dict) else {}


def accident_gap(applicant: dict[str, Any], looked_up: dict[str, Any]) -> bool:
    on_file = _as_int(_mvr(looked_up).get("at_fault_accidents"))
    disclosed = _as_int(applicant.get("accidents_disclosed"))
    return on_file > disclosed


def undisclosed_violations(applicant: dict[str, Any], looked_up: dict[str, Any]) -> list[str]:
    """MVR violations and at-fault gaps the application did not disclose."""
    disclosed = _labels(applicant.get("violations"))
    on_file = _labels(_mvr(looked_up).get("violations"))
    required = on_file - disclosed
    if accident_gap(applicant, looked_up):
        required.add("at_fault_accident")
    return [label for label in ALLOWED_VIOLATIONS if label in required]


def expected_surcharge(applicant: dict[str, Any], looked_up: dict[str, Any]) -> bool:
    """True when the record should raise the price."""
    territory = looked_up.get("territory") or {}
    mvr = _mvr(looked_up)
    if isinstance(territory, dict) and territory.get("risk_band") == "high":
        return True
    if mvr.get("license_status") != "valid":
        return True
    if _as_int(mvr.get("at_fault_accidents")) >= 2:
        return True
    if _as_int(applicant.get("annual_mileage")) >= 25_000:
        return True
    labels = _labels(applicant.get("violations")) | _labels(mvr.get("violations"))
    return any(label in labels for label in SEVERE_VIOLATIONS)


def _findings(
    raw: Any,
    looked_up: dict[str, Any],
    applicant: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("nondisclosure_findings must be a list")  # noqa: TRY004
    on_file = [
        item
        for item in (_mvr(looked_up).get("violations") or [])
        if isinstance(item, str)
    ]
    gap = accident_gap(applicant, looked_up)
    disclosed = _labels(applicant.get("violations"))
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("nondisclosure finding must be an object")  # noqa: TRY004
        evidence_type = item.get("evidence_type")
        evidence = item.get("evidence")
        suggests = canonical_violation(item.get("suggests"))
        explanation = item.get("explanation")
        if evidence_type != "mvr":
            raise ValueError(f"invalid evidence_type: {evidence_type!r}")
        if not isinstance(evidence, str):
            raise ValueError(f"evidence not in mvr: {evidence!r}")  # noqa: TRY004
        if suggests is None:
            raise ValueError(f"suggests not allowed: {item.get('suggests')!r}")
        if evidence == _ACCIDENT_EVIDENCE:
            if not gap:
                raise ValueError(f"evidence not in mvr: {evidence!r}")
            if suggests != "at_fault_accident":
                raise ValueError("suggests does not match mvr evidence")
        elif evidence not in on_file:
            raise ValueError(f"evidence not in mvr: {evidence!r}")
        elif suggests != canonical_violation(evidence):
            raise ValueError("suggests does not match mvr evidence")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("finding explanation must be a non-empty string")
        if suggests in disclosed:
            continue
        if suggests == "at_fault_accident" and not gap:
            continue
        if suggests in seen:
            raise ValueError("duplicate finding")
        seen.add(suggests)
        findings.append(
            {
                "evidence_type": "mvr",
                "evidence": evidence,
                "suggests": suggests,
                "explanation": explanation.strip(),
            }
        )
    required = set(undisclosed_violations(applicant, looked_up))
    if seen != required:
        missing = required - seen
        raise ValueError(
            "finding omitted for an undisclosed record: " + ", ".join(sorted(missing))
        )
    return findings


def _rate(raw: Any, expected: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("rate_assessment must be an object")  # noqa: TRY004
    surcharge = raw.get("surcharge")
    reason = raw.get("reason")
    if not isinstance(surcharge, bool):
        raise ValueError("rate_assessment.surcharge must be a bool")  # noqa: TRY004
    if surcharge is not expected:
        raise ValueError("surcharge does not match the record")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("rate_assessment.reason must be a non-empty string")
    return {"surcharge": surcharge, "reason": reason.strip()}


def enrichment_agent(applicant: dict[str, Any], *, case_id: str, llm: Any) -> dict[str, Any]:
    looked_up = lookup_enrichment(case_id)
    disclosed = [
        label
        for item in (applicant.get("violations") or [])
        if (label := canonical_violation(item))
    ]
    surcharge_required = expected_surcharge(applicant, looked_up)
    payload = {
        "disclosed_violations": disclosed,
        "accidents_disclosed": applicant.get("accidents_disclosed"),
        "undisclosed_violations": undisclosed_violations(applicant, looked_up),
        "surcharge_required": surcharge_required,
        "age": applicant.get("age"),
        "zip_code": applicant.get("zip_code"),
        "years_licensed": applicant.get("years_licensed"),
        "annual_mileage": applicant.get("annual_mileage"),
        "coverage_level": applicant.get("coverage_level"),
        "vehicle_year": applicant.get("vehicle_year"),
        "vehicle_make": applicant.get("vehicle_make"),
        "vehicle_model": applicant.get("vehicle_model"),
        "driving_record_text": applicant.get("driving_record_text"),
        **looked_up,
    }
    content = llm.complete(
        [
            {"role": "system", "content": ENRICH_SYSTEM},
            {"role": "user", "content": json.dumps(payload)},
        ],
        agent="enrichment",
    )
    data = parse_json_object(content)
    return {
        **looked_up,
        "nondisclosure_findings": _findings(
            data.get("nondisclosure_findings"), looked_up, applicant
        ),
        "rate_assessment": _rate(data.get("rate_assessment"), surcharge_required),
    }
