-- Dex dev storage (S0-02).
--
-- The Dex v2.41 image in the local compose stack uses PostgreSQL storage so the dev issuer is
-- durable and mirrors the reference deployment model (DEX-SSO-ADDENDUM §2: Dex owns its storage
-- and the platform never queries it). Dex runs its own migrations into this database at startup;
-- this script only creates the database.
--
-- Runs once, on first initialisation of an empty `pgdata` volume (official postgres image
-- docker-entrypoint-initdb.d behaviour). To re-run after `down --volumes`, the next start recreates it.
CREATE DATABASE dex;
