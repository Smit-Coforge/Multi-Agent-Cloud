"""
This module tests that one run does not leave data in the next.

Responsibilities:
- Run Alex Rivera and then Jordan Blake in the same process.
- Check that Blake's case contains none of Rivera's data.
"""

from src.graph import run_pipeline
from tests.conftest import ALEX, ALEX_RAW, JORDAN, JORDAN_RAW, fake_for


def test_second_case_does_not_contain_first(tmp_path) -> None:
    case_a = run_pipeline(
        ALEX_RAW,
        "C-001",
        fake_for(ALEX),
        report_dir=tmp_path,
    )
    case_b = run_pipeline(
        JORDAN_RAW,
        "C-002",
        fake_for(JORDAN, "C-002"),
        report_dir=tmp_path,
    )
    assert case_a["applicant"]["name"] == "Alex Rivera"
    assert case_b["applicant"]["name"] == "Jordan Blake"
    assert "Alex" not in str(case_b["applicant"])
    assert "Alex" not in str(case_b["enrichment"])
    assert case_a["case_id"] != case_b["case_id"]
