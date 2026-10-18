from io import BytesIO

from pypdf import PdfReader


MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 30
MAX_TEXT_CHARS = 60000


def read_syllabus_pdf(uploaded_file):
    if uploaded_file.size > MAX_PDF_BYTES:
        raise ValueError("PDFs must be 5 MiB or smaller.")
    header = uploaded_file.read(5)
    uploaded_file.seek(0)
    if header != b"%PDF-":
        raise ValueError("The selected file is not a valid PDF.")
    try:
        reader = PdfReader(BytesIO(uploaded_file.read()), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported. Paste the text instead.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDFs must contain 30 pages or fewer.")
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            if text:
                pages.append(f"[Page {index}]\n{text}")
        result = "\n\n".join(pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("This PDF could not be read. Try a searchable, unencrypted PDF.") from exc
    if not result:
        raise ValueError("No selectable text found. Paste the syllabus text; scanned PDFs are not supported.")
    if len(result) > MAX_TEXT_CHARS:
        raise ValueError("Extracted PDF text exceeds 60,000 characters.")
    return result


def normalize_text(text):
    return " ".join(text.split())


def page_texts(text):
    pages = {}
    current_page = 1
    chunks = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[Page ") and stripped.endswith("]"):
            if chunks:
                pages[current_page] = normalize_text(" ".join(chunks))
            try:
                current_page = int(stripped[6:-1])
            except ValueError:
                chunks.append(line)
            else:
                chunks = []
        else:
            chunks.append(line)
    if chunks:
        pages[current_page] = normalize_text(" ".join(chunks))
    if not pages:
        pages[1] = normalize_text(text)
    return pages
