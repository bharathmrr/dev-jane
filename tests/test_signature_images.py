from app.api.v1.document_endpoints import _clean_sig_image


def test_clean_sig_image_accepts_png_and_jpeg_data_urls():
    png = "data:image/png;base64," + ("A" * 44)
    jpeg = "data:image/jpeg;base64," + ("B" * 44)

    assert _clean_sig_image(png) == png
    assert _clean_sig_image(jpeg) == jpeg


def test_clean_sig_image_rejects_unsupported_or_empty_images():
    assert _clean_sig_image("") == ""
    assert _clean_sig_image("data:image/svg+xml;base64," + ("A" * 44)) == ""
    assert _clean_sig_image("data:image/png;base64,short") == ""
