import sys
sys.path.insert(0, '.')
import fitz
from app.services.pdf_documents import (
    apply_replacements_html,
    default_replacements,
    html_to_pdf,
    kyc_fields_from_submission,
    template_html,
)

fields = kyc_fields_from_submission(
    company_name='Sarus Aerospace Pvt Ltd',
    cin_number='U12345KA2020PTC098765',
    extra={'registered_address': 'G-11, Century Park', 'city': 'Bengaluru',
           'state': 'Karnataka', 'pin_code': '560001'},
    is_overseas=False,
)

for dt_, ov in (('nda', False), ('nda', True), ('agreement', False)):
    h = template_html(dt_, ov)
    reps = default_replacements(dt_, fields)
    filled = apply_replacements_html(h, reps)
    left = [p for p in ('[Date]', '[Company Name]', '[Company Registration Number]',
                        '[Address]', '[ABC]') if p in filled]
    pdf = html_to_pdf(filled)
    doc = fitz.open(stream=pdf, filetype='pdf')
    print(dt_, 'ov' if ov else 'in',
          '| html', len(h), 'chars | leftover:', left or 'NONE',
          '| company x', filled.count('Sarus Aerospace Pvt Ltd'),
          '| pdf pages:', doc.page_count, '| bytes:', len(pdf))
    if dt_ == 'nda' and not ov:
        doc[0].get_pixmap(dpi=100).save('_html_p1.png')
    doc.close()
print('done')
