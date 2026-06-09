-- Migration: Add KYC verification number fields and auto-verification result to kyc_submissions
-- Run once against the Neon DB after deploying the code changes.

ALTER TABLE kyc_submissions
  ADD COLUMN IF NOT EXISTS gstin_number VARCHAR(20),
  ADD COLUMN IF NOT EXISTS pan_number VARCHAR(20),
  ADD COLUMN IF NOT EXISTS cin_number VARCHAR(25),
  ADD COLUMN IF NOT EXISTS kyc_verification_result JSONB DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS auto_verified BOOLEAN DEFAULT FALSE;
