"""
This module tests a full underwriting run with a fake model.

Responsibilities:
- Run intake, enrichment, risk, and recommendation and check the decision.
- Check that each run writes its own report and trace.
- Check that a failing agent is retried and then referred.
"""

import json
from pathlib import Path

from src.agents.risk import compute_auto_score
from src.graph import run_pipeline
from src.llm import FakeLLM
from src.tools.enrichment_lookup import lookup_enrichment
from src.tracing import trace_totals
from tests.conftest import (
    ALEX,
    ALEX_RAW,
    CASEY_RAW,
    JORDAN,
    JORDAN_RAW,
    TimeoutOnRiskLLM,
    fake_for,
)


class TimeoutOnRecommendationLLM(FakeLLM):
    def complete(self, messages: list[dict[str, str]], *, agent: str) -> str:
        self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}
        if agent == "recommendation":
            raise TimeoutError("simulated LLM timeout")
        return super().complete(messages, agent=agent)


def _report(report_dir: Path, case_id: str) -> dict:
    files = sorted((report_dir / case_id).glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def _trace(report_dir: Path, case_id: str) -> list:
    report = _report(report_dir, case_id)
    rows = json.loads(Path(report["trace_path"]).read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    return rows


def _load(folder: str, case_id: str) -> dict:
    return json.loads(Path(folder, f"{case_id}.json").read_text(encoding="utf-8"))


def test_low_risk_file_approves(tmp_path: Path) -> None:
    case = run_pipeline(ALEX_RAW, "C-001", fake_for(ALEX), report_dir=tmp_path)
    assert case["decision"]["outcome"] == "approve"
    assert case["decision"]["reason"] == "Threshold applied to the risk score."
    assert case["risk"]["score"] < 0.4
    assert case["risk"]["factors"] == ["Model factor"]
    assert case["risk"]["underwriter_note"] == "Note from the model."
    report = _report(tmp_path, "C-001")
    assert "trace" not in report
    assert report["case_id"] == "C-001"
    assert report["decision"]["outcome"] == "approve"
    rows = _trace(tmp_path, "C-001")
    agents = [row["agent"] for row in rows]
    assert agents == ["intake", "enrichment", "risk", "recommendation"]
    for row in rows:
        assert "input" in row
        assert "output" in row
        assert "duration_ms" in row
        assert row["prompt_tokens"] == 10
        assert row["completion_tokens"] == 5
        assert row["cost_usd"] == 0
        assert row["status"] == "ok"
        assert row["retry"] == 0
    totals = trace_totals(rows)
    assert totals["duration_ms"] == sum(row["duration_ms"] for row in rows)
    assert totals["prompt_tokens"] == 40
    assert totals["completion_tokens"] == 20
    assert totals["cost_usd"] == 0


def test_medium_risk_file_refers(tmp_path: Path) -> None:
    raw = _load("data/applications", "C-004")
    rate = {"surcharge": False, "reason": "One speeding ticket does not raise the price."}
    mileage_flag = {
        "fields": ["age", "annual_mileage"],
        "issue": "Under 25 and driving at least 20000 miles a year.",
    }
    applicant = {
        **raw,
        "violations": ["speeding"],
        "consistency_flags": [mileage_flag],
    }
    enrichment = {
        **lookup_enrichment("C-004"),
        "nondisclosure_findings": [],
        "rate_assessment": rate,
    }
    score, band, _factors = compute_auto_score(applicant, enrichment)
    llm = FakeLLM(
        {
            "intake": {
                "violations": ["speeding"],
                "consistency_flags": [mileage_flag],
            },
            "enrichment": {
                "nondisclosure_findings": [],
                "rate_assessment": rate,
            },
            "risk": {
                "score": score,
                "band": band,
                "factors": ["speeding ticket"],
                "underwriter_note": "Young driver with one speeding ticket.",
            },
            "recommendation": {
                "outcome": "refer",
                "reason": "Refer because of age, mileage, and a speeding ticket.",
                "review_checklist": ["Confirm the speeding ticket", "Review mileage"],
            },
        }
    )
    case = run_pipeline(raw, "C-004", llm, report_dir=tmp_path)
    assert case["decision"]["outcome"] == "refer"
    assert case["decision"]["reason"] == "Refer because of age, mileage, and a speeding ticket."
    assert case["decision"]["review_checklist"] == [
        "Confirm the speeding ticket",
        "Review mileage",
    ]
    assert case["risk"]["score"] == score
    assert 0.4 <= case["risk"]["score"] <= 0.7
    assert case["risk"]["factors"] == ["speeding ticket"]
    assert case["risk"]["underwriter_note"] == "Young driver with one speeding ticket."


def test_high_risk_file_denies(tmp_path: Path) -> None:
    case = run_pipeline(JORDAN_RAW, "C-002", fake_for(JORDAN, "C-002"), report_dir=tmp_path)
    assert case["decision"]["outcome"] == "deny"
    assert case["risk"]["score"] > 0.7
    assert case["applicant"]["violations"] == ["dui", "speeding"]
    assert case["applicant"]["vehicle_model"] == "Charger"


def test_rejected_risk_reply_is_kept_on_the_trace(tmp_path: Path) -> None:
    llm = FakeLLM(
        {
            "intake": {"violations": [], "consistency_flags": []},
            "enrichment": {
                "nondisclosure_findings": [],
                "rate_assessment": {"surcharge": False, "reason": "Clean record."},
            },
            "risk": {
                "score": 0.1,
                "band": "low",
                "factors": ["prior dui"],
                "underwriter_note": "No dui on this file.",
            },
        }
    )
    case = run_pipeline(CASEY_RAW, "C-003", llm, report_dir=tmp_path)
    assert case["decision"]["outcome"] == "refer"
    assert case["risk"] is None
    risk_errors = [
        row for row in _trace(tmp_path, "C-003") if row["agent"] == "risk"
    ]
    assert [row["retry"] for row in risk_errors] == [0, 1, 2]
    for row in risk_errors:
        assert row["output"] is None
        assert row["reply"] is not None
        assert "prior dui" in row["reply"]


def test_risk_timeout_retries_then_escalates(tmp_path: Path) -> None:
    llm = TimeoutOnRiskLLM(
        {
            "intake": {"violations": [], "consistency_flags": []},
            "enrichment": {
                "nondisclosure_findings": [],
                "rate_assessment": {"surcharge": False, "reason": "Clean record."},
            },
            "recommendation": {"reason": "unused"},
        }
    )
    case = run_pipeline(CASEY_RAW, "C-003", llm, report_dir=tmp_path)
    assert case["decision"]["outcome"] == "refer"
    assert "risk failed after 3 attempts" in case["decision"]["reason"]
    assert case["decision"]["review_checklist"]
    assert case["risk"] is None
    rows = _trace(tmp_path, "C-003")
    risk_errors = [row for row in rows if row["agent"] == "risk"]
    assert [row["retry"] for row in risk_errors] == [0, 1, 2]
    assert all(row["status"] == "error" for row in risk_errors)
    assert all(row["reply"] is None for row in risk_errors)
    for row in rows:
        assert row["cost_usd"] == 0
    assert any(row["agent"] == "escalate" and row["status"] == "escalated" for row in rows)
    assert not any(row["agent"] == "recommendation" for row in rows)


def test_recommendation_timeout_retries_then_escalates(tmp_path: Path) -> None:
    llm = TimeoutOnRecommendationLLM(fake_for(ALEX).responses)
    case = run_pipeline(ALEX_RAW, "C-001", llm, report_dir=tmp_path)
    assert case["decision"]["outcome"] == "refer"
    assert case["decision"]["reason"] == "recommendation failed after 3 attempts"
    assert case["decision"]["review_checklist"]
    assert case["risk"] is not None
    assert case["risk"]["factors"] == ["Model factor"]
    rows = _trace(tmp_path, "C-001")
    assert [row["agent"] for row in rows] == [
        "intake",
        "enrichment",
        "risk",
        "recommendation",
        "recommendation",
        "recommendation",
        "escalate",
    ]
    recommendation_errors = [row for row in rows if row["agent"] == "recommendation"]
    assert [row["retry"] for row in recommendation_errors] == [0, 1, 2]
    assert all(row["status"] == "error" for row in recommendation_errors)
    escalate = rows[-1]
    assert escalate["agent"] == "escalate"
    assert escalate["status"] == "escalated"
    assert escalate["output"]["outcome"] == "refer"
    for row in rows:
        assert row["cost_usd"] == 0


def test_each_run_writes_its_own_report(tmp_path: Path) -> None:
    first = run_pipeline(ALEX_RAW, "C-001", fake_for(ALEX), report_dir=tmp_path)
    second = run_pipeline(ALEX_RAW, "C-001", fake_for(ALEX), report_dir=tmp_path)
    files = sorted((tmp_path / "C-001").glob("*.json"))
    assert len(files) == 2
    assert first["report_path"] != second["report_path"]
    saved = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    assert "trace" not in saved[0]
    assert "trace" not in saved[1]
    traces = [json.loads(Path(item["trace_path"]).read_text(encoding="utf-8")) for item in saved]
    assert traces[0] != []
    assert traces[1] != []
    assert saved[0]["trace_path"] != saved[1]["trace_path"]
    assert saved[0]["case_id"] == "C-001"
    assert saved[1]["case_id"] == "C-001"


def test_trace_totals_sum_sequential_rows() -> None:
    trace = [
        {
            "duration_ms": 4,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": 0,
        },
        {
            "duration_ms": 6,
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "cost_usd": 0,
        },
    ]
    totals = trace_totals(trace)
    assert totals == {
        "duration_ms": 10,
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "cost_usd": 0,
    }


def test_case_files_cover_scenarios() -> None:
    alex = _load("data/applications", "C-001")
    riley = _load("data/applications", "C-005")
    assert alex["name"] == "Alex Rivera"
    assert riley["name"] == "Riley Chen"
    for case_id, person in (("C-001", alex), ("C-005", riley)):
        score, _, _ = compute_auto_score(
            {**person, "consistency_flags": []},
            lookup_enrichment(case_id),
        )
        assert score < 0.4

    jordan = _load("data/applications", "C-002")
    jordan_score, _, _ = compute_auto_score(jordan, lookup_enrichment("C-002"))
    assert "dui" in jordan["violations_disclosed"]
    assert jordan_score > 0.7

    nora = _load("data/applications", "C-007")
    assert nora["age"] < 25
    assert nora["annual_mileage"] >= 25000

    helen = _load("data/applications", "C-008")
    assert helen["years_licensed"] > helen["age"] - 16
    assert helen["vehicle_year"] < 2010
    assert helen["coverage_level"] == "full"

    denise = _load("data/applications", "C-009")
    denise_file = _load("data/enrichment", "C-009")
    assert denise["violations_disclosed"] == []
    assert denise_file["mvr"]["violations"] == ["speeding"]

    evan = _load("data/applications", "C-010")
    assert evan["driving_record_text"] == "speeding ticket and a fender bender"

    for number in range(1, 11):
        record = _load("data/enrichment", f"C-{number:03d}")
        assert "mvr" in record
        assert "claims_history" in record
        assert "vehicle_record" in record
        assert "territory" in record
        assert "matches_application" not in record["mvr"]
        assert record["territory"]["risk_band"] in {"low", "medium", "high"}
