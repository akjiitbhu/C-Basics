"""Make a scanned-looking statement PDF (an image with no text layer, slightly skewed and soft).

    python tests/make_scanned_sample.py out.pdf [table|text]

Used by the Windows build to prove that OCR works inside the packaged exe.
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def make(out_path, kind="table"):
    import pypdfium2 as pdfium
    from PIL import ImageFilter
    from tests import pdf_samples
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src.pdf")
    (pdf_samples.table_pdf if kind == "table" else pdf_samples.text_pdf)(src)
    img = pdfium.PdfDocument(src)[0].render(scale=200 / 72).to_pil().convert("L")
    img = img.rotate(0.6, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.7))
    px = img.load()
    random.seed(1)
    for _ in range(img.width * img.height // 150):                      # speckle noise, like a photocopy
        x, y = random.randrange(img.width), random.randrange(img.height)
        px[x, y] = max(0, px[x, y] - random.randrange(80))
    img.save(out_path, "PDF", resolution=200)


if __name__ == "__main__":
    make(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "table")
    print("wrote", sys.argv[1])
