"""JSON Schemas (Draft 7) passed to Devin as structured_output_schema."""

from __future__ import annotations

from typing import Any

REMEDIATION_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["success", "summary"],
    "properties": {
        "success": {"type": "boolean"},
        "pr_url": {"type": "string"},
        "checks_run": {"type": "array", "items": {"type": "string"}},
        "precommit_pass": {"type": "boolean"},
        "tests_pass": {"type": "boolean"},
        "summary": {"type": "string"},
        "blockers": {"type": "string"},
    },
}

# Smoke session: measures whether the warm snapshot makes per-session
# verification affordable (see docs/spec.md, "Prereqs").
SMOKE_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["node_modules_present", "jest_seconds", "jest_passed"],
    "properties": {
        "node_modules_present": {"type": "boolean"},
        "npm_install_seconds": {"type": ["number", "null"]},
        "jest_seconds": {"type": "number"},
        "jest_passed": {"type": "boolean"},
        "precommit_available": {"type": "boolean"},
        "precommit_seconds": {"type": ["number", "null"]},
        "precommit_passed": {"type": ["boolean", "null"]},
        "notes": {"type": "string"},
    },
}
