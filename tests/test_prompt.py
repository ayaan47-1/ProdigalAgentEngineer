"""Tests for prompt.py — the v2 system prompt.

These tests assert structural invariants the prompt must carry. The
prompt's *content* will evolve over the rebuild cycle; what's locked is:

  - It mentions every tool by name (so the LLM knows the tool catalog).
  - It mentions every canonical kind (so the LLM knows when to call
    render_canonical_message).
  - It encodes the SESSION_START_SENTINEL value (so the bootstrap path
    is testable).
  - The SHA-256 helper is stable and changes when the prompt changes.

We deliberately do NOT assert on exact prompt prose — that would lock
the prompt against iteration. Eval personas test BEHAVIOR; these tests
test CONTRACT.
"""
from __future__ import annotations

from payment_agent import prompt, tools


class TestPromptStructure:
    def test_prompt_is_non_empty(self):
        assert len(prompt.SYSTEM_PROMPT) > 1000

    def test_prompt_has_versioned_constant(self):
        assert prompt.SYSTEM_PROMPT_VERSION
        # Loose semver shape check.
        parts = prompt.SYSTEM_PROMPT_VERSION.split(".")
        assert len(parts) >= 3

    def test_session_start_sentinel_is_documented(self):
        assert prompt.SESSION_START_SENTINEL in prompt.SYSTEM_PROMPT

    def test_empty_input_sentinel_is_documented(self):
        assert prompt.EMPTY_INPUT_SENTINEL in prompt.SYSTEM_PROMPT


class TestPromptMentionsEveryTool:
    """The system prompt must reference every tool by name so the LLM
    knows the tool catalog without depending on the schema list alone.
    """

    def test_mentions_lookup_account(self):
        assert tools.TOOL_LOOKUP_ACCOUNT in prompt.SYSTEM_PROMPT

    def test_mentions_submit_verification(self):
        assert tools.TOOL_SUBMIT_VERIFICATION in prompt.SYSTEM_PROMPT

    def test_mentions_process_payment(self):
        assert tools.TOOL_PROCESS_PAYMENT in prompt.SYSTEM_PROMPT

    def test_mentions_cancel_session(self):
        assert tools.TOOL_CANCEL_SESSION in prompt.SYSTEM_PROMPT

    def test_mentions_render_canonical_message(self):
        assert tools.TOOL_RENDER_CANONICAL_MESSAGE in prompt.SYSTEM_PROMPT


class TestPromptMentionsEveryCanonicalKind:
    """Each canonical kind must be referenced so the LLM knows when to
    render it. We assert the kind name appears at least once in the
    prompt body.
    """

    def test_mentions_all_kinds(self):
        for kind in tools.ALL_KINDS:
            assert kind in prompt.SYSTEM_PROMPT, (
                f"kind {kind!r} not mentioned in system prompt"
            )


class TestPromptCoversHardRules:
    """Loose substring tests for the load-bearing safety rules. These
    catch accidental deletions during prompt iteration without locking
    the exact wording.
    """

    def test_mentions_verification_before_payment(self):
        # Rule #1 from the layering map.
        text = prompt.SYSTEM_PROMPT.lower()
        assert "without" in text
        assert "verif" in text  # 'verification' / 'verified'
        # And payment-related copy.
        assert "process_payment" in prompt.SYSTEM_PROMPT

    def test_mentions_confirmation_gate(self):
        # Rule #2 — confirmation before payment.
        assert "confirmation" in prompt.SYSTEM_PROMPT.lower()
        # The canonical 'confirmation_prompt' kind is mentioned.
        assert "confirmation_prompt" in prompt.SYSTEM_PROMPT

    def test_forbids_sensitive_data_in_replies(self):
        # Rule #3 — no PII echo.
        text = prompt.SYSTEM_PROMPT.lower()
        assert "dob" in text or "date of birth" in text
        assert "aadhaar" in text
        assert "pincode" in text
        assert "cvv" in text

    def test_mentions_retry_budgets(self):
        # Hard rules #4 and #5 — retry caps named.
        assert "3" in prompt.SYSTEM_PROMPT  # verification budget
        assert "5" in prompt.SYSTEM_PROMPT  # payment budget

    def test_mentions_account_id_echo_rule(self):
        # V2-19 — echo the account ID before lookup.
        text = prompt.SYSTEM_PROMPT.lower()
        assert "echo" in text or "got it" in text

    def test_mentions_anti_enumeration(self):
        # Rule #8 — account_not_found is terminal, no second lookup.
        text = prompt.SYSTEM_PROMPT.lower()
        assert (
            "anti-enumeration" in text
            or "account_not_found" in text
            or "do not invite" in text
        )


class TestPromptCoversEdgeCases:
    """The prompt has an explicit edge-cases section per V2-23."""

    def test_mentions_dob_disambiguation(self):
        text = prompt.SYSTEM_PROMPT.lower()
        assert "disambig" in text or "ambiguous" in text
        # Specifically the date case.
        assert "august" in text or "october" in text

    def test_mentions_cycle_violation(self):
        assert "CYCLE_VIOLATION_NAME" in prompt.SYSTEM_PROMPT

    def test_mentions_cancellation_reasons(self):
        text = prompt.SYSTEM_PROMPT
        assert "user_requested_cancellation" in text
        assert "out_of_scope_request" in text or "scope_shift" in text


class TestPromptHasFewShotExamples:
    """V2-24 — 2-3 short examples included."""

    def test_has_at_least_two_examples(self):
        # Examples are introduced with '## Example' headers.
        assert prompt.SYSTEM_PROMPT.count("## Example") >= 2

    def test_examples_cover_dob_disambiguation(self):
        # Example A
        text = prompt.SYSTEM_PROMPT.lower()
        assert "1992-08-10" in prompt.SYSTEM_PROMPT or "august 10" in text

    def test_examples_cover_scope_shift(self):
        assert "scope_shift" in prompt.SYSTEM_PROMPT

    def test_examples_cover_identity_pivot(self):
        # Example C — different name mid-cycle.
        text = prompt.SYSTEM_PROMPT.lower()
        assert "priya mehta" in text or "different name" in text or (
            "CYCLE_VIOLATION_NAME" in prompt.SYSTEM_PROMPT
            and "earlier you said" in text
        )


class TestPromptHash:
    def test_sha256_is_64_hex_chars(self):
        h = prompt.system_prompt_sha256()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_sha256_is_stable(self):
        # Two calls in a row produce the same hash.
        assert prompt.system_prompt_sha256() == prompt.system_prompt_sha256()

    def test_sha256_reflects_content(self):
        # The hash should match a direct hashlib computation on the
        # current prompt bytes — guards against future divergence
        # between the helper and the constant.
        import hashlib

        expected = hashlib.sha256(
            prompt.SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest()
        assert prompt.system_prompt_sha256() == expected
