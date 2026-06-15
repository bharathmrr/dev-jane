import sys, io
sys.path.insert(0, '.')
import fitz

# tiny red PNG signature stand-in
pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 40))
pix.clear_with(220)
png = pix.tobytes("png")

arch = fitz.Archive()
arch.add(str(__import__('pathlib').Path('app/templates/fonts')))
arch.add(png, "sig1.png")   # buffer entry
css = "@font-face { font-family: carlito; src: url(Carlito-Regular.ttf); } body{font-family:carlito;}"
html = '<p>Before image</p><p><img src="sig1.png" style="width:120pt;"></p><p>After image</p>'
story = fitz.Story(html=html, user_css=css, archive=arch)
buf = io.BytesIO()
w = fitz.DocumentWriter(buf)
dev = w.begin_page(fitz.paper_rect('a4'))
more, filled = story.place(fitz.Rect(50, 50, 545, 800))
story.draw(dev)
w.end_page(); w.close()
buf.seek(0)
doc = fitz.open(stream=buf.getvalue(), filetype='pdf')
print('images on page:', len(doc[0].get_images()), '| filled h:', filled[3] if filled else 0)
doc.close()
