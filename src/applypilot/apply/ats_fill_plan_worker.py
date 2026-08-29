"""Isolated stdin/stdout worker for the bounded ATS fill-plan specialist."""

from __future__ import annotations

import json
import sys

from applypilot.apply.specialists import dispatch_production_specialist


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise TypeError("request must be an object")
        result = dispatch_production_specialist(
            str(request.get("kind") or ""),
            phase=str(request.get("phase") or ""),
            payload=request.get("payload"),
            snapshot_catalog=request.get("snapshot_catalog"),
        )
        rendered = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(rendered.encode("utf-8")) > 16 * 1024:
            raise ValueError("output exceeds limit")
        sys.stdout.write(rendered)
        return 0
    except Exception as exc:  # noqa: BLE001 - process boundary reports type only
        sys.stderr.write(type(exc).__name__)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
