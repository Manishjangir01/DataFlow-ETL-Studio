-- Runs once, when the PostgreSQL volume is first created.
-- The "dataflow" database (metadata) is created by POSTGRES_DB; this adds a separate
-- "warehouse" database that pipelines can load into (WAREHOUSE_PG_URL).
CREATE DATABASE warehouse OWNER dataflow;
