"""Run the risk agent once and print the risk object.

The Fargate task uses this as its command. The case is the JSON packed
in the image. The model is Ollama at OLLAMA_HOST. stdout is what
CloudWatch Logs stores for the task.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.risk import risk_scoring_agent
from src.llm import OllamaLLM

SAMPLE = Path(__file__).with_name("sample-input.json")


def main() -> None:
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    case_id = str(payload["case_id"])
    print(f"risk-agent start case_id={case_id}", flush=True)
    risk = risk_scoring_agent(
        payload["applicant"],
        payload["enrichment"],
        case_id=case_id,
        llm=OllamaLLM(),
    )
    print(json.dumps(risk), flush=True)


if __name__ == "__main__":
    main()
