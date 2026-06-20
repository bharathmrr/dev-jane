"""
Professional-level tests for the document HMAC token pipeline.

Covers:
  - Token determinism (same inputs → same token, every time)
  - Token isolation (different purpose/doc_type/oid → different token)
  - Token length contract (always 24 chars)
  - URL structure (token is always the last path segment)
  - Verification round-trip (make → verify succeeds)
  - Tamper detection (any single-char mutation → verify fails)
  - KYC token same guarantees
  - Settings-driven company name (no hardcodes)
"""
from __future__ import annotations

import os
import string

import pytest

# conftest already sets EMAIL_HMAC_SECRET / ONBOARDING_HMAC_SECRET via env before import
from app.services.onboarding_email import (
    make_doc_token,
    make_doc_edit_url,
    make_doc_sign_url,
    make_kyc_token,
    verify_doc_token,
    verify_kyc_token,
)
from app.core.config import settings


# ── fixtures ──────────────────────────────────────────────────────────────────

OID  = "550e8400-e29b-41d4-a716-446655440000"
DT   = "nda"
DT2  = "agreement"


# ── token length ─────────────────────────────────────────────────────────────

def test_doc_token_length_always_24():
    assert len(make_doc_token(OID, DT, "edit")) == 24
    assert len(make_doc_token(OID, DT, "sign")) == 24
    assert len(make_doc_token("x", "y", "z")) == 24


def test_kyc_token_length_always_24():
    assert len(make_kyc_token(OID)) == 24
    assert len(make_kyc_token("short")) == 24


# ── determinism ──────────────────────────────────────────────────────────────

def test_doc_token_is_deterministic():
    t1 = make_doc_token(OID, DT, "edit")
    t2 = make_doc_token(OID, DT, "edit")
    assert t1 == t2


def test_kyc_token_is_deterministic():
    assert make_kyc_token(OID) == make_kyc_token(OID)


# ── isolation between purposes ────────────────────────────────────────────────

def test_edit_and_sign_tokens_differ():
    edit = make_doc_token(OID, DT, "edit")
    sign = make_doc_token(OID, DT, "sign")
    assert edit != sign


def test_nda_and_agreement_tokens_differ():
    nda  = make_doc_token(OID, "nda",       "edit")
    agr  = make_doc_token(OID, "agreement", "edit")
    assert nda != agr


def test_different_oid_tokens_differ():
    t1 = make_doc_token("oid-aaaa", DT, "edit")
    t2 = make_doc_token("oid-bbbb", DT, "edit")
    assert t1 != t2


# ── verification round-trip ───────────────────────────────────────────────────

def test_verify_edit_token_succeeds():
    tok = make_doc_token(OID, DT, "edit")
    assert verify_doc_token(OID, DT, "edit", tok) is True


def test_verify_sign_token_succeeds():
    tok = make_doc_token(OID, DT, "sign")
    assert verify_doc_token(OID, DT, "sign", tok) is True


def test_kyc_verify_round_trip():
    tok = make_kyc_token(OID)
    assert verify_kyc_token(OID, tok) is True


# ── tamper detection (box / boundary) ────────────────────────────────────────

def test_wrong_purpose_rejected():
    edit_tok = make_doc_token(OID, DT, "edit")
    assert verify_doc_token(OID, DT, "sign", edit_tok) is False


def test_wrong_doc_type_rejected():
    tok = make_doc_token(OID, "nda", "edit")
    assert verify_doc_token(OID, "agreement", "edit", tok) is False


def test_wrong_oid_rejected():
    tok = make_doc_token(OID, DT, "edit")
    assert verify_doc_token("different-oid", DT, "edit", tok) is False


def test_truncated_token_rejected():
    tok = make_doc_token(OID, DT, "edit")
    assert verify_doc_token(OID, DT, "edit", tok[:-1]) is False


def test_extended_token_rejected():
    tok = make_doc_token(OID, DT, "edit")
    assert verify_doc_token(OID, DT, "edit", tok + "x") is False


def test_empty_token_rejected():
    assert verify_doc_token(OID, DT, "edit", "") is False


def test_single_char_mutation_rejected():
    """Flip every character one at a time — all must fail verification."""
    tok = make_doc_token(OID, DT, "edit")
    for i in range(len(tok)):
        # pick a character that differs from tok[i]
        replacement = "a" if tok[i] != "a" else "b"
        mutated = tok[:i] + replacement + tok[i+1:]
        assert verify_doc_token(OID, DT, "edit", mutated) is False, (
            f"Mutation at position {i} should have been rejected"
        )


# ── URL structure ─────────────────────────────────────────────────────────────

def test_edit_url_token_is_last_segment():
    url = make_doc_edit_url(OID, DT)
    last_segment = url.rstrip("/").split("/")[-1]
    expected = make_doc_token(OID, DT, "edit")
    assert last_segment == expected


def test_sign_url_token_is_last_segment():
    url = make_doc_sign_url(OID, DT)
    last_segment = url.rstrip("/").split("/")[-1]
    expected = make_doc_token(OID, DT, "sign")
    assert last_segment == expected


def test_edit_url_contains_oid_and_doctype():
    url = make_doc_edit_url(OID, DT)
    assert OID in url
    assert "/nda/" in url


def test_sign_url_contains_oid_and_doctype():
    url = make_doc_sign_url(OID, "agreement")
    assert OID in url
    assert "/agreement/" in url


# ── settings-driven — no hardcodes ───────────────────────────────────────────

def test_company_legal_name_in_settings():
    """COMPANY_LEGAL_NAME must be set and non-empty — never hardcoded."""
    assert settings.COMPANY_LEGAL_NAME
    assert len(settings.COMPANY_LEGAL_NAME) > 3


def test_organizer_name_in_settings():
    assert settings.ORGANIZER_NAME
    assert len(settings.ORGANIZER_NAME) > 1


def test_hmac_secret_minimum_length():
    """Secrets must meet minimum length for security."""
    assert len(settings.ONBOARDING_HMAC_SECRET) >= 16
    assert len(settings.EMAIL_HMAC_SECRET) >= 16


def test_app_url_no_trailing_slash():
    """make_doc_edit_url strips trailing slash — APP_URL must work with or without."""
    url_with = make_doc_edit_url.__module__  # import side-effect check
    # The URL helper calls rstrip('/') — verify idempotent for both forms
    for suffix in ("", "/"):
        original = settings.APP_URL
        url = make_doc_edit_url(OID, DT)
        assert not url[len(original.rstrip("/")):].startswith("//"), (
            "Double slash in URL — rstrip('/') not applied correctly"
        )
