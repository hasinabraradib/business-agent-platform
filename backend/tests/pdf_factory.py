"""Minimal PDF writer for tests, so no PDF-generation dependency is needed."""


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _stream(content: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s>>\nstream\n" % (len(content), extra) + content + b"\nendstream"


def text_pdf(pages: list[list[str]]) -> bytes:
    """A PDF whose pages contain the given lines of (Latin-1) text."""
    page_count = len(pages)
    # 1 catalog, 2 page tree, 3 font, then (page, content) pairs.
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(page_count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        ops = "BT /F1 12 Tf 16 TL 72 720 Td " + " ".join(
            f"({_escape(line)}) Tj T*" for line in lines
        )
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(_stream((ops + " ET").encode("latin-1")))
    return _assemble(objects)


def scanned_pdf() -> bytes:
    """A one-page PDF that only draws an image, like a scan: no text layer at all."""
    pixels = bytes([0, 255, 255, 0])  # 2x2 grayscale
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
        _stream(b"q 500 0 0 700 50 50 cm /Im1 Do Q"),
        _stream(
            pixels,
            b"/Type /XObject /Subtype /Image /Width 2 /Height 2 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 ",
        ),
    ]
    return _assemble(objects)
