"""Typed exceptions for I/O failures only.

State-machine signals (verification failed, retries exhausted, payment outcome)
use enum returns from transition functions, not exceptions — this keeps
transitions pure. See DECISIONS plan-mode additions.

These exceptions are raised by ``api.py`` and ``llm.py`` at the I/O boundary
and translated into state-machine effects by ``agent.py``.
"""
from __future__ import annotations


class PaymentAgentError(Exception):
    """Base class for all payment-agent I/O exceptions."""


class ApiTimeout(PaymentAgentError):
    """The HTTP call exceeded its configured timeout (per-call or total budget).

    Raised by ``api.py`` after the documented retry policy is exhausted.
    """


class ApiTransport(PaymentAgentError):
    """Underlying transport failure (connection refused, DNS, TLS, etc.).

    Distinct from :class:`ApiTimeout` so callers can distinguish "no answer"
    from "couldn't reach." Raised by ``api.py`` after retries.
    """


class ApiUnknown(PaymentAgentError):
    """Parent class for "transient/unknown" API failures.

    Per DECISIONS #13: 5xx and 4xx-with-undocumented-error-code both route
    to silent-retry on lookup and immediate terminal on payment — identical
    behavior at the state-machine layer. The split into subclasses
    (:class:`ApiServerError`, :class:`ApiUnexpectedResponse`) is for
    log/ops visibility only — api.py logs the concrete class name in the
    redacted error_class field. Catch ApiUnknown (the parent) for routing.
    """


class ApiServerError(ApiUnknown):
    """API returned a 5xx status code."""


class ApiUnexpectedResponse(ApiUnknown):
    """API returned an unexpected response: undocumented 4xx error_code,
    non-JSON body, or a schema that fails LookupResponse / payment-success
    Pydantic validation."""


class LlmCallFailed(PaymentAgentError):
    """The Anthropic SDK call failed (network, auth, rate limit, etc.).

    Raised by ``llm.py``. ``agent.py`` may degrade to the deterministic-only
    fallback path (DECISIONS #18) on this exception when ``ANTHROPIC_API_KEY``
    is absent at startup; runtime LLM failures with a present key surface as
    a graceful turn-level error message.
    """


class AgentInternalError(PaymentAgentError):
    """State-machine invariant violation surfaced by the orchestrator.

    Raised by ``agent.py``'s defensive paths when the deterministic kernel
    or the orchestrator's own loop hits a state that cannot occur in correct
    code (e.g., CALL_PROCESS_PAYMENT requested with missing slots, side-
    effect drive loop exceeding its bound). Caught at the ``next()`` boundary
    and converted into a clean closed-session message — the eval harness is
    a graded deliverable and must not crash on defensive paths. The
    underlying bug surfaces via a stderr warning, not a Python traceback.
    """
