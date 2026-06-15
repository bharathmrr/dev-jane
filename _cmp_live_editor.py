"""Visual comparison: original template PDF vs Live-Editor round-trip output.

For each template, render:
  _cmp/orig_{name}_p{N}.png      — original template page N
  _cmp/live_{name}_p{N}.png      — extract_editable_html() -> live_html_to_pdf() page N
"""
import sys
sys.path.insert(0, '.')
import pathlib

import fitz

from app.services.pdf_documents import (
    extract_editable_html,
    live_html_to_pdf,
    sanitize_live_html,
    template_path,
)

OUT = pathlib.Path('_cmp')
OUT.mkdir(exist_ok=True)

PAGES = 3  # first N pages of each

for doc_type, ov, name in (('nda', False, 'nda_in'), ('agreement', False, 'agree')):
    # original
    orig = fitz.open(template_path(doc_type, ov))
    for i in range(min(PAGES, orig.page_count)):
        orig[i].get_pixmap(dpi=80).save(OUT / f'orig_{name}_p{i+1}.png')
    n_orig = orig.page_count
    orig.close()

    # live-editor round trip (no edits at all), through the save sanitizer
    html = sanitize_live_html(extract_editable_html(doc_type, ov, []))
    pdf = live_html_to_pdf(html)
    live = fitz.open(stream=pdf, filetype='pdf')
    for i in range(min(PAGES, live.page_count)):
        live[i].get_pixmap(dpi=80).save(OUT / f'live_{name}_p{i+1}.png')
    n_live = live.page_count
    live.close()

    print(f'{name}: orig {n_orig} pages -> live {n_live} pages | html {len(html)} chars')

print('done')
