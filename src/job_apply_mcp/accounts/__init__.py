"""ATS credential vault, logins and accounts (ADR-0046). Secrets live only in the OS keychain and reach a page
only through `browser_ops.PlaywrightLoginOps.fill_password`; every use needs `consent.active(settings,
"credentials")`."""
from .login import AccountManager, LoginOutcome, RunReport
from .vault import Vault, VaultError, normalize_host

__all__ = ["AccountManager", "LoginOutcome", "RunReport", "Vault", "VaultError", "normalize_host"]
