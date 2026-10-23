"""FS-00 platform security services (pure: no SQLAlchemy/DB/network imports).

Modules
-------
* :mod:`app.core.security.passwords` — Argon2id hashing + rehash-on-login policy.
* :mod:`app.core.security.tokens` — platform JWT access/refresh/selection tokens, rotation/replay,
  API-key HMAC helpers.
* :mod:`app.core.security.scopes` — capability catalogue, role expansion, route guard, SoD,
  assignability/privilege/last-admin guards.
* :mod:`app.core.security.tenant_context` — token → tenant/membership context, isolation and
  platform-admin vs tenant-admin classification, login tenant selection.
* :mod:`app.core.security.idempotency` — key canonicalisation, request fingerprints, replay/conflict
  decisions.
* :mod:`app.core.security.authn` — Dex SSO connection policy, enforced-SSO/local fallback, JIT/link
  decisions, group→role mapping bounds, ID-token claim validation.

The package deliberately has no persistence or HTTP dependencies so it can be unit-tested in isolation
and reused by request handlers, jobs and the contract tests.
"""

from __future__ import annotations

from app.core.security import authn, errors, idempotency, passwords, scopes, tenant_context, tokens

__all__ = [
    "authn",
    "errors",
    "idempotency",
    "passwords",
    "scopes",
    "tenant_context",
    "tokens",
]
