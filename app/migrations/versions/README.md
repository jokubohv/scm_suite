# Revision files

This directory is the **single Alembic chain** (see `../README.md`).

- S0 baseline: **empty** — FS-00 adds `0001_platform_core`, `0002_platform_security`,
  `0003_platform_events`; later feature sets add their pre-allocated `0004…0030` revisions.
- File name must equal the revision id: `0004_item_master.py` with `revision = "0004_item_master"`.
- One linear chain only: each revision has exactly one `down_revision` (string or `None`); merges are
  resolved by the integrator (rebase), never by `--merge`.
- Validate before pushing: `python -m app.migrations.chain_guard`.
- Never edit a revision that is already merged to the integration branch — add a new revision instead.
