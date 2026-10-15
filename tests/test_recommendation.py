"""
This module tests the recommendation agent.

Responsibilities:
- Check that a valid approve, refer, or deny is stored with its reason.
- Check that an approve carries the rate class the assessment requires.
- Check that a high score cannot be approved and that a refer includes a checklist.
"""

import json

import pytest

from src.agents.recommendation import (
    RECOMMENDATION_SYSTEM,
    pick_outcome,
    recommendation_agent,
)
from src.llm import FakeLLM
from src.tools.enrichment_lookup import lookup_enrichment
from tests.conftest import ALEX

ENRICHMENT = lookup_enrichment("C-001")
RISK = {"score": 0.38, "band": "low", "factors": ["clean record"]}


class _Capture(FakeLLM):
    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        self.messages = messages
        return super().complete(messages, agent=agent)


def test_recommendation_prompt_does_not_show_one_rate_class() -> None:
    assert '"rate_class": "standard"' not in RECOMMENDATION_SYSTEM
    assert '"rate_class": "surcharge"' not in RECOMMENDATION_SYSTEM


def test_recommendation_tells_the_model_the_required_rate_class() -> None:
    priced = {**ENRICHMENT, "rate_assessment": {"surcharge": True, "reason": "Record requires a higher price."}}
    llm = _Capture(
        {
            "recommendation": {
                "outcome": "approve",
                "reason": "Issue at a higher price.",
                "rate_class": "surcharge",
            }
        }
    )
    recommendation_agent(ALEX, priced, RISK, case_id="C-002", llm=llm)
    payload = json.loads(llm.messages[1]["content"])
    assert payload["referral_required"] is False
    assert payload["rate_class_required"] == "surcharge"


def test_pick_outcome_thresholds() -> None:
    assert pick_outcome(0.38) == "approve"
    assert pick_outcome(0.4) == "refer"
    assert pick_outcome(0.7) == "refer"
    assert pick_outcome(0.81) == "deny"


def test_recommendation_keeps_valid_outcome_and_reason() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "approve",
                "reason": "Low risk file.",
                "rate_class": "standard",
            }
        }
    )
    out = recommendation_agent(ALEX, ENRICHMENT, RISK, case_id="C-001", llm=llm)
    assert out["outcome"] == "approve"
    assert out["reason"] == "Low risk file."
    assert out["rate_class"] == "standard"
    assert "review_checklist" not in out


def test_recommendation_stores_an_outcome_the_score_alone_would_not_pick() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "deny",
                "reason": "The file as a whole is not acceptable.",
            }
        }
    )
    out = recommendation_agent(ALEX, ENRICHMENT, RISK, case_id="C-001", llm=llm)
    assert out["outcome"] == "deny"
    assert out["reason"] == "The file as a whole is not acceptable."
    assert "review_checklist" not in out


def test_recommendation_rejects_approve_when_the_score_is_high() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "approve",
                "reason": "The risk score is low.",
                "rate_class": "standard",
            }
        }
    )
    risk = {"score": 0.8, "band": "high", "factors": ["years licensed exceeds age"]}
    with pytest.raises(ValueError, match="cannot approve a high risk score"):
        recommendation_agent(ALEX, ENRICHMENT, risk, case_id="C-008", llm=llm)


def test_consistency_flags_require_refer() -> None:
    flagged = {
        **ALEX,
        "violations": ["speeding"],
        "consistency_flags": [
            {
                "fields": ["age", "years_licensed"],
                "issue": "years_licensed is greater than age - 16",
            }
        ],
    }
    approved = FakeLLM(
        {
            "recommendation": {
                "outcome": "approve",
                "reason": "No severe violations.",
                "rate_class": "standard",
            }
        }
    )
    with pytest.raises(ValueError, match="consistency flags require refer"):
        recommendation_agent(flagged, ENRICHMENT, RISK, case_id="C-011", llm=approved)
    referred = FakeLLM(
        {
            "recommendation": {
                "outcome": "refer",
                "reason": "A person should confirm the license date.",
                "review_checklist": ["Confirm years licensed against age"],
            }
        }
    )
    out = recommendation_agent(flagged, ENRICHMENT, RISK, case_id="C-011", llm=referred)
    assert out["outcome"] == "refer"


def test_severe_violation_can_still_be_denied_when_flagged() -> None:
    flagged = {
        **ALEX,
        "violations": ["dui"],
        "consistency_flags": [
            {
                "fields": ["age", "years_licensed"],
                "issue": "years_licensed is greater than age - 16",
            }
        ],
    }
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "deny",
                "reason": "The file has a DUI and should not be issued.",
            }
        }
    )
    out = recommendation_agent(flagged, ENRICHMENT, RISK, case_id="C-002", llm=llm)
    assert out["outcome"] == "deny"


def test_reason_cannot_call_a_mid_score_above_the_cutoff() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "refer",
                "reason": (
                    "The driver's risk score is 0.5, which is above the 0.7 threshold "
                    "for approval, so a review is required."
                ),
                "review_checklist": ["Confirm the license date"],
            }
        }
    )
    risk = {"score": 0.5, "band": "medium", "factors": ["mileage"]}
    with pytest.raises(ValueError, match="reason contradicts the risk score"):
        recommendation_agent(ALEX, ENRICHMENT, risk, case_id="C-011", llm=llm)


def test_recommendation_may_refer_a_high_score() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "refer",
                "reason": "A person should review the age and years licensed.",
                "review_checklist": ["Confirm years licensed against age"],
            }
        }
    )
    risk = {"score": 0.8, "band": "high", "factors": ["years licensed exceeds age"]}
    out = recommendation_agent(ALEX, ENRICHMENT, risk, case_id="C-008", llm=llm)
    assert out["outcome"] == "refer"
    assert out["review_checklist"] == ["Confirm years licensed against age"]


def test_approve_rate_class_must_match_the_assessment() -> None:
    priced = {**ENRICHMENT, "rate_assessment": {"surcharge": True, "reason": "DUI on the MVR."}}
    standard = FakeLLM(
        {
            "recommendation": {
                "outcome": "approve",
                "reason": "Issue it.",
                "rate_class": "standard",
            }
        }
    )
    with pytest.raises(ValueError):
        recommendation_agent(ALEX, priced, RISK, case_id="C-002", llm=standard)
    surcharge = FakeLLM(
        {
            "recommendation": {
                "outcome": "approve",
                "reason": "Issue at a higher price.",
                "rate_class": "surcharge",
            }
        }
    )
    out = recommendation_agent(ALEX, priced, RISK, case_id="C-002", llm=surcharge)
    assert out["rate_class"] == "surcharge"


def test_recommendation_rejects_unknown_outcome() -> None:
    llm = FakeLLM({"recommendation": {"outcome": "postpone", "reason": "Wait."}})
    with pytest.raises(ValueError):
        recommendation_agent(ALEX, ENRICHMENT, RISK, case_id="C-001", llm=llm)


def test_recommendation_raises_when_llm_fails() -> None:
    llm = FakeLLM({"recommendation": "not-json"})
    with pytest.raises(ValueError):
        recommendation_agent(ALEX, ENRICHMENT, RISK, case_id="C-001", llm=llm)


def test_recommendation_raises_on_empty_reason() -> None:
    llm = FakeLLM(
        {"recommendation": {"outcome": "approve", "reason": "  ", "rate_class": "standard"}}
    )
    with pytest.raises(ValueError):
        recommendation_agent(ALEX, ENRICHMENT, RISK, case_id="C-001", llm=llm)


def test_refer_includes_review_checklist() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "refer",
                "reason": "Refer for a human review of the speeding ticket.",
                "review_checklist": ["Confirm the speeding ticket", "Review mileage"],
            }
        }
    )
    risk = {"score": 0.55, "band": "medium", "factors": ["One speeding ticket"]}
    out = recommendation_agent(ALEX, ENRICHMENT, risk, case_id="C-004", llm=llm)
    assert out["outcome"] == "refer"
    assert out["reason"] == "Refer for a human review of the speeding ticket."
    assert out["review_checklist"] == ["Confirm the speeding ticket", "Review mileage"]


def test_refer_raises_when_llm_fails() -> None:
    llm = FakeLLM({"recommendation": "not-json"})
    risk = {"score": 0.55, "band": "medium", "factors": ["One speeding ticket"]}
    with pytest.raises(ValueError):
        recommendation_agent(ALEX, ENRICHMENT, risk, case_id="C-004", llm=llm)


def test_refer_raises_on_bad_checklist() -> None:
    llm = FakeLLM(
        {
            "recommendation": {
                "outcome": "refer",
                "reason": "Refer for a human review.",
                "review_checklist": [],
            }
        }
    )
    risk = {"score": 0.55, "band": "medium", "factors": ["One speeding ticket"]}
    with pytest.raises(ValueError):
        recommendation_agent(ALEX, ENRICHMENT, risk, case_id="C-004", llm=llm)


class _TimeoutLLM(FakeLLM):
    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        raise TimeoutError("simulated LLM timeout")


def test_recommendation_timeout_raises() -> None:
    llm = _TimeoutLLM({})
    with pytest.raises(TimeoutError):
        recommendation_agent(ALEX, ENRICHMENT, RISK, case_id="C-001", llm=llm)
