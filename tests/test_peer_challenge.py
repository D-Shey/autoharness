"""Tests for Peer Review Challenge & Cross-Validation (Perplexity Multiagent model)."""
from __future__ import annotations

from autoharness import reviewers


SAMPLE_CRITIC_REVIEW = """
VERDICT: missing
FINDINGS:
- Finding: Missing validation for negative balance in account service
- Issue: Incomplete error handling for timeout in network dispatcher
- Concern: Lacks unit test coverage for concurrent transactions
"""


def test_extract_critic_claims():
    claims = reviewers.extract_critic_claims(SAMPLE_CRITIC_REVIEW)
    assert len(claims) >= 3
    assert any("negative balance" in c for c in claims)
    assert any("timeout" in c for c in claims)


def test_peer_challenge_all_overruled():
    # Author provides evidence covering all points
    code_evidence = {
        "account.py": "def validate_balance(b): if b < 0: raise ValueError('negative balance error')",
        "network.py": "def dispatch(): try: pass except TimeoutError: return handle_timeout()",
        "test_concurrency.py": "def test_concurrent_transactions(): assert True",
    }
    challenge = reviewers.create_peer_challenge(
        SAMPLE_CRITIC_REVIEW,
        original_verdict="missing",
        author_defense="All items are already implemented and tested in codebase.",
        code_evidence=code_evidence,
    )
    assert challenge["total_claims"] == 3

    resolution = reviewers.synthesize_peer_resolution(challenge)
    assert resolution["resolution"] == "overruled"
    assert resolution["revised_verdict"] == "met"
    assert resolution["points_overruled"] == 3
    assert resolution["points_upheld"] == 0


def test_peer_challenge_partial_compromise():
    # Evidence for only one claim
    code_evidence = {
        "account.py": "def validate_balance(b): if b < 0: raise ValueError('negative balance error')",
    }
    challenge = reviewers.create_peer_challenge(
        SAMPLE_CRITIC_REVIEW,
        original_verdict="missing",
        author_defense="",
        code_evidence=code_evidence,
    )
    resolution = reviewers.synthesize_peer_resolution(challenge)
    assert resolution["resolution"] == "compromise"
    assert resolution["revised_verdict"] == "partial"
    assert resolution["points_overruled"] >= 1
    assert resolution["points_upheld"] >= 1


def test_peer_challenge_all_upheld():
    challenge = reviewers.create_peer_challenge(
        SAMPLE_CRITIC_REVIEW,
        original_verdict="missing",
        author_defense="",
        code_evidence={},
    )
    resolution = reviewers.synthesize_peer_resolution(challenge)
    assert resolution["resolution"] == "upheld"
    assert resolution["revised_verdict"] == "missing"
    assert resolution["points_upheld"] == 3

    report = reviewers.format_peer_challenge_report(resolution)
    assert "UPHELD" in report
