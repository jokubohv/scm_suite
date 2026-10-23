"""Contract test harness base (S0-04).

Local fixtures only: this package deliberately does not import or modify
``app/tests/conftest.py`` (owned by the S0-05 harness task). FS builders reuse
``fixtures.py`` / ``schema_validation.py`` for provider and consumer contract tests.
"""
