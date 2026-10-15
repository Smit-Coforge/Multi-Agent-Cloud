"""
This module tests the read and write contracts around each agent.

Responsibilities:
- Call the intake wrapper with a function that also returns decision and risk.
- Check that only the applicant key is written back onto the case.
"""

from src.state import new_case
from src.tracing import wrap
from tests.conftest import ALEX, ALEX_RAW, fake_for


def test_wrapper_only_returns_owned_key() -> None:
    llm = fake_for(ALEX)

    def rogue(view, *, case_id, llm):
        return {**ALEX, "decision": "approve", "risk": {"score": 0}}

    node = wrap("intake", rogue, llm, [])
    patch = node(new_case("t", ALEX_RAW))
    assert list(patch.keys()) == ["applicant"]
    assert "decision" not in patch
    assert "risk" not in patch
