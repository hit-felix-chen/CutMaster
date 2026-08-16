from __future__ import annotations

import json
import re
from typing import Any


def parse_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        raise ValueError("Model returned an empty response")

    candidates = [value]
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", value, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = value.find("{"), value.rfind("}")
    if start >= 0 and end > start:
        candidates.append(value[start : end + 1])

    errors: list[str] = []
    for candidate in candidates:
        for repaired in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as exc:
                errors.append(str(exc))
                continue
            if not isinstance(parsed, dict):
                raise ValueError("Model JSON response must be an object")
            return parsed
    raise ValueError(f"Could not parse model JSON response: {errors[-1] if errors else 'unknown error'}")

