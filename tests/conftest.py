"""
This module builds the shared applicants and canned model replies used by tests.

Responsibilities:
- Define the Alex Rivera, Jordan Blake, and Casey Morgan applications.
- Build a FakeLLM whose replies match those applicants and their records.
- Skip the retry wait so a failing agent does not sleep.
"""

from __future__ import annotations

from typing import Any

import pytest

from src import tracing
from src.agents.enrichment import expected_surcharge, undisclosed_violations
from src.agents.intake import required_flag_fields
from src.agents.recommendation import pick_outcome
from src.agents.risk import compute_auto_score
from src.llm import FakeLLM
from src.tools.enrichment_lookup import lookup_enrichment

ALEX_RAW: dict[str, Any] = {
    "name": "Alex Rivera",
    "age": 34,
    "zip_code": "94110",
    "years_licensed": 16,
    "vehicle_year": 2019,
    "vehicle_make": "Honda",
    "vehicle_model": "Civic",
    "annual_mileage": 8000,
    "coverage_level": "full",
    "violations_disclosed": [],
    "accidents_disclosed": 0,
}

ALEX: dict[str, Any] = {**ALEX_RAW, "violations": [], "consistency_flags": []}

JORDAN_RAW: dict[str, Any] = {
    "name": "Jordan Blake",
    "age": 22,
    "zip_code": "48201",
    "years_licensed": 4,
    "vehicle_year": 2015,
    "vehicle_make": "Dodge",
    "vehicle_model": "Charger",
    "annual_mileage": 18000,
    "coverage_level": "full",
    "violations_disclosed": ["dui", "speeding"],
    "accidents_disclosed": 2,
}

JORDAN: dict[str, Any] = {
    **JORDAN_RAW,
    "violations": ["dui", "speeding"],
    "consistency_flags": [],
}

CASEY_RAW: dict[str, Any] = {
    "name": "Casey Morgan",
    "age": 41,
    "zip_code": "60614",
    "years_licensed": 20,
    "vehicle_year": 2018,
    "vehicle_make": "Toyota",
    "vehicle_model": "Camry",
    "annual_mileage": 9000,
    "coverage_level": "liability",
    "violations_disclosed": [],
    "accidents_disclosed": 0,
}

CASEY: dict[str, Any] = {**CASEY_RAW, "violations": [], "consistency_flags": []}


def _flags_for(applicant: dict[str, Any]) -> list[dict[str, Any]]:
    flags = [dict(flag) for flag in applicant.get("consistency_flags") or []]
    covered = {
        frozenset(flag["fields"])
        for flag in flags
        if isinstance(flag.get("fields"), list)
    }
    for fields in required_flag_fields(applicant):
        if frozenset(fields) not in covered:
            flags.append(
                {
                    "fields": fields,
                    "issue": "This check is true on the form.",
                }
            )
    return flags


def fake_for(applicant: dict[str, Any], case_id: str = "C-001") -> FakeLLM:
    violations = list(applicant.get("violations", []))
    flags = _flags_for(applicant)
    looked_up = lookup_enrichment(case_id)
    scored = {
        **applicant,
        "violations": violations,
        "consistency_flags": flags,
    }
    surcharge = expected_surcharge(scored, looked_up)
    findings = [
        {
            "evidence_type": "mvr",
            "evidence": "at_fault_accidents" if label == "at_fault_accident" else label,
            "suggests": label,
            "explanation": "On the MVR and not disclosed.",
        }
        for label in undisclosed_violations(scored, looked_up)
    ]
    enrichment = {
        **looked_up,
        "nondisclosure_findings": findings,
        "rate_assessment": {
            "surcharge": surcharge,
            "reason": "Price follows the driving record.",
        },
    }
    score, band, _factors = compute_auto_score(scored, enrichment)
    outcome = pick_outcome(score)
    recommendation: dict[str, Any] = {
        "outcome": outcome,
        "reason": "Threshold applied to the risk score.",
    }
    if outcome == "approve":
        recommendation["rate_class"] = "surcharge" if surcharge else "standard"
    if outcome == "refer":
        recommendation["review_checklist"] = ["Review the file before issuing."]
    return FakeLLM(
        {
            "intake": {
                "violations": violations,
                "consistency_flags": flags,
            },
            "enrichment": {
                "nondisclosure_findings": findings,
                "rate_assessment": enrichment["rate_assessment"],
            },
            "risk": {
                "score": score,
                "band": band,
                "factors": ["Model factor"],
                "underwriter_note": "Note from the model.",
            },
            "recommendation": recommendation,
        }
    )


class TimeoutOnRiskLLM(FakeLLM):
    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if agent == "risk":
            raise TimeoutError("simulated LLM timeout")
        return super().complete(messages, agent=agent)


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing.time, "sleep", lambda _seconds: None)
