"""Human-signed, time-limited grants (ADR-0027). The only entry points other modules need:
`require_grant(settings, scope)` / `try_use(...)`, and `otp.assist_code(...)` for verification flows."""
from .store import Grant, GrantError, GrantStore, require_grant, try_use

__all__ = ["Grant", "GrantError", "GrantStore", "require_grant", "try_use"]
