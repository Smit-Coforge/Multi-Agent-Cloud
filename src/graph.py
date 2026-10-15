"""
This module wires the four agents into one sequential underwriting run.

Responsibilities:
- Build the graph from intake through enrichment, risk, and recommendation.
- Refer the case when an agent still fails after three attempts.
- Write one report and one trace for the finished run.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agents.enrichment import enrichment_agent
from src.agents.intake import intake_agent
from src.agents.recommendation import recommendation_agent
from src.agents.risk import risk_scoring_agent
from src.state import CaseState, new_case
from src.tracing import wrap, write_trace


def _call_intake(view: dict[str, Any], *, case_id: str, llm: Any) -> dict[str, Any]:
    return intake_agent(view["raw"], case_id=case_id, llm=llm)


def _call_enrichment(view: dict[str, Any], *, case_id: str, llm: Any) -> dict[str, Any]:
    return enrichment_agent(view["applicant"], case_id=case_id, llm=llm)


def _call_risk(view: dict[str, Any], *, case_id: str, llm: Any) -> dict[str, Any]:
    return risk_scoring_agent(
        view["applicant"], view["enrichment"], case_id=case_id, llm=llm
    )


def _call_recommendation(view: dict[str, Any], *, case_id: str, llm: Any) -> dict[str, Any]:
    return recommendation_agent(
        view["applicant"],
        view["enrichment"],
        view["risk"],
        case_id=case_id,
        llm=llm,
    )


def _router(agent: str, nxt: str):
    def route(case: CaseState) -> str:
        errors = (case.get("meta") or {}).get("errors") or []
        if agent in errors:
            return "escalate"
        return nxt

    return route


def _escalate(case: CaseState, log: list[dict[str, Any]]) -> dict[str, Any]:
    errors = (case.get("meta") or {}).get("errors") or ["unknown"]
    agent = errors[0]
    decision = {
        "outcome": "refer",
        "reason": f"{agent} failed after 3 attempts",
        "review_checklist": ["Review the case manually before issuing."],
    }
    write_trace(
        log,
        {
            "case_id": case["case_id"],
            "agent": "escalate",
            "input": {"meta": case.get("meta")},
            "output": decision,
            "started_at": "",
            "duration_ms": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "status": "escalated",
            "error": None,
            "retry": 0,
        },
    )
    print(f"  ⚠ escalate        REFER: {decision['reason']}")
    update: dict[str, Any] = {"decision": decision}
    if agent == "risk":
        update["risk"] = None
    return update


def build_graph(llm: Any, log: list[dict[str, Any]]) -> Any:
    graph = StateGraph(CaseState)
    graph.add_node("intake", wrap("intake", _call_intake, llm, log))
    graph.add_node("enrichment", wrap("enrichment", _call_enrichment, llm, log))
    graph.add_node("risk", wrap("risk", _call_risk, llm, log))
    graph.add_node(
        "recommendation",
        wrap("recommendation", _call_recommendation, llm, log),
    )
    graph.add_node("escalate", lambda case: _escalate(case, log))

    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake",
        _router("intake", "enrichment"),
        {"enrichment": "enrichment", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "enrichment",
        _router("enrichment", "risk"),
        {"risk": "risk", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "risk",
        _router("risk", "recommendation"),
        {"recommendation": "recommendation", "escalate": "escalate"},
    )
    graph.add_conditional_edges(
        "recommendation",
        _router("recommendation", END),
        {END: END, "escalate": "escalate"},
    )
    graph.add_edge("escalate", END)
    return graph.compile()


def run_pipeline(
    raw: Any,
    case_id: str,
    llm: Any,
    report_dir: str | Path = "report",
) -> dict[str, Any]:
    log: list[dict[str, Any]] = []
    app = build_graph(llm, log)
    case = app.invoke(new_case(case_id, raw))
    _write_run_report(report_dir, case_id, case, log)
    return case


def _trace_root(report_dir: Path) -> Path:
    """Traces sit next to report/, not inside the case file."""
    if report_dir.name == "report":
        return report_dir.parent / "trace"
    return report_dir / "trace"


def _write_run_report(
    report_dir: str | Path,
    case_id: str,
    case: dict[str, Any],
    log: list[dict[str, Any]],
) -> Path:
    """One report and one trace per run. A later run does not replace either."""
    folder = Path(report_dir) / case_id
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / f"{stamp}.json"
    if path.exists():
        path = folder / f"{stamp}-{time.time_ns()}.json"
    trace_folder = _trace_root(Path(report_dir)) / case_id
    trace_folder.mkdir(parents=True, exist_ok=True)
    trace_path = trace_folder / path.name
    case["report_path"] = str(path)
    case["trace_path"] = str(trace_path)
    path.write_text(json.dumps(case, indent=2, default=str), encoding="utf-8")
    trace_path.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    return path
