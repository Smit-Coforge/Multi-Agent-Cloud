"""
This module standardizes a raw auto insurance application into an applicant record.

Responsibilities:
- Copy required form fields from the application JSON.
- Ask the model for violation labels and consistency flags.
- Reject labels and flags that do not match checks already computed from the form.
"""

from __future__ import annotations

import json
from typing import Any

from src.llm import parse_json_object
from src.state import ALLOWED_VIOLATIONS, canonical_violation

REQUIRED = (
    "name",
    "age",
    "zip_code",
    "years_licensed",
    "vehicle_year",
    "vehicle_make",
    "vehicle_model",
    "annual_mileage",
    "coverage_level",
    "violations_disclosed",
    "accidents_disclosed",
)

APPLICATION_FIELDS = REQUIRED + ("driving_record_text",)
_COVERAGE = ("liability", "full")

_LABELS = ", ".join(ALLOWED_VIOLATIONS)
_FIELDS = ", ".join(APPLICATION_FIELDS)

# label, phrases that count as that label in the form text
_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("speeding", ("speeding", "speed ticket")),
    ("red_light", ("red_light", "red light")),
    ("dui", ("dui", "dwi", "drunk")),
    ("reckless", ("reckless",)),
    ("at_fault_accident", ("at_fault_accident", "fender bender", "at-fault", "crash")),
    ("suspended_license", ("suspended_license", "suspended", "revoked")),
)

INTAKE_SYSTEM = f"""You standardize an auto insurance application. Use only the user JSON.
Return JSON only, with exactly two keys and no others:
violations (array of strings) and consistency_flags (array of objects).
Do not return name, age, zip, license years, vehicle, mileage, coverage, or accident count. Code copies those from the form.

violations:
violations_required is the label list for this form. Do not recompute it.
Return that list, each label once, and no other labels.
Allowed labels are:
{_LABELS}
If violations_required is empty, violations is an empty array.

consistency_flags:
checks_required is the list of checks that are already true. Do not recompute it.
Emit one flag for each entry in checks_required, and no others.
Each flag's fields array must equal that entry, in the same order.
Each object has a fields array and an issue string.
Every name in fields must be one of:
{_FIELDS}
Say what you noticed in issue. Use the matching check below:
1. years_licensed is greater than age - 16. Fields: age, years_licensed.
2. age is under 25 and annual_mileage is 20000 or more. Fields: age, annual_mileage.
3. vehicle_year is under 2010 and coverage_level is full. Fields: vehicle_year, coverage_level.
4. accidents_disclosed is greater than years_licensed. Fields: accidents_disclosed, years_licensed.
If checks_required is empty, consistency_flags is an empty array."""


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _form(raw_application: Any) -> dict[str, Any]:
    if not isinstance(raw_application, dict):
        raise ValueError("application must be a JSON object")  # noqa: TRY004
    missing = [key for key in REQUIRED if key not in raw_application]
    if missing:
        raise ValueError(f"intake missing keys: {missing}")
    for key in ("name", "zip_code", "vehicle_make", "vehicle_model"):
        value = raw_application[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
    if raw_application.get("coverage_level") not in _COVERAGE:
        raise ValueError("coverage_level must be liability or full")
    violations = raw_application.get("violations_disclosed")
    if not isinstance(violations, list) or any(not isinstance(item, str) for item in violations):
        raise ValueError("violations_disclosed must be a list of strings")
    for key in ("age", "years_licensed", "vehicle_year", "annual_mileage", "accidents_disclosed"):
        value = raw_application[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    return raw_application


def _phrase_hit(text: str) -> bool:
    lowered = text.casefold()
    return any(phrase in lowered for _, phrases in _PHRASES for phrase in phrases)


def required_violations(form: dict[str, Any]) -> list[str]:
    """Labels the form's disclosed record actually names."""
    parts: list[str] = []
    disclosed = form.get("violations_disclosed") or []
    if isinstance(disclosed, list):
        parts.extend(item for item in disclosed if isinstance(item, str))
    text = form.get("driving_record_text")
    if isinstance(text, str):
        parts.append(text)
    blob = " ".join(parts).casefold()
    found = [label for label, phrases in _PHRASES if any(phrase in blob for phrase in phrases)]
    if isinstance(disclosed, list) and any(
        isinstance(item, str) and item.strip() and not _phrase_hit(item) for item in disclosed
    ):
        found.append("other")
    return found


def _violations(raw: Any, form: dict[str, Any]) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError("violations must be a list of allowed labels")  # noqa: TRY004
    labels: list[str] = []
    for item in raw:
        label = canonical_violation(item)
        if label is None:
            raise ValueError(f"violation not allowed: {item!r}")
        if label not in labels:
            labels.append(label)
    if set(labels) != set(required_violations(form)):
        raise ValueError("violations do not match the form")
    return labels


# name, field set that identifies the check, field order for a synthesized flag
_FLAG_FIELDS: tuple[tuple[str, frozenset[str], tuple[str, ...]], ...] = (
    (
        "license_age",
        frozenset({"age", "years_licensed"}),
        ("age", "years_licensed"),
    ),
    (
        "young_mileage",
        frozenset({"age", "annual_mileage"}),
        ("age", "annual_mileage"),
    ),
    (
        "old_full",
        frozenset({"vehicle_year", "coverage_level"}),
        ("vehicle_year", "coverage_level"),
    ),
    (
        "accident_count",
        frozenset({"accidents_disclosed", "years_licensed"}),
        ("accidents_disclosed", "years_licensed"),
    ),
)


def _true_checks(form: dict[str, Any]) -> set[str]:
    age = _as_int(form.get("age"))
    years = _as_int(form.get("years_licensed"))
    mileage = _as_int(form.get("annual_mileage"))
    vehicle_year = _as_int(form.get("vehicle_year"))
    accidents = _as_int(form.get("accidents_disclosed"))
    true: set[str] = set()
    if years > age - 16:
        true.add("license_age")
    if age < 25 and mileage >= 20_000:
        true.add("young_mileage")
    if vehicle_year < 2010 and form.get("coverage_level") == "full":
        true.add("old_full")
    if accidents > years:
        true.add("accident_count")
    return true


def required_flag_fields(form: dict[str, Any]) -> list[list[str]]:
    """Field lists for the checks that are true on this form."""
    true = _true_checks(form)
    return [list(fields) for name, _keys, fields in _FLAG_FIELDS if name in true]


def _flags(raw: Any, form: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("consistency_flags must be a list")  # noqa: TRY004
    true = _true_checks(form)
    seen: set[str] = set()
    flags: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("consistency flag must be an object")  # noqa: TRY004
        fields = item.get("fields")
        issue = item.get("issue")
        if not isinstance(fields, list) or not fields:
            raise ValueError("consistency flag fields must be a non-empty list")
        for field in fields:
            if not isinstance(field, str) or field not in APPLICATION_FIELDS:
                raise ValueError(f"unknown application field: {field!r}")
        if not isinstance(issue, str) or not issue.strip():
            raise ValueError("consistency flag issue must be a non-empty string")
        field_set = frozenset(fields)
        matched = [name for name, keys, _fields in _FLAG_FIELDS if keys == field_set]
        if len(matched) != 1 or matched[0] not in true:
            raise ValueError("consistency flag is not true on the form")
        seen.add(matched[0])
        flags.append({"fields": list(fields), "issue": issue.strip()})
    missing = true - seen
    if missing:
        raise ValueError(
            "consistency flag omitted for a check the form meets: "
            + ", ".join(sorted(missing))
        )
    return flags


def intake_agent(raw_application: Any, *, case_id: str, llm: Any) -> dict[str, Any]:
    form = _form(raw_application)
    content = llm.complete(
        [
            {"role": "system", "content": INTAKE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        **form,
                        "violations_required": required_violations(form),
                        "checks_required": required_flag_fields(form),
                    }
                ),
            },
        ],
        agent="intake",
    )
    data = parse_json_object(content)
    applicant = {key: form[key] for key in REQUIRED}
    applicant["violations"] = _violations(data.get("violations"), form)
    if "driving_record_text" in form:
        text = form["driving_record_text"]
        if not isinstance(text, str):
            raise ValueError("driving_record_text must be a string")
        applicant["driving_record_text"] = text
    applicant["consistency_flags"] = _flags(data.get("consistency_flags"), form)
    return applicant
