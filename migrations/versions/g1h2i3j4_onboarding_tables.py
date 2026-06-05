"""Add onboarding_records and kyc_submissions tables.

Revision ID: g1h2i3j4
Revises: f8e9d0c1b2a3
Create Date: 2026-06-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'g1h2i3j4'
down_revision = 'f8e9d0c1b2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enums
    op.execute(sa.text("""
        DO $$ BEGIN
          CREATE TYPE company_type_enum AS ENUM ('indian', 'overseas');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
          CREATE TYPE kyc_status_enum AS ENUM
            ('PENDING','FORM_SENT','SUBMITTED','UNDER_REVIEW','REJECTED','APPROVED');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
          CREATE TYPE nda_status_enum AS ENUM
            ('PENDING','DRAFT_GENERATED','TEAM_REVIEW','DRAFT_REJECTED',
             'SENT_TO_LEAD','SIGNED_RECEIVED','SIGN_UNDER_REVIEW','SIGN_REJECTED',
             'APPROVED','PROCEED_NEXT');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
          CREATE TYPE agreement_status_enum AS ENUM
            ('PENDING','DRAFT_GENERATED','TEAM_REVIEW','DRAFT_REJECTED',
             'SENT_TO_LEAD','SIGNED_RECEIVED','SIGN_UNDER_REVIEW','SIGN_REJECTED',
             'APPROVED','PROCEED_NEXT');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
          CREATE TYPE kyc_company_type_enum AS ENUM ('indian', 'overseas');
        EXCEPTION WHEN duplicate_object THEN null; END $$;
    """))

    # onboarding_records
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS onboarding_records (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            lead_id         UUID NOT NULL REFERENCES leads_v2(id) ON DELETE CASCADE,
            company_type    company_type_enum,

            -- KYC
            kyc_status           kyc_status_enum NOT NULL DEFAULT 'PENDING',
            kyc_status_display   VARCHAR(512),
            kyc_form_token       VARCHAR(128),
            kyc_form_sent_at     TIMESTAMPTZ,
            kyc_submitted_at     TIMESTAMPTZ,
            kyc_approved_at      TIMESTAMPTZ,
            kyc_followup_count   INTEGER NOT NULL DEFAULT 0,
            kyc_last_followup_at TIMESTAMPTZ,

            -- NDA
            nda_status                nda_status_enum NOT NULL DEFAULT 'PENDING',
            nda_status_display        VARCHAR(512),
            nda_draft_content         TEXT,
            nda_draft_zoho_file_id    VARCHAR(255),
            nda_signed_zoho_file_id   VARCHAR(255),
            nda_sent_at               TIMESTAMPTZ,
            nda_signed_received_at    TIMESTAMPTZ,
            nda_approved_at           TIMESTAMPTZ,
            nda_followup_count        INTEGER NOT NULL DEFAULT 0,
            nda_last_followup_at      TIMESTAMPTZ,
            nda_draft_revision        INTEGER NOT NULL DEFAULT 0,
            nda_team_notes            TEXT,

            -- Customer Agreement
            agreement_status                agreement_status_enum NOT NULL DEFAULT 'PENDING',
            agreement_status_display        VARCHAR(512),
            agreement_draft_content         TEXT,
            agreement_draft_zoho_file_id    VARCHAR(255),
            agreement_signed_zoho_file_id   VARCHAR(255),
            agreement_sent_at               TIMESTAMPTZ,
            agreement_signed_received_at    TIMESTAMPTZ,
            agreement_approved_at           TIMESTAMPTZ,
            agreement_followup_count        INTEGER NOT NULL DEFAULT 0,
            agreement_last_followup_at      TIMESTAMPTZ,
            agreement_draft_revision        INTEGER NOT NULL DEFAULT 0,
            agreement_team_notes            TEXT,

            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

            CONSTRAINT uq_onboarding_lead UNIQUE (lead_id)
        );
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_onboarding_lead_id ON onboarding_records(lead_id);"))

    # kyc_submissions
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS kyc_submissions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            onboarding_id   UUID NOT NULL REFERENCES onboarding_records(id) ON DELETE CASCADE,
            attempt_number  INTEGER NOT NULL DEFAULT 1,

            company_type    kyc_company_type_enum NOT NULL,
            company_name    VARCHAR(255) NOT NULL,
            contact_name    VARCHAR(255) NOT NULL,
            contact_number  VARCHAR(50)  NOT NULL,

            gst_certificate_zoho_id  VARCHAR(255),
            gst_certificate_filename VARCHAR(255),
            incorporation_zoho_id    VARCHAR(255),
            incorporation_filename   VARCHAR(255),

            reviewer_notes  TEXT,
            reviewed_at     TIMESTAMPTZ,

            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_kyc_onboarding_id ON kyc_submissions(onboarding_id);"))

    # Auto-update updated_at trigger for onboarding_records
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ language 'plpgsql';
    """))
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS set_onboarding_updated_at ON onboarding_records;
        CREATE TRIGGER set_onboarding_updated_at
          BEFORE UPDATE ON onboarding_records
          FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    """))
    op.execute(sa.text("""
        DROP TRIGGER IF EXISTS set_kyc_updated_at ON kyc_submissions;
        CREATE TRIGGER set_kyc_updated_at
          BEFORE UPDATE ON kyc_submissions
          FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS kyc_submissions CASCADE;"))
    op.execute(sa.text("DROP TABLE IF EXISTS onboarding_records CASCADE;"))
    op.execute(sa.text("DROP TYPE IF EXISTS agreement_status_enum;"))
    op.execute(sa.text("DROP TYPE IF EXISTS nda_status_enum;"))
    op.execute(sa.text("DROP TYPE IF EXISTS kyc_status_enum;"))
    op.execute(sa.text("DROP TYPE IF EXISTS kyc_company_type_enum;"))
    op.execute(sa.text("DROP TYPE IF EXISTS company_type_enum;"))
