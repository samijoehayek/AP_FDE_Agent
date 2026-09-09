-- Create the role the application connects as. Local development only.
--
-- This exists because of a control that did not work. The Postgres Docker image
-- makes POSTGRES_USER a SUPERUSER, and a superuser bypasses every privilege
-- check -- so the REVOKE that makes audit_events append-only was recorded in the
-- table's ACL and then ignored at query time. "Append-only" was true on paper
-- and false in the database.
--
-- The fix is separation of duties, which is what production would do anyway:
--
--   ap_agent      owns the schema and runs migrations. Superuser locally.
--   ap_agent_app  serves traffic. Ordinary role, and therefore actually subject
--                 to the grants the migration sets.
--
-- Provisioning creates roles; migrations grant privileges. That split is why
-- this password lives in a compose-only init script and not in a migration.
-- In production this role comes from your infrastructure, and the migration
-- grants to whatever AP_AGENT_DB_APP_ROLE names.

CREATE ROLE ap_agent_app
    LOGIN
    PASSWORD 'ap_agent_app'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOBYPASSRLS;

GRANT CONNECT ON DATABASE ap_agent TO ap_agent_app;
GRANT USAGE ON SCHEMA public TO ap_agent_app;
