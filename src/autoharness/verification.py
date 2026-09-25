"""Reusable, evidence-oriented checks for common project tasks."""
from __future__ import annotations

from autoharness.env import HarnessError


GUIDES = {
    "browser": {
        "brief_verify": "Open the affected page in a browser, exercise the user path, inspect console and network errors, save a screenshot and result log.",
        "steps": [
            "Start the app using its documented development command and open the affected route.",
            "Run the user interaction in a browser or Playwright; assert the visible result and relevant navigation.",
            "Check browser console and failed network requests; capture a screenshot at the final state.",
        ],
        "evidence": "Screenshot plus browser or Playwright output with the route and assertion result.",
    },
    "api": {
        "brief_verify": "Send a real request to the affected API route, assert status and response fields, test one failure case, save request/response evidence.",
        "steps": [
            "Start the service and send a request to the affected route with curl or an API test client.",
            "Assert status code, response schema and one relevant value; repeat with an invalid request.",
            "Save redacted request/response samples and the assertion output.",
        ],
        "evidence": "API test output or redacted request/response file with assertions.",
    },
    "api-client": {
        "brief_verify": "Save a real response of the third-party API as the reference, check the code's output against a separate request, and check its behaviour on an unreachable host, an error status and an unexpected answer.",
        "steps": [
            "Send the real request with curl first and save the raw response as a reference: that is what the code must parse.",
            "Run the code and compare every value it prints or returns with a separate request made at the same time.",
            "Point it at an unreachable host, an error status and a malformed or unexpected answer (missing, zero or non-numeric values); "
            "check each gives a clear error and the documented exit code or exception, not a traceback or a wrong value.",
            "Check the code stays within the user's constraints (dependencies, runtime, keys kept out of the source).",
        ],
        "evidence": "The saved raw response plus the output of the comparison and failure checks.",
    },
    "logs": {
        "brief_verify": "Exercise the affected path, inspect service logs for the expected event and new errors, save a bounded log excerpt.",
        "steps": [
            "Start the service with logs captured to a file and note the start time.",
            "Exercise the affected request or job, then search the new log interval for its correlation ID.",
            "Check expected event and absence of new errors; redact secrets before saving an excerpt.",
        ],
        "evidence": "Timestamped, redacted log excerpt plus the action that produced it.",
    },
    "database": {
        "brief_verify": "Run the affected database operation on disposable data, assert rows and rollback or cleanup, save query/test output.",
        "steps": [
            "Use a disposable test database or transaction and seed the minimum fixture.",
            "Run the operation and assert resulting rows, constraints and one failure case.",
            "Rollback or clean up; save test output without credentials or personal data.",
        ],
        "evidence": "Database test output showing assertions and cleanup or rollback.",
    },
}


def guide(kind: str) -> dict:
    if kind not in GUIDES:
        raise HarnessError(f"unknown verification kind {kind!r}")
    return {"kind": kind, **GUIDES[kind]}
