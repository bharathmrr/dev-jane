from app.services.kyc_verify import run_kyc_verification


def test_kyc_indian_valid_all_fields():
    """Valid Indian company — all fields pass format checks."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Tata Consultancy Services",
        gstin="27AAACT1234A1Z5",
        pan="AAACT1234A",
        cin="L65110MH1995PLC089762",
    )
    assert result["overall_passed"] is True
    assert not result["issues"]
    assert result["gstin_check"]["valid"] is True
    assert result["pan_check"]["valid"] is True


def test_kyc_indian_pan_extracted_from_gstin():
    """PAN omitted — auto-extracted from GSTIN, no issues."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Tata Consultancy Services",
        gstin="27AAACT1234A1Z5",
        pan=None,
        cin=None,
    )
    assert result["overall_passed"] is True
    assert not result["issues"]
    assert "PAN extracted from GSTIN" in result["notes"]


def test_kyc_indian_rejected_invalid_gstin_format():
    """Invalid GSTIN format → overall_passed=False with format error."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Reliance Industries",
        gstin="INVALIDGSTIN",
        pan="AAACT1234A",
        cin=None,
    )
    assert result["overall_passed"] is False
    assert result["auto_approvable"] is False
    assert any("Invalid GSTIN format" in issue for issue in result["issues"])


def test_kyc_indian_rejected_pan_mismatch():
    """PAN provided does not match PAN embedded in GSTIN."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Tata Consultancy Services",
        gstin="27AAACT1234A1Z5",   # embedded PAN = AAACT1234A
        pan="BBBCB1234B",           # mismatch
        cin=None,
    )
    assert result["overall_passed"] is False
    assert result["auto_approvable"] is False
    assert any("does not match PAN embedded in GSTIN" in issue for issue in result["issues"])


def test_kyc_indian_rejected_missing_gstin():
    """GSTIN omitted for Indian company → required field error."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Acme India",
        gstin=None,
        pan="AAACT1234A",
        cin=None,
    )
    assert result["overall_passed"] is False
    assert any("GSTIN is required" in issue for issue in result["issues"])


def test_kyc_indian_optional_cin_invalid():
    """CIN provided but invalid format for Indian company."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Acme India",
        gstin="27AAACT1234A1Z5",
        pan="AAACT1234A",
        cin="INVALID_CIN",
    )
    assert result["overall_passed"] is False
    assert any("Invalid CIN format" in issue for issue in result["issues"])


def test_kyc_indian_optional_ifsc_invalid():
    """IFSC provided but invalid format for Indian company."""
    result = run_kyc_verification(
        company_type="indian",
        company_name="Acme India",
        gstin="27AAACT1234A1Z5",
        pan="AAACT1234A",
        cin=None,
        ifsc="BADIFSC",
    )
    assert result["overall_passed"] is False
    assert any("Invalid IFSC format" in issue for issue in result["issues"])


def test_kyc_overseas_valid():
    """Valid overseas company — all required fields present."""
    result = run_kyc_verification(
        company_type="overseas",
        company_name="Acme Corp",
        gstin=None,
        pan=None,
        cin=None,
        country_of_incorporation="USA",
        company_reg_number="1234567",
        tax_id_tin="12-3456789",
    )
    assert result["overall_passed"] is True
    assert not result["issues"]


def test_kyc_overseas_missing_required_fields():
    """Overseas company missing all three required fields → 3 issues."""
    result = run_kyc_verification(
        company_type="overseas",
        company_name="Acme Corp",
        gstin=None,
        pan=None,
        cin=None,
    )
    assert result["overall_passed"] is False
    assert len([i for i in result["issues"] if "required" in i]) == 3


def test_kyc_overseas_invalid_lei():
    """Overseas company with invalid LEI format."""
    result = run_kyc_verification(
        company_type="overseas",
        company_name="Acme Corp",
        gstin=None,
        pan=None,
        cin=None,
        country_of_incorporation="UK",
        company_reg_number="SC123456",
        tax_id_tin="GB123456789",
        lei_number="INVALID_LEI",
    )
    assert result["overall_passed"] is False
    assert any("Invalid LEI format" in issue for issue in result["issues"])
