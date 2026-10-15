# Multi-Agent Underwriting Pipeline

Auto insurance: intake → enrichment → risk → recommendation, with tracing and retry-then-escalate.

The pipeline rates the driver and the car: age, driving record, vehicle, and location. A bad record raises the price or leads to a denial.

## Part 1: State and memory

This is the state design, written before the four agents were connected. Each run keeps one shared case file: a single dict that intake, enrichment, risk, and recommendation all read from and write back to.

**Choice: a shared case file.** `run_pipeline()` builds that dict with `new_case()` and LangGraph carries it as the graph state. The other option was message passing, where each agent would hand its output straight to the next agent as that agent's only input. A later step that needed an earlier result would have to receive it again through every step in between.

**Why a shared case file**

1. **Observability.** The whole case sits in one dict, so a trace can record exactly which keys an agent was allowed to see and what it wrote back. Debugging is opening that dict. The same dict is what `report/<case_id>/<timestamp>.json` saves at the end of the run.
2. **Extensibility.** Recommendation already reads the driver and the car on `applicant`, the motor vehicle record on `enrichment`, and the score on `risk`. Intake does not have to forward all of that through every step. A later premium quote, which needs the vehicle and that score, would add those two keys to its contract. The agents already in the pipeline stay as they are. With message passing, that quote would mean changing the payload at every step between intake and risk.

Shared state is simpler to set up and riskier if it is left open: an agent can read or write a key it does not own, and one applicant's data can bleed into the next run. Four guardrails close that.

| Guardrail | What it prevents | How |
|---|---|---|
| Fresh case per run | A previous applicant left behind in a global dict | `new_case()` is called inside `run_pipeline()`. There is no module-level `CASE = {}`. |
| `case_id` on every record | A log line that cannot be tied to an applicant | The id is on the case, on every agent call, and on every trace line. |
| Read/write contracts | An agent seeing or overwriting another agent's keys | `CONTRACTS` in `src/state.py` names each agent's read keys and its one write key. The wrapper in `src/tracing.py` passes only those read keys and accepts a write only for that agent's own key. |
| Leakage test | Context bleed between two runs in one process | `tests/test_no_leakage.py` runs C-001 (Alex Rivera) and then C-002 (Jordan Blake) and asserts Blake's case contains none of Rivera's data. |

**Traces and reports are not working memory.** `trace/` is a write-only step log, including each raw model reply. `report/` is a copy of the finished case for a person to read. Agents never open those files. The case dict is the only memory the pipeline works from, and that dict is discarded when the run ends.

### Case keys

| Key | Written by | Read by |
|---|---|---|
| `case_id` | `run_pipeline` | everyone (logs / traces) |
| `raw` | `run_pipeline` | intake |
| `applicant` | intake | enrichment, risk, recommendation |
| `enrichment` | enrichment | risk, recommendation |
| `risk` | risk | recommendation |
| `decision` | recommendation, or escalate on failure | final output |
| `meta` | node wrapper | failure routing, tests |

### Read/write matrix

`R` = may read. `W` = may write. Anything else is blocked by the wrapper.

| Agent | raw | applicant | enrichment | risk | decision |
|---|---|---|---|---|---|
| intake | R | W | | | |
| enrichment | | R | W | | |
| risk | | R | R | W | |
| recommendation | | R | R | R | W |

New fields stay inside those keys: `violations` and `consistency_flags` on `applicant`, `nondisclosure_findings` and `rate_assessment` on `enrichment`, `underwriter_note` on `risk`, and `review_checklist` on a refer `decision`. An approve `decision` also has `rate_class` (`standard` or `surcharge`).

## Data

`data/applications/<case_id>.json` is the application: name, age, zip code, years licensed, vehicle year, make, and model, annual mileage, coverage level (`liability` or `full`), disclosed violations, and disclosed at-fault accident count. `driving_record_text` is optional free text from a "describe your record" box.

`data/enrichment/<case_id>.json` holds the outside record: motor vehicle record, claims, vehicle record, and garaging territory. It has no pre-baked match boolean. The enrichment agent always loads all four lookups.

| Case | What it is for |
|---|---|
| C-001 Alex Rivera | Clean record, standard rate |
| C-002 Jordan Blake | Deny: DUI, repeat at-fault accidents, high-risk zip |
| C-003 Casey Morgan | Clean record, nothing disclosed |
| C-004 Samir Patel | Young driver, high mileage, one speeding ticket |
| C-005 Riley Chen | Clean record, standard rate |
| C-006 Harold Nguyen | Suspended license and reckless driving |
| C-007 Nora Alvarez | 26,000 miles a year, so the price goes up |
| C-008 Helen Park | Licensed longer than her age allows; 2008 car with full coverage |
| C-009 Denise Okonkwo | Nothing disclosed; speeding on the MVR |
| C-010 Evan Brooks | Free text: "speeding ticket and a fender bender" |
| C-011 Morgan Ellis | Mid file for refer: license dates do not add up, age 24 with 21,000 miles, one speeding ticket |

## Agents

The LLM proposes the labels, flags, findings, surcharge, score, band, factors, note, outcome, and reason. Code raises `ValueError` when that proposal fails one of the checks below. The node wrapper retries that agent 3 times, then refers the case. A failed reply is kept on the trace as `reply`.

| Agent | LLM proposes | Code raises unless |
|---|---|---|
| intake | Allowed violation labels from `violations_disclosed` and `driving_record_text`, and `consistency_flags`. The model's issue text is what gets stored. | Form fields are copied from the JSON. The prompt includes `violations_required` and `checks_required`, which code already computed. The model copies those lists. The label set must match (`speeding`, `red_light`, `dui`, `reckless`, `at_fault_accident`, `suspended_license`, `other`). The checks are: `years_licensed > age - 16`; `age < 25` and `annual_mileage >= 20000`; `vehicle_year < 2010` and `coverage_level` is `full`; `accidents_disclosed > years_licensed`. A flag that fails its check or names an unknown field raises. A check the form meets that the model omitted raises. Required keys still have to be present. |
| enrichment | `nondisclosure_findings` (`evidence_type` of `mvr`, plus evidence, suggests, and an explanation) and `rate_assessment` (`surcharge` plus a reason). | All four lookups are copied from the file. The prompt includes `undisclosed_violations` and `surcharge_required`. The model copies them. Evidence is a string on `mvr.violations`, or `at_fault_accidents` when that count is higher than `accidents_disclosed`. `suggests` is that same label. A finding for a violation already disclosed is dropped. `surcharge` must match the record: high-risk zip, license not valid, 2 or more at-fault accidents, mileage 25000 or more, or a `dui`, `reckless`, or `suspended_license` on the application or the MVR. The reason must be non-empty. |
| risk | `score`, `band`, `factors`, and `underwriter_note`. | `score` is between 0 and 1. `band` matches that score. The prompt does not include a sample score. `factors` is a non-empty list of non-empty strings. `underwriter_note` is a non-empty string. A file with no DUI has no factor that uses the word dui. |
| recommendation | `outcome`, `reason`, `rate_class` on approve, and, on refer, `review_checklist`. | `outcome` is approve, refer, or deny, chosen from the whole file. `reason` is non-empty, and a reason that calls a score of 0.7 or below "above 0.7" is rejected. An approve is rejected when the risk score is above 0.7. The prompt includes `referral_required`. That is true when the file has a consistency flag and no severe violation, and the outcome must then be refer. The prompt includes `rate_class_required`. An approve must copy it: `surcharge` when the rate assessment says so, and `standard` otherwise. A refer includes a non-empty `review_checklist`. |

## How to run

```bash
# unit + pipeline tests (FakeLLM, no Ollama)
pytest tests

# live case (Ollama qwen3:8b on the host)
python run.py C-001
# C-002 through C-011
```

Risk stores the model's score when it is between 0 and 1, the band matches that score, the factors and note are non-empty, and a file with no DUI has no dui factor. Recommendation stores the model's outcome when it is approve, refer, or deny, the reason is non-empty, an approve is not used for a score above 0.7, an approve carries the rate class the assessment requires, and a refer includes a checklist.

If an agent raises (timeout, bad JSON, or a proposal that disagrees with the file), the wrapper retries 3 times then refers. Nothing is keyed off a special case id.

Docker talks to host Ollama at `http://host.docker.internal:11434` (`OLLAMA_HOST` in `docker-compose.yml`). Each run writes `report/<case_id>/<timestamp>.json` for the case and `trace/<case_id>/<timestamp>.json` for the step log. The case records `trace_path`.

## Part 4

Cost and latency come from three finished runs. Each trace has one row per agent. This graph is sequential, so the case latency is the sum of `duration_ms`. The case cost is the sum of `cost_usd`. Token totals are the sums of `prompt_tokens` and `completion_tokens`.

| Case | What the run decided | Trace |
|---|---|---|
| C-007 Nora Alvarez | Approve, `rate_class: surcharge`. Score 0.3, band low. She drives 26,000 miles a year, so the price goes up. | `trace/C-007/20261011T234241600303Z.json` |
| C-006 Harold Nguyen | Deny. Score 0.3, band low. Suspended license, reckless driving, and an at-fault accident. | `trace/C-006/20261011T235706612761Z.json` |
| C-011 Morgan Ellis | Refer. Score 0.5, band medium. The license dates do not add up, and he is 24 with 21,000 miles a year. | `trace/C-011/20261011T235012910962Z.json` |

### Per agent

| Case | Agent | duration_ms | Prompt tokens | Completion tokens | cost_usd | status |
|---|---|---:|---:|---:|---:|---|
| C-007 | intake | 2645 | 518 | 59 | 0 | ok |
| C-007 | enrichment | 2430 | 577 | 49 | 0 | ok |
| C-007 | risk | 4585 | 529 | 97 | 0 | ok |
| C-007 | recommendation | 2256 | 529 | 42 | 0 | ok |
| C-006 | intake | 901 | 517 | 16 | 0 | ok |
| C-006 | enrichment | 2484 | 611 | 48 | 0 | ok |
| C-006 | risk | 4574 | 522 | 99 | 0 | ok |
| C-006 | recommendation | 2168 | 548 | 40 | 0 | ok |
| C-011 | intake | 3958 | 556 | 94 | 0 | ok |
| C-011 | enrichment | 1542 | 599 | 37 | 0 | ok |
| C-011 | risk | 3479 | 577 | 76 | 0 | ok |
| C-011 | recommendation | 3303 | 607 | 67 | 0 | ok |

### Per case

| Case | duration_ms | Prompt tokens | Completion tokens | Total tokens | cost_usd |
|---|---:|---:|---:|---:|---:|
| C-007 | 11916 | 2153 | 247 | 2400 | 0 |
| C-006 | 10127 | 2198 | 203 | 2401 | 0 |
| C-011 | 12282 | 2339 | 274 | 2613 | 0 |
| All three | 34325 | 6690 | 724 | 7414 | 0 |

`cost_usd` is 0 on every row because `token_cost_usd` prices a local Ollama model at 0. There is no per-call API charge. A paid API would cost these token counts times that provider's price per prompt token and per completion token. The three cases together are 6,690 prompt tokens and 724 completion tokens.

The slowest step is risk: 4,585 ms on C-007 and 4,574 ms on C-006. Intake is the other variable step, from 901 ms on C-006 to 3,958 ms on C-011. Nothing here overlaps, so those times add.

### Real-time or batch

The average case takes about 11.4 seconds, and every case is between 10.1 and 12.3 seconds. A real-time use case needs an answer while a person is waiting, on the order of a second. These runs are too slow for that. An overnight batch does not wait on one applicant. A few seconds per case is acceptable there, and the $0 local cost does not change with volume. The limit in a batch is how many cases fit in the night, not the dollar price of a call.
