import sys, io, base64
sys.path.insert(0, '.')
import fitz

import app.api.v1.document_endpoints as de
import app.services.pdf_documents as pd
from app.services.onboarding_ai import summarize_doc_comment
print('imports ok')

# tiny png as a fake signature
pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 60))
pix.clear_with(30)
png_b64 = base64.b64encode(pix.tobytes('png')).decode()
data_url = f'data:image/png;base64,{png_b64}'

# 1. sanitize keeps a valid sig img, strips a hostile one
html = (f'<p style="font-size:11pt;">Before</p><img src="{data_url}" style="width:150pt;">'
        f'<img src="http://evil/x.png"><img src="javascript:alert(1)">')
s = pd.sanitize_live_html(html)
assert s.count('<img') == 1 and 'data:image/png' in s and 'evil' not in s
assert pd.sanitize_live_html(s) == s, 'not idempotent with img'
print('sanitize img ok')

# 2. inline img renders in the live PDF
pdf = pd.live_html_to_pdf(s + '<p>After</p>')
doc = fitz.open(stream=pdf, filetype='pdf')
assert len(doc[0].get_images()) >= 1, 'inline image missing in PDF'
print('inline img in PDF ok, pages:', doc.page_count)
doc.close()

# 3. signature certificate page uses the uploaded image
base_pdf = pd.live_html_to_pdf('<p>Doc body</p>')
sig = {'signed_name': 'Asha Rao', 'designation': 'Director', 'email': 'a@x.com',
       'company_name': 'Sarus', 'signed_at': 'today', 'ip': '1.2.3.4',
       'sig_image': data_url}
out = pd.append_signature_page(base_pdf, 'nda', sig=sig,
                               internal_sig={'signed_name': 'Leo', 'company_name': 'JAPL',
                                             'signed_at': 'today', 'sig_font': 'standard'})
doc = fitz.open(stream=out, filetype='pdf')
last = doc[doc.page_count - 1]
assert len(last.get_images()) >= 1, 'sig image missing on certificate page'
print('certificate sig image ok')
doc.close()

# 4. validation helper
assert de._clean_sig_image(data_url) == data_url
assert de._clean_sig_image('data:image/svg+xml;base64,AAAA') == ''
assert de._clean_sig_image('http://x/y.png') == ''
print('sig validation ok')

# 5. AI comment summary degrades gracefully without API key
import app.core.config as cfg
old = cfg.settings.ANTHROPIC_API_KEY
cfg.settings.ANTHROPIC_API_KEY = ''
assert summarize_doc_comment('please change clause 7 notice to 30 days') == []
cfg.settings.ANTHROPIC_API_KEY = old
print('ai fallback ok')
