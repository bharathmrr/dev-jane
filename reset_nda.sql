UPDATE onboarding_records
SET
  nda_status = 'PENDING',
  nda_status_display = 'NDA Pending',
  nda_sent_at = NULL,
  nda_draft_content = (
    nda_draft_content::jsonb ||
    '{"stage": "draft", "comments": [], "signature": null, "internal_signature": null}'::jsonb
  )::text
WHERE id = 'b7173420-076c-4b8a-a0e4-257cf557edaf';
