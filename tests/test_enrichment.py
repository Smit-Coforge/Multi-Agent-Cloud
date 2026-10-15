"""
This module tests the enrichment agent.

Responsibilities:
- Check that a finding cites evidence that is on the motor vehicle record.
- Check that a disclosed violation is not kept as a finding.
- Check that invented evidence or a missing undisclosed violation is rejected.
"""

import pytest

from src.agents.enrichment import enrichment_agent
from src.llm import FakeLLM
from src.tools.enrichment_lookup import FIELDS, lookup_enrichment
from tests.conftest import ALEX


def _rate(surcharge: bool) -> dict:
    return {"surcharge": surcharge, "reason": "Price follows the driving record."}


def test_enrichment_rejects_evidence_not_in_records() -> None:
    llm = FakeLLM(
        {
            "enrichment": {
                "nondisclosure_findings": [
                    {
                        "evidence_type": "mvr",
                        "evidence": "dui",
                        "suggests": "dui",
                        "explanation": "This ticket is not on the C-001 file.",
                    }
                ],
                "rate_assessment": _rate(False),
            }
        }
    )
    with pytest.raises(ValueError):
        enrichment_agent(ALEX, case_id="C-001", llm=llm)


def test_enrichment_rejects_a_label_that_does_not_match_the_evidence() -> None:
    llm = FakeLLM(
        {
            "enrichment": {
                "nondisclosure_findings": [
                    {
                        "evidence_type": "mvr",
                        "evidence": "speeding",
                        "suggests": "dui",
                        "explanation": "The ticket was relabeled.",
                    }
                ],
                "rate_assessment": _rate(False),
            }
        }
    )
    with pytest.raises(ValueError):
        enrichment_agent({"violations": []}, case_id="C-009", llm=llm)


def test_enrichment_raises_when_an_undisclosed_violation_is_omitted() -> None:
    llm = FakeLLM(
        {
            "enrichment": {
                "nondisclosure_findings": [],
                "rate_assessment": _rate(False),
            }
        }
    )
    with pytest.raises(ValueError):
        enrichment_agent({"violations": []}, case_id="C-009", llm=llm)


def test_enrichment_drops_finding_for_a_disclosed_violation() -> None:
    llm = FakeLLM(
        {
            "enrichment": {
                "nondisclosure_findings": [
                    {
                        "evidence_type": "mvr",
                        "evidence": "speeding",
                        "suggests": "speeding",
                        "explanation": "Already on the application.",
                    },
                    {
                        "evidence_type": "mvr",
                        "evidence": "dui",
                        "suggests": "dui",
                        "explanation": "DUI is on the MVR and was not disclosed.",
                    },
                ],
                "rate_assessment": _rate(True),
            }
        }
    )
    out = enrichment_agent(
        {"violations": ["speeding"], "accidents_disclosed": 2},
        case_id="C-002",
        llm=llm,
    )
    assert out["nondisclosure_findings"] == [
        {
            "evidence_type": "mvr",
            "evidence": "dui",
            "suggests": "dui",
            "explanation": "DUI is on the MVR and was not disclosed.",
        }
    ]


def test_enrichment_stores_valid_finding() -> None:
    finding = {
        "evidence_type": "mvr",
        "evidence": "speeding",
        "suggests": "speeding",
        "explanation": "Speeding is on the MVR and was not disclosed.",
    }
    llm = FakeLLM(
        {
            "enrichment": {
                "nondisclosure_findings": [finding],
                "rate_assessment": _rate(False),
            }
        }
    )
    looked_up = lookup_enrichment("C-009")
    out = enrichment_agent({"violations": []}, case_id="C-009", llm=llm)
    assert out["nondisclosure_findings"] == [finding]
    assert out["rate_assessment"]["surcharge"] is False
    for key in FIELDS:
        assert out[key] == looked_up[key]
    assert "summary" not in out
