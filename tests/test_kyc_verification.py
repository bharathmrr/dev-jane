import pytest
from unittest.mock import patch, MagicMock
from app.services.kyc_verify import run_kyc_verification

def test_kyc_indian_auto_approved():
    """
    Test Indian Company where GSTIN format is valid, 
    and mock API successfully returns the matching business name.
    Should result in: overall_passed=True, auto_approvable=True (AUTO-APPROVED).
    """
    mock_gst_response = {
        "taxpayerInfo": {
            "tradeNam": "Tata Consultancy Services",
            "sts": "Active"
        }
    }
    
    with patch("app.services.kyc_verify.requests.get") as mock_get:
        # Configure mock response for GST lookup
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_gst_response
        mock_get.return_value = mock_resp
        
        result = run_kyc_verification(
            company_type="indian",
            company_name="Tata Consultancy Services",
            gstin="27AAACT1234A1Z5", # valid format
            pan="AAACT1234A",         # matches extracted PAN
            cin="L65110MH1995PLC089762"
        )
        
        assert result["overall_passed"] is True
        assert result["auto_approvable"] is True
        assert not result["issues"]
        assert "GST portal verified name: Tata Consultancy Services" in result["notes"]


def test_kyc_indian_manual_review_api_unreachable():
    """
    Test Indian Company where GSTIN format is valid,
    but the GST API is unreachable or returns a bad status code.
    Should result in: overall_passed=True, auto_approvable=False (MANUAL REVIEW).
    """
    with patch("app.services.kyc_verify.requests.get") as mock_get:
        # Mock API returning an error
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_get.return_value = mock_resp
        
        result = run_kyc_verification(
            company_type="indian",
            company_name="Tata Consultancy Services",
            gstin="27AAACT1234A1Z5", # valid format
            pan="AAACT1234A",
            cin="L65110MH1995PLC089762"
        )
        
        assert result["overall_passed"] is True
        assert result["auto_approvable"] is False
        assert not result["issues"]
        assert any("GST portal returned HTTP 502" in note or "unreachable" in note for note in result["notes"])


def test_kyc_indian_rejected_invalid_gstin_format():
    """
    Test Indian Company with an invalid GSTIN format.
    Should result in: overall_passed=False, auto_approvable=False (REJECTED).
    """
    result = run_kyc_verification(
        company_type="indian",
        company_name="Reliance Industries",
        gstin="INVALIDGSTIN",
        pan="AAACT1234A",
        cin="L65110MH1995PLC089762"
    )
    
    assert result["overall_passed"] is False
    assert result["auto_approvable"] is False
    assert any("GSTIN: Invalid GSTIN format" in issue for issue in result["issues"])


def test_kyc_indian_rejected_pan_mismatch():
    """
    Test Indian Company with a valid GSTIN format,
    but the provided PAN does not match the PAN extracted from the GSTIN.
    Should result in: overall_passed=False, auto_approvable=False (REJECTED).
    """
    result = run_kyc_verification(
        company_type="indian",
        company_name="Tata Consultancy Services",
        gstin="27AAACT1234A1Z5",  # extracted PAN is AAACT1234A
        pan="BBBCB1234B",          # does not match AAACT1234A
        cin="L65110MH1995PLC089762"
    )
    
    assert result["overall_passed"] is False
    assert result["auto_approvable"] is False
    assert any("PAN: PAN BBBCB1234B does not match PAN in GSTIN" in issue for issue in result["issues"])


def test_kyc_overseas_auto_approved():
    """
    Test Overseas Company with valid optional formats.
    Should result in: overall_passed=True, auto_approvable=True (AUTO-APPROVED)
    since GST/PAN checks are skipped for Overseas.
    """
    result = run_kyc_verification(
        company_type="overseas",
        company_name="Acme Corp",
        gstin=None,
        pan=None,
        cin="L65110MH1995PLC089762"
    )
    
    assert result["overall_passed"] is True
    assert result["auto_approvable"] is True
    assert not result["issues"]
    assert any("Overseas company" in note and "GST/PAN verification skipped" in note for note in result["notes"])


def test_kyc_overseas_rejected_invalid_cin_format():
    """
    Test Overseas Company with an invalid CIN format.
    Should result in: overall_passed=False, auto_approvable=False (REJECTED).
    """
    result = run_kyc_verification(
        company_type="overseas",
        company_name="Acme Corp",
        gstin=None,
        pan=None,
        cin="INVALID_CIN_FORMAT"
    )
    
    assert result["overall_passed"] is False
    assert result["auto_approvable"] is False
    assert any("CIN: Invalid CIN format" in issue for issue in result["issues"])


def test_kyc_overseas_rejected_invalid_ifsc_format():
    """
    Test Overseas Company with invalid IFSC format.
    Should result in: overall_passed=False, auto_approvable=False (REJECTED).
    """
    result = run_kyc_verification(
        company_type="overseas",
        company_name="Acme Corp",
        gstin=None,
        pan=None,
        cin=None,
        ifsc="INVALIDIFSC"
    )
    
    assert result["overall_passed"] is False
    assert result["auto_approvable"] is False
    assert any("IFSC: Invalid IFSC format" in issue for issue in result["issues"])
