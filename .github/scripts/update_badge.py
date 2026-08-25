"""Write the shields.io tests-badge JSON from pytest's JUnit XML report.

Usage:
    python update_badge.py <junit_xml_path> <badge_json_path> <pytest_outcome>

pytest_outcome is the GitHub Actions step outcome ("success" / "failure").
Stdlib only, so CI needs no extra packages.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    xml_path, badge_path, outcome = sys.argv[1], sys.argv[2], sys.argv[3]

    passed = failed = errors = 0
    parse_error = ""
    try:
        root = ET.parse(xml_path).getroot()
        suite = root.find(".//testsuite")
        if suite is None:
            suite = root
        total = int(suite.get("tests", "0"))
        failed = int(suite.get("failures", "0"))
        errors = int(suite.get("errors", "0"))
        skipped = int(suite.get("skipped", "0"))
        passed = max(total - failed - errors - skipped, 0)
    except Exception as exc:  # noqa: BLE001 - badge must render even on garbage input
        parse_error = str(exc)

    if outcome == "success":
        badge = {
            "schemaVersion": 1,
            "label": "tests",
            "message": f"{passed} passing",
            "color": "brightgreen",
        }
    elif parse_error or (failed == 0 and errors > 0):
        # Suite crashed before running (collection/import error) or the
        # report itself is unreadable.
        detail = parse_error.splitlines()[0] if parse_error else f"{errors} errors"
        badge = {
            "schemaVersion": 1,
            "label": "tests",
            "message": f"failing ({detail})"[:60],
            "color": "red",
        }
    else:
        badge = {
            "schemaVersion": 1,
            "label": "tests",
            "message": f"{passed} passing, {failed} failed",
            "color": "red",
        }

    with open(badge_path, "w", encoding="utf-8") as fh:
        json.dump(badge, fh)
        fh.write("\n")
    print(json.dumps(badge))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
