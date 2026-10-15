"""
This module tests the intake agent.

Responsibilities:
- Check that form fields are copied from the application.
- Check that free text becomes allowed violation labels and that true checks become flags.
- Check that an unknown label, an unknown field, or a missed check is rejected.
"""

import json

import pytest

from src.agents.intake import intake_agent
from src.llm import FakeLLM
from tests.conftest import ALEX_RAW


class _Capture(FakeLLM):
    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        self.messages = messages
        return super().complete(messages, agent=agent)


def test_intake_keeps_form_fields() -> None:
    llm = FakeLLM({"intake": {"name": "Someone Else", "violations": [], "consistency_flags": []}})
    out = intake_agent(ALEX_RAW, case_id="C-001", llm=llm)
    assert out["name"] == "Alex Rivera"
    assert out["age"] == 34
    assert out["vehicle_make"] == "Honda"
    assert out["vehicle_model"] == "Civic"
    assert out["coverage_level"] == "full"
    assert out["annual_mileage"] == 8000
    assert out["violations"] == []
    assert out["consistency_flags"] == []


def test_intake_maps_free_text_to_allowed_labels() -> None:
    raw = {
        **ALEX_RAW,
        "violations_disclosed": [],
        "accidents_disclosed": 1,
        "driving_record_text": "speeding ticket and a fender bender",
    }
    llm = FakeLLM(
        {
            "intake": {
                "vehicle_model": "spaceship",
                "violations": ["speeding", "at_fault_accident"],
                "consistency_flags": [],
            }
        }
    )
    out = intake_agent(raw, case_id="C-010", llm=llm)
    assert out["violations"] == ["speeding", "at_fault_accident"]
    assert out["driving_record_text"] == "speeding ticket and a fender bender"
    assert out["vehicle_model"] == "Civic"


def test_intake_accepts_violation_labels_in_any_case() -> None:
    raw = {**ALEX_RAW, "violations_disclosed": ["DUI", " Speeding "]}
    llm = FakeLLM(
        {"intake": {"violations": ["DUI", " Speeding "], "consistency_flags": []}}
    )
    out = intake_agent(raw, case_id="C-002", llm=llm)
    assert out["violations"] == ["dui", "speeding"]


def test_intake_rejects_unknown_violation_label() -> None:
    llm = FakeLLM({"intake": {"violations": ["parking"], "consistency_flags": []}})
    with pytest.raises(ValueError):
        intake_agent(ALEX_RAW, case_id="C-001", llm=llm)


def test_intake_raises_on_a_mileage_flag_for_alex() -> None:
    flags = [
        {
            "fields": ["age", "annual_mileage"],
            "issue": "Mileage looks high for this age.",
        }
    ]
    llm = FakeLLM({"intake": {"violations": [], "consistency_flags": flags}})
    with pytest.raises(ValueError):
        intake_agent(ALEX_RAW, case_id="C-001", llm=llm)


def test_intake_keeps_llm_issue_text_for_true_flags() -> None:
    raw = {
        **ALEX_RAW,
        "age": 20,
        "years_licensed": 10,
        "vehicle_year": 2008,
        "coverage_level": "full",
    }
    license_flag = {
        "fields": ["age", "years_licensed"],
        "issue": "Years licensed would put the first license before age 16.",
    }
    coverage_flag = {
        "fields": ["vehicle_year", "coverage_level"],
        "issue": "Full coverage on a vehicle older than 2010.",
    }
    llm = FakeLLM(
        {
            "intake": {
                "violations": [],
                "consistency_flags": [license_flag, coverage_flag],
            }
        }
    )
    out = intake_agent(raw, case_id="C-008", llm=llm)
    assert out["consistency_flags"] == [license_flag, coverage_flag]


def test_intake_raises_when_a_true_check_is_omitted() -> None:
    raw = {
        **ALEX_RAW,
        "age": 20,
        "years_licensed": 10,
        "vehicle_year": 2008,
        "coverage_level": "full",
    }
    flags = [
        {
            "fields": ["age", "years_licensed"],
            "issue": "Years licensed would put the first license before age 16.",
        }
    ]
    llm = FakeLLM({"intake": {"violations": [], "consistency_flags": flags}})
    with pytest.raises(ValueError):
        intake_agent(raw, case_id="C-008", llm=llm)


def test_intake_rejects_unknown_flag_field() -> None:
    llm = FakeLLM(
        {
            "intake": {
                "violations": [],
                "consistency_flags": [
                    {"fields": ["favorite_color"], "issue": "not on the form"}
                ],
            }
        }
    )
    with pytest.raises(ValueError):
        intake_agent(ALEX_RAW, case_id="C-001", llm=llm)


def test_intake_tells_the_model_which_checks_are_true() -> None:
    llm = _Capture({"intake": {"violations": [], "consistency_flags": []}})
    intake_agent(ALEX_RAW, case_id="C-001", llm=llm)
    payload = json.loads(llm.messages[1]["content"])
    assert payload["violations_required"] == []
    assert payload["checks_required"] == []

    raw = {
        **ALEX_RAW,
        "age": 20,
        "years_licensed": 10,
        "vehicle_year": 2008,
        "coverage_level": "full",
    }
    flags = [
        {
            "fields": ["age", "years_licensed"],
            "issue": "Years licensed would put the first license before age 16.",
        },
        {
            "fields": ["vehicle_year", "coverage_level"],
            "issue": "Full coverage on a vehicle older than 2010.",
        },
    ]
    llm = _Capture({"intake": {"violations": [], "consistency_flags": flags}})
    intake_agent(raw, case_id="C-008", llm=llm)
    payload = json.loads(llm.messages[1]["content"])
    assert payload["checks_required"] == [
        ["age", "years_licensed"],
        ["vehicle_year", "coverage_level"],
    ]


def test_intake_requires_form_fields() -> None:
    raw = {key: value for key, value in ALEX_RAW.items() if key != "zip_code"}
    llm = FakeLLM({"intake": {"violations": [], "consistency_flags": []}})
    with pytest.raises(ValueError, match="zip_code"):
        intake_agent(raw, case_id="C-001", llm=llm)
