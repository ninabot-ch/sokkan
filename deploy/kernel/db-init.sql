-- The kernel engine's database: a SCHEMA of SOKKAN's own Postgres (docker-compose.kernel.yml,
-- service kernel-db). Idempotent: played at every start, by the database's administrator.
--   kernel_owner : owns the schema, applies the kernel's migrations (service kernel-migrate)
--   kernel_app   : the running kernel — reads and writes the tables, owns nothing, never
--                  superuser nor BYPASSRLS (the kernel's row policies apply to it)
-- Both see `kernel` first, then `public` (where the pgvector extension of SOKKAN lives). Neither
-- has any right on SOKKAN's own tables.
\set ON_ERROR_STOP on
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kernel_owner') THEN
        CREATE ROLE kernel_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kernel_app') THEN
        CREATE ROLE kernel_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;
ALTER ROLE kernel_owner PASSWORD :'owner_password';
ALTER ROLE kernel_app PASSWORD :'app_password';
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS kernel AUTHORIZATION kernel_owner;
ALTER ROLE kernel_owner SET search_path = kernel, public;
ALTER ROLE kernel_app SET search_path = kernel, public;
SELECT format('GRANT CONNECT ON DATABASE %I TO kernel_owner, kernel_app', current_database()) \gexec
-- SOKKAN's own schema: the kernel's roles may resolve the extension's types there, nothing else
REVOKE CREATE ON SCHEMA public FROM kernel_owner, kernel_app;
GRANT USAGE ON SCHEMA kernel TO kernel_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kernel TO kernel_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA kernel TO kernel_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kernel_owner IN SCHEMA kernel
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kernel_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kernel_owner IN SCHEMA kernel
    GRANT USAGE ON SEQUENCES TO kernel_app;
