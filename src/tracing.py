"""
This module wraps each agent so a run can be traced and retried.

Responsibilities:
- Pass an agent only the case keys its contract allows it to read.
- Record duration, token counts, and a zero local cost for each attempt.
- Retry a failing agent three times, then mark that agent as failed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from src.state import CONTRACTS, CaseState

AgentFn = Callable[..., dict[str, Any]]

def token_cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    """Ollama runs locally, so prompt and completion tokens have no dollar price."""
    del prompt_tokens, completion_tokens
    return 0.0


def trace_totals(trace: list[dict[str, Any]]) -> dict[str, float | int]:
    """Sum a sequential trace. Duration adds; this graph does not overlap steps."""
    return {
        "duration_ms": sum(int(row["duration_ms"]) for row in trace),
        "prompt_tokens": sum(int(row["prompt_tokens"]) for row in trace),
        "completion_tokens": sum(int(row["completion_tokens"]) for row in trace),
        "cost_usd": sum(float(row["cost_usd"]) for row in trace),
    }


def write_trace(log: list[dict[str, Any]], record: dict[str, Any]) -> None:
    log.append(record)


def _reply(llm: Any) -> str | None:
    reply = getattr(llm, "last_content", None)
    if isinstance(reply, str):
        return reply
    return None


def _priced_usage(llm: Any) -> tuple[int, int, float]:
    usage = llm.last_usage or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    return (
        prompt_tokens,
        completion_tokens,
        token_cost_usd(prompt_tokens, completion_tokens),
    )


def wrap(name: str, fn: AgentFn, llm: Any, log: list[dict[str, Any]]) -> Any:
    rule = CONTRACTS[name]

    def node(case: CaseState) -> dict[str, Any]:
        view = {key: case[key] for key in rule["reads"]}  # type: ignore[literal-required]
        case_id = case["case_id"]
        llm.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for attempt in range(3):
            started_at = datetime.now(UTC).isoformat()
            t0 = time.perf_counter()
            llm.last_content = None
            try:
                out = fn(view, case_id=case_id, llm=llm)
                if not isinstance(out, dict):
                    raise TypeError(f"{name} must return a dict")
                duration_ms = int((time.perf_counter() - t0) * 1000)
                prompt_tokens, completion_tokens, cost_usd = _priced_usage(llm)
                write_trace(
                    log,
                    {
                        "case_id": case_id,
                        "agent": name,
                        "input": view,
                        "output": out,
                        "reply": _reply(llm),
                        "started_at": started_at,
                        "duration_ms": duration_ms,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cost_usd": cost_usd,
                        "status": "ok",
                        "error": None,
                        "retry": attempt,
                    },
                )
                print(f"  ✓ {name:16} {duration_ms}ms")
                return {rule["writes"]: out}
            except Exception as exc:  # noqa: BLE001 — retry any agent failure
                duration_ms = int((time.perf_counter() - t0) * 1000)
                prompt_tokens, completion_tokens, cost_usd = _priced_usage(llm)
                write_trace(
                    log,
                    {
                        "case_id": case_id,
                        "agent": name,
                        "input": view,
                        "output": None,
                        "reply": _reply(llm),
                        "started_at": started_at,
                        "duration_ms": duration_ms,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cost_usd": cost_usd,
                        "status": "error",
                        "error": repr(exc),
                        "retry": attempt,
                    },
                )
                print(f"  ✗ {name:16} retry={attempt} {exc!r}")
                if attempt < 2:
                    time.sleep([1, 2][attempt])
        errors = list((case.get("meta") or {}).get("errors") or [])
        errors.append(name)
        retries = dict((case.get("meta") or {}).get("retries") or {})
        retries[name] = 3
        return {"meta": {"errors": errors, "retries": retries}}

    return node
