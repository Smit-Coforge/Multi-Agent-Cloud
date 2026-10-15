"""
This module tests risk scoring.

Responsibilities:
- Check the score and band for a clean file and a severe file.
- Check that a valid model score, band, factors, and note are stored.
- Check that a bad score, band, or a DUI factor on a file with no DUI is rejected.
"""

import pytest

from src.agents.risk import RISK_SYSTEM, compute_auto_score, risk_scoring_agent
from src.llm import FakeLLM
from src.tools.enrichment_lookup import lookup_enrichment
from tests.conftest import ALEX, JORDAN

ENRICHMENT_ALEX = lookup_enrichment("C-001")
ENRICHMENT_JORDAN = lookup_enrichment("C-002")


def test_risk_prompt_does_not_show_a_sample_score() -> None:
    assert "0.0" not in RISK_SYSTEM
    assert '"band": "low"' not in RISK_SYSTEM


def test_compute_score_low_risk_approvable() -> None:
    score, band, factors = compute_auto_score(ALEX, ENRICHMENT_ALEX)
    assert score < 0.4
    assert band == "low"
    assert factors


def test_compute_score_high_risk_declinable() -> None:
    score, band, _factors = compute_auto_score(JORDAN, ENRICHMENT_JORDAN)
    assert score > 0.7
    assert band == "high"


def test_risk_stores_valid_model_judgment() -> None:
    score, _band, _factors = compute_auto_score(ALEX, ENRICHMENT_ALEX)
    llm = FakeLLM(
        {
            "risk": {
                "score": score + 0.02,
                "band": "low",
                "factors": ["Model factor"],
                "underwriter_note": "Model wanted a different score.",
            }
        }
    )
    out = risk_scoring_agent(ALEX, ENRICHMENT_ALEX, case_id="t", llm=llm)
    assert out["score"] == score + 0.02
    assert out["band"] == "low"
    assert out["factors"] == ["Model factor"]
    assert out["underwriter_note"] == "Model wanted a different score."


def test_risk_stores_a_judgment_that_differs_from_the_formula() -> None:
    llm = FakeLLM(
        {
            "risk": {
                "score": 0.99,
                "band": "high",
                "factors": ["Age and record"],
                "underwriter_note": "The file as a whole is high risk.",
            }
        }
    )
    out = risk_scoring_agent(ALEX, ENRICHMENT_ALEX, case_id="t", llm=llm)
    formula, _, _ = compute_auto_score(ALEX, ENRICHMENT_ALEX)
    assert out["score"] == 0.99
    assert out["score"] != formula
    assert "formula_score" not in out
    assert out["band"] == "high"


def test_risk_rejects_score_band_or_factors_that_fail_the_check() -> None:
    score, _band, _factors = compute_auto_score(ALEX, ENRICHMENT_ALEX)
    mismatched_band = FakeLLM(
        {
            "risk": {
                "score": score,
                "band": "high",
                "factors": ["Model factor"],
                "underwriter_note": "Note from the model.",
            }
        }
    )
    with pytest.raises(ValueError):
        risk_scoring_agent(ALEX, ENRICHMENT_ALEX, case_id="t", llm=mismatched_band)
    empty_factors = FakeLLM(
        {
            "risk": {
                "score": score,
                "band": "low",
                "factors": [],
                "underwriter_note": "Note from the model.",
            }
        }
    )
    with pytest.raises(ValueError):
        risk_scoring_agent(ALEX, ENRICHMENT_ALEX, case_id="t", llm=empty_factors)
    dui_on_clean_file = FakeLLM(
        {
            "risk": {
                "score": score,
                "band": "low",
                "factors": ["Prior dui"],
                "underwriter_note": "Note from the model.",
            }
        }
    )
    with pytest.raises(ValueError):
        risk_scoring_agent(ALEX, ENRICHMENT_ALEX, case_id="t", llm=dui_on_clean_file)


def test_findings_surcharge_and_flags_raise_score() -> None:
    base, _, _ = compute_auto_score(ALEX, ENRICHMENT_ALEX)

    disclosed = {
        **ENRICHMENT_ALEX,
        "nondisclosure_findings": [
            {
                "evidence_type": "mvr",
                "evidence": "speeding",
                "suggests": "speeding",
                "explanation": "not disclosed",
            }
        ],
    }
    nondisclosure_score, _, nondisclosure_factors = compute_auto_score(ALEX, disclosed)
    assert nondisclosure_score > base
    assert any("undisclosed" in factor.lower() for factor in nondisclosure_factors)

    surcharge = {
        **ENRICHMENT_ALEX,
        "rate_assessment": {"surcharge": True, "reason": "High mileage."},
    }
    surcharge_score, _, surcharge_factors = compute_auto_score(ALEX, surcharge)
    assert surcharge_score > base
    assert any("surcharge" in factor.lower() for factor in surcharge_factors)

    flagged = {
        **ALEX,
        "consistency_flags": [
            {
                "fields": ["age", "annual_mileage"],
                "issue": "Mileage is high for this age.",
            }
        ],
    }
    flagged_score, _, flagged_factors = compute_auto_score(flagged, ENRICHMENT_ALEX)
    assert flagged_score > base
    assert any("consistency" in factor.lower() for factor in flagged_factors)

    many_flags = {
        **ALEX,
        "consistency_flags": [
            {"fields": ["age", "years_licensed"], "issue": f"flag {index}"}
            for index in range(6)
        ],
    }
    many_score, _, _ = compute_auto_score(many_flags, ENRICHMENT_ALEX)
    assert many_score > flagged_score
    assert many_score - base <= 0.16 + 1e-9
