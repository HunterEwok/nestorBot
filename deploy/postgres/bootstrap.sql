-- One-time setup, run as the postgres superuser:
--   sudo -u postgres psql -v collector_password=... -v reader_password=... \
--        -f deploy/postgres/bootstrap.sql
-- Creates the two roles and the database. Tables come from migrations (python -m nestor.migrate).

\set ON_ERROR_STOP on
\if :{?db}
\else
  \set db nestor
\endif

-- Collector: owns the schema, the only role that writes.
CREATE ROLE nestor LOGIN PASSWORD :'collector_password';
-- Hermes MCP server: SELECT on the reader.* views only.
CREATE ROLE hermes_reader LOGIN PASSWORD :'reader_password';
-- Second line of defence: every transaction of this role is read-only.
ALTER ROLE hermes_reader SET default_transaction_read_only = on;

CREATE DATABASE :"db" OWNER nestor ENCODING 'UTF8' TEMPLATE template0;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO hermes_reader;

\connect :"db"
-- PostgreSQL 15+ already denies CREATE on public; also hide its tables from other roles.
ALTER SCHEMA public OWNER TO nestor;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
