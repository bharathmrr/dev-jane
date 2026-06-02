"""Drop all tables and types in the Neon database, then recreate via Alembic."""
import subprocess
import sys

import psycopg
from app.core.config import settings

DSN = settings.database_url_sync.replace("postgresql+psycopg", "postgresql")

DROP_SQL = """
DO $$ DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
    END LOOP;
END $$;

DO $$ DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT typname FROM pg_type WHERE typtype = 'e' AND typnamespace = 'public'::regnamespace) LOOP
        EXECUTE 'DROP TYPE IF EXISTS ' || quote_ident(r.typname) || ' CASCADE';
    END LOOP;
END $$;
"""

print("Connecting to Neon DB...")
with psycopg.connect(DSN, autocommit=True) as conn:
    print("Dropping all tables and enum types...")
    conn.execute(DROP_SQL)
    print("Done. Database is clean.")
