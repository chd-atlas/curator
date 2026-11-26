"""Converters and the full-text verdict. Every fixture is synthetic and built here."""

from __future__ import annotations

import io
import zipfile

import pytest

from chd_curator import convert as cv
from chd_curator.convert import Converted, NotText, convert, full_text_verdict, unpack
from chd_curator.quotes import normalise_text

JATS = b"""<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
<front><article-meta><title-group><article-title>A synthetic study</article-title></title-group>
<abstract><p>Short abstract here.</p></abstract></article-meta></front>
<body>
<sec><title>Methods</title><p>We read <italic>forty</italic> records.</p></sec>
<sec><title>Results</title><p>Carriers appeared.</p>
<table-wrap><label>Table 1</label><caption><p>Carriers</p></caption>
<table><thead><tr><th>Gene</th><th>Change</th></tr></thead>
<tbody><tr><td>GENEA</td><td>c.1A&gt;G</td></tr></tbody></table>
<table-wrap-foot><fn><p>Counted once per family.</p></fn></table-wrap-foot></table-wrap>
</sec></body>
<back><ref-list><ref><mixed-citation>Cited, 1999.</mixed-citation></ref></ref-list></back>
</article>"""

JATS_NO_BODY = (
    b"<article><front><article-meta><title-group><article-title>T</article-title>"
    b"</title-group></article-meta></front></article>"
)
NO_BODY = "no body text: the record has no <body>, or its <body> holds no element"


def _pdf(pages: list[str]) -> bytes:
    objs: list[bytes] = []
    n = len(pages)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
            f"/BaseFont /Helvetica >> >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def _zip(members: list[tuple[str, bytes]]) -> bytes:
    """A zip of `members`, the same bytes on every call: each member is dated 1980-01-01, not
    now. Dated now, two calls either side of a two-second step of a zip's clock gave different
    bytes (measured), so a test comparing a written workbook with `_xlsx()` could fail when a
    step fell inside its run."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


_CT = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
_SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xlsx_parts() -> list[tuple[str, bytes]]:
    workbook = (
        f'<workbook xmlns="{_SS}" xmlns:r="{_REL}"><sheets>'
        f'<sheet name="Alpha" sheetId="1" r:id="rId1"/>'
        f'<sheet name="Beta sheet" sheetId="2" r:id="rId2"/></sheets></workbook>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>'
    )
    shared = (
        f'<sst xmlns="{_SS}"><si><t>Gene</t></si><si><t>Count</t></si><si><t>GENEA</t></si></sst>'
    )
    sheet1 = (
        f'<worksheet xmlns="{_SS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>7</v></c></row>'
        "</sheetData></worksheet>"
    )
    sheet2 = (
        f'<worksheet xmlns="{_SS}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>left</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>right</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    return [
        ("[Content_Types].xml", _CT),
        ("xl/workbook.xml", workbook.encode()),
        ("xl/_rels/workbook.xml.rels", rels.encode()),
        ("xl/sharedStrings.xml", shared.encode()),
        ("xl/worksheets/sheet1.xml", sheet1.encode()),
        ("xl/worksheets/sheet2.xml", sheet2.encode()),
    ]


def _xlsx() -> bytes:
    return _zip(_xlsx_parts())


def _docx() -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = (
        f'<w:document xmlns:w="{w}"><w:body>'
        "<w:p><w:r><w:t>Opening line.</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Gene</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Change</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>GENEB</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>c.2T&gt;C</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>Closing line.</w:t></w:r></w:p></w:body></w:document>"
    )
    return _zip([("[Content_Types].xml", _CT), ("word/document.xml", doc.encode())])


def _words(n: int) -> str:
    return " ".join(f"term{i}" for i in range(n))


def _html(body: str) -> bytes:
    head = "<head><title>t</title><script>var x = 1;</script></head>"
    return f"<!DOCTYPE html><html>{head}<body>{body}</body></html>".encode()


LOGIN = _html("<h1>Sign in</h1><p>Please log in to read this article. Purchase access below.</p>")
FULL = _html(
    f"<h1>A synthetic article</h1><h2>Materials and Methods</h2><p>{_words(1200)}</p>"
    f"<h2>Results</h2><p>{_words(1200)}</p>"
    "<table><tr><th>Gene</th><th>n</th></tr><tr><td>GENEC</td><td>3</td></tr></table>"
)


def test_jats_keeps_titles_tables_and_footnotes_and_skips_references() -> None:
    c = convert("paper.xml", JATS)
    assert c.kind == "jats" and c.has_body
    assert c.text.splitlines() == [
        "A synthetic study",
        "## Abstract",
        "Short abstract here.",
        "## Methods",
        "We read forty records.",
        "## Results",
        "Carriers appeared.",
        "Table 1",
        "Carriers",
        "---row",
        "Gene | Change",
        "---row",
        "GENEA | c.1A>G",
        "[TABLE FOOTNOTE] Counted once per family.",
    ]
    assert c.words == len(c.text.split())
    assert "Cited" not in c.text


def test_a_table_row_round_trips_through_the_quote_normaliser() -> None:
    text = convert("paper.xml", JATS).text
    assert "geneA c.1A>G".lower() in normalise_text(text)


def test_pdf_pages_are_converted_in_order() -> None:
    c = convert("paper.pdf", _pdf(["First page words", "Second page words"]))
    assert c.kind == "pdf"
    assert c.text.index("First page words") < c.text.index("Second page words")
    assert c.words == 6


def test_xlsx_converts_every_sheet_and_every_row() -> None:
    c = convert("supp.xlsx", _xlsx())
    assert c.kind == "xlsx"
    assert c.text.splitlines() == [
        "## Sheet: Alpha",
        "---row",
        "Gene | Count",
        "---row",
        "GENEA | 7",
        "## Sheet: Beta sheet",
        "---row",
        "left |  | right",
    ]


def test_docx_converts_paragraphs_and_table_rows_in_order() -> None:
    c = convert("supp.docx", _docx())
    assert c.kind == "docx"
    assert c.text.splitlines() == [
        "Opening line.",
        "---row",
        "Gene | Change",
        "---row",
        "GENEB | c.2T>C",
        "Closing line.",
    ]


def test_html_keeps_headings_and_tables_and_drops_scripts() -> None:
    c = convert("page.html", FULL)
    assert c.kind == "html"
    lines = c.text.splitlines()
    assert lines[:2] == ["## A synthetic article", "## Materials and Methods"]
    assert "---row\nGene | n\n---row\nGENEC | 3" in c.text
    assert "var x" not in c.text


def test_magic_bytes_beat_the_extension() -> None:
    assert convert("paper.txt", _pdf(["one two"])).kind == "pdf"
    assert convert("data.bin", _xlsx()).kind == "xlsx"
    assert convert("notes.txt", b"plain words here").kind == "text"


@pytest.mark.parametrize(
    ("name", "data", "named"),
    [
        ("figure.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "PNG image"),
        ("old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20, ".xls"),
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20, ".doc"),
        ("blob.dat", b"\x01\x02\x03", ".dat"),
        ("bundle.zip", _zip([("a.txt", b"x")]), "zip"),
    ],
)
def test_other_types_raise_not_text_naming_the_type(name: str, data: bytes, named: str) -> None:
    with pytest.raises(NotText, match=named):
        convert(name, data)


def test_unpack_reduces_paths_to_base_names_and_keeps_every_file() -> None:
    z = _zip(
        [
            ("supp.xlsx", _xlsx()),
            ("sub/dir/notes.docx", _docx()),
            ("fig.png", b"\x89PNG"),
            ("../evil.txt", b"evil"),
        ]
    )
    unpacked = unpack(z)
    out = dict(unpacked.files)
    assert list(out) == ["supp.xlsx", "notes.docx", "fig.png", "evil.txt"]
    assert out["evil.txt"] == b"evil"
    assert unpacked.skipped == ()
    assert convert("supp.xlsx", out["supp.xlsx"]).kind == "xlsx"


def test_unpack_skips_oversize_members_and_members_past_the_total_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (cv.MAX_MEMBER_BYTES, cv.MAX_TOTAL_BYTES) == (50 * 1024 * 1024, 200 * 1024 * 1024)
    monkeypatch.setattr(cv, "MAX_MEMBER_BYTES", 10)
    monkeypatch.setattr(cv, "MAX_TOTAL_BYTES", 15)
    assert unpack(_zip([("a.txt", b"x" * 10)])).files == (("a.txt", b"x" * 10),)
    z = _zip(
        [
            ("a.txt", b"x" * 8),
            ("big.txt", b"x" * 11),
            ("b.txt", b"y" * 8),
            ("c.txt", b"z" * 7),
        ]
    )
    unpacked = unpack(z)
    assert unpacked.files == (("a.txt", b"x" * 8), ("c.txt", b"z" * 7))
    assert unpacked.skipped == (
        ("big.txt", "over 10 bytes uncompressed"),
        ("b.txt", "past the archive's total of 15 bytes uncompressed"),
    )


def test_unpack_skips_a_member_it_cannot_read_and_keeps_the_others() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("good.txt", b"kept")
        zf.writestr("damaged.txt", b"original bytes")
    data = buf.getvalue().replace(b"original bytes", b"altered bytes!")
    unpacked = unpack(data)
    assert unpacked.files == (("good.txt", b"kept"),)
    ((name, reason),) = unpacked.skipped
    assert name == "damaged.txt" and reason.startswith("cannot be read: BadZipFile")
    # a directory offset that points past where the directory is: zipfile seeks before the
    # archive's first byte and raises ValueError, which skips the member rather than the run
    shifted = bytearray(_zip([("a.txt", b"x" * 100)]))
    end = shifted.rfind(b"PK\x05\x06")
    offset = int.from_bytes(shifted[end + 16 : end + 20], "little")
    shifted[end + 16 : end + 20] = (offset + 1000).to_bytes(4, "little")
    assert unpack(bytes(shifted)) == cv.Unpacked(
        (), (("a.txt", "cannot be read: ValueError: negative seek value -1000"),)
    )


def test_verdict_refuses_landing_pages_and_short_pdfs_and_accepts_full_text() -> None:
    login = full_text_verdict(convert("p.html", LOGIN))
    assert login == "a landing page: 13 words, no methods heading, no results heading"
    assert full_text_verdict(convert("p.html", FULL)) is None
    assert full_text_verdict(convert("p.pdf", _pdf([_words(900)]))) == (
        "too short for a paper: 900 words, under 1,000"
    )
    assert full_text_verdict(convert("p.pdf", _pdf([_words(1000)]))) is None
    assert full_text_verdict(convert("p.xml", JATS)) is None
    assert full_text_verdict(convert("p.xml", JATS_NO_BODY)) == NO_BODY


def test_html_verdict_needs_both_headings_and_the_word_floor() -> None:
    long = Converted(_words(2000), "html", 2000)
    assert full_text_verdict(long, ["Methods", "Results"]) is None
    assert full_text_verdict(long, ["Methods"]) == "a landing page: 2000 words, no results heading"
    assert full_text_verdict(long, ["Results"]) == "a landing page: 2000 words, no methods heading"
    assert full_text_verdict(long, ["Patients", "Case report"]) is None
    short = Converted(_words(1999), "html", 1999)
    assert full_text_verdict(short, ["Methods", "Results"]) == "a landing page: 1999 words"


def test_supplement_kinds_are_not_judged_as_full_text() -> None:
    reason = full_text_verdict(convert("s.xlsx", _xlsx()))
    assert reason is not None and "xlsx" in reason


# --- fix round 1 ----------------------------------------------------------------------------


def test_unpack_names_colliding_base_names_by_path_and_keeps_unique_ones() -> None:
    z = _zip(
        [
            ("a/Table S1.xlsx", b"1"),
            ("b/Table S1.xlsx", b"2"),
            ("c/./unique.txt", b"3"),
            ("../d/Table S1.xlsx", b"4"),
        ]
    )
    assert unpack(z).files == (
        ("a__Table S1.xlsx", b"1"),
        ("b__Table S1.xlsx", b"2"),
        ("unique.txt", b"3"),
        ("d__Table S1.xlsx", b"4"),
    )
    still_equal = unpack(_zip([("a/x.txt", b"1"), ("a//x.txt", b"2"), ("a/./x.txt", b"3")]))
    assert still_equal.files == (("a__x.txt", b"1"), ("2__a__x.txt", b"2"), ("3__a__x.txt", b"3"))


def test_unpack_skips_members_named_dot_dotdot_or_empty() -> None:
    z = _zip([("a/..", b"1"), ("b/.", b"2"), ("ok.txt", b"3")])
    assert unpack(z).files == (("ok.txt", b"3"),)


def test_html_with_omitted_end_tags_keeps_its_cells_and_body() -> None:
    c = convert("p.html", b"<html><table><tr><td>a<td>b<td>c</tr><tr><th>d<th>e</table></html>")
    assert c.text.splitlines() == ["---row", "a | b | c", "---row", "d | e"]
    unclosed = b"<html><head><title>t</title><body><h2>Methods</h2><p>Some words.</p>"
    assert convert("p.html", unclosed).text.splitlines() == ["## Methods", "Some words."]
    closed = convert("p.html", b"<table><tr><td>a<td>b</tr>text after the row")
    assert closed.text.splitlines() == ["---row", "a | b", "text after the row"]
    open_at_end = convert("p.html", b"<table><tr><td>x<td>y")
    assert open_at_end.text.splitlines() == ["---row", "x | y"]


def test_html_declared_charset_and_bom_are_honoured() -> None:
    meta = "<html><head><meta charset=iso-8859-1></head><body><p>caf\xe9 words</p></body></html>"
    assert convert("p.html", meta.encode("latin-1")).text == "café words"
    http = (
        '<html><meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
        "<p>na\xefve</p>"
    )
    assert convert("p.html", http.encode("latin-1")).text == "naïve"
    xml_decl = '<?xml version="1.0" encoding="iso-8859-1"?><html><p>caf\xe9</p></html>'
    assert convert("p.html", xml_decl.encode("latin-1")).text == "café"
    assert convert("p.html", b"\xef\xbb\xbf<!DOCTYPE html><p>bom page</p>").text == "bom page"
    with pytest.raises(NotText, match="nosuch"):
        convert("p.html", b"<html><meta charset=nosuch><p>x</p></html>")


def test_docx_reads_content_controls_and_tracked_insertions() -> None:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = (
        f'<w:document xmlns:w="{w}"><w:body>'
        "<w:sdt><w:sdtContent><w:p><w:r><w:t>Inside control.</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>K</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>V</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:sdtContent></w:sdt>"
        "<w:ins><w:p><w:r><w:t>Inserted.</w:t></w:r></w:p></w:ins>"
        "<w:p><w:r><w:t>After.</w:t></w:r></w:p></w:body></w:document>"
    )
    c = convert("s.docx", _zip([("[Content_Types].xml", _CT), ("word/document.xml", doc.encode())]))
    assert c.text.splitlines() == ["Inside control.", "---row", "K | V", "Inserted.", "After."]


def test_jats_reads_floats_group_sub_articles_and_text_after_a_table() -> None:
    xml = b"""<article><front><article-meta><title-group><article-title>Main</article-title>
</title-group></article-meta></front>
<body><sec><title>Results</title>
<p>Before <table-wrap><table><tr><td>a</td></tr></table></table-wrap>
after the table.</p></sec></body>
<floats-group><fig><caption><p>A floated figure.</p></caption></fig></floats-group>
<sub-article><front-stub><title-group><article-title>Reply</article-title></title-group>
</front-stub><body><p>Reply body text.</p></body></sub-article></article>"""
    c = convert("p.xml", xml)
    assert c.text.splitlines() == [
        "Main",
        "## Results",
        "Before",
        "---row",
        "a",
        "after the table.",
        "A floated figure.",
        "Reply",
        "Reply body text.",
    ]
    assert c.has_body


def test_xlsx_refuses_a_column_beyond_xfd_and_a_total_over_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def book(sheet_cell: str) -> bytes:
        parts = dict(_xlsx_parts())
        parts["xl/worksheets/sheet1.xml"] = (
            f'<worksheet xmlns="{_SS}"><sheetData><row r="1">{sheet_cell}</row>'
            "</sheetData></worksheet>"
        ).encode()
        return _zip(list(parts.items()))

    ok = convert("s.xlsx", book('<c r="XFD1" t="inlineStr"><is><t>edge</t></is></c>'))
    assert ok.text.splitlines()[2].endswith("| edge")
    with pytest.raises(NotText, match="XFE1"):
        convert("s.xlsx", book('<c r="XFE1" t="inlineStr"><is><t>far</t></is></c>'))
    with pytest.raises(NotText, match="ZZZZZZ1"):
        convert("s.xlsx", book('<c r="ZZZZZZ1"><v>1</v></c>'))
    monkeypatch.setattr(cv, "MAX_TOTAL_BYTES", 600)
    with pytest.raises(NotText, match="in total"):
        convert("s.xlsx", _xlsx())


def test_xlsx_shared_string_index_errors_are_refusals() -> None:
    parts = dict(_xlsx_parts())
    parts["xl/worksheets/sheet1.xml"] = (
        f'<worksheet xmlns="{_SS}"><sheetData><row r="1"><c r="B7" t="s"><v>99</v></c></row>'
        "</sheetData></worksheet>"
    ).encode()
    with pytest.raises(NotText, match="B7"):
        convert("s.xlsx", _zip(list(parts.items())))
    del parts["xl/sharedStrings.xml"]
    with pytest.raises(NotText, match="B7"):
        convert("s.xlsx", _zip(list(parts.items())))


# --- Stage B fix wave -----------------------------------------------------------------------


def _encrypted_pdf(user_password: str, algorithm: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf([_words(1200)]))))
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm=algorithm)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("algorithm", ["AES-128", "AES-256"])
def test_an_aes_pdf_with_an_empty_user_password_converts(algorithm: str) -> None:
    c = convert("paper.pdf", _encrypted_pdf("", algorithm))
    assert (c.kind, c.words) == ("pdf", 1200)
    assert full_text_verdict(c) is None


def test_a_pdf_that_needs_a_user_password_is_not_text_and_says_so() -> None:
    with pytest.raises(NotText, match="^a PDF that needs a password to open$"):
        convert("paper.pdf", _encrypted_pdf("secret", "AES-128"))


def test_table_footnote_marks_stay_on_their_cells_and_label_their_footnotes() -> None:
    xml = b"""<article><body><sec><title>Results</title><table-wrap><label>Table 1</label><table>
<tr><td>GENEA</td><td>c.1A&gt;G<xref ref-type="table-fn" rid="a">a</xref></td></tr>
<tr><td>GENEB</td><td>c.2T&gt;C<sup><xref ref-type="table-fn" rid="b">b</xref></sup></td></tr>
<tr><td>GENEC</td><td>c.3G&gt;A<xref ref-type="table-fn" rid="c">*</xref></td></tr>
<tr><td>GENED</td><td>c.4C&gt;T<xref ref-type="bibr" rid="r1">7</xref></td></tr></table>
<table-wrap-foot><fn id="a"><label>a</label><p>De novo.</p></fn>
<fn-group><fn id="b"><label>b</label><p>Inherited from the father.</p><p>Confirmed.</p></fn>
<fn id="c"><p>Not labelled in its own footnote.</p></fn></fn-group>
<p>Counted once per family.</p></table-wrap-foot></table-wrap></sec></body></article>"""
    c = convert("p.xml", xml)
    assert c.text.splitlines() == [
        "## Results",
        "Table 1",
        "---row",
        "GENEA | c.1A>G [a]",
        "---row",
        "GENEB | c.2T>C [b]",
        "---row",
        "GENEC | c.3G>A [*]",
        "---row",
        "GENED | c.4C>T7",
        "[TABLE FOOTNOTE] [a] De novo.",
        "[TABLE FOOTNOTE] [b] Inherited from the father. Confirmed.",
        "[TABLE FOOTNOTE] [*] Not labelled in its own footnote.",
        "[TABLE FOOTNOTE] Counted once per family.",
    ]


def _one_paragraph_docx(runs: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = f'<w:document xmlns:w="{w}"><w:body><w:p>{runs}</w:p></w:body></w:document>'
    return _zip([("[Content_Types].xml", _CT), ("word/document.xml", doc.encode())])


def test_docx_hyphens_are_written_and_a_soft_hyphen_is_not() -> None:
    hard = "<w:r><w:t>c.123</w:t><w:noBreakHyphen/><w:t>4A&gt;G</w:t></w:r>"
    assert convert("s.docx", _one_paragraph_docx(hard)).text == "c.123-4A>G"
    soft = "<w:r><w:t>hetero</w:t><w:softHyphen/><w:t>zygous</w:t></w:r>"
    assert convert("s.docx", _one_paragraph_docx(soft)).text == "heterozygous"


@pytest.mark.parametrize(
    ("font", "char", "written"),
    [
        ("Symbol", "F0A3", "≤"),
        ("Symbol", "F0B3", "≥"),
        ("Symbol", "F0B1", "±"),
        ("Symbol", "F0B4", "×"),
        ("Symbol", "F0B8", "÷"),
        ("Symbol", "F0B0", "°"),
        ("Symbol", "F06D", "μ"),
        ("Symbol", "F061", "α"),
        ("Symbol", "F062", "β"),
        ("Symbol", "F044", "Δ"),
        ("Symbol", "F057", "Ω"),
        ("symbol", "00A3", "≤"),
        ("Symbol", "F0FF", "[symbol Symbol F0FF]"),
        ("Wingdings", "F0FC", "[symbol Wingdings F0FC]"),
        ("Symbol", "zz", "[symbol Symbol zz]"),
        (None, "F0A3", "[symbol (no font) F0A3]"),
    ],
)
def test_a_docx_symbol_is_its_character_or_named_never_dropped(
    font: str | None, char: str, written: str
) -> None:
    font_attr = f' w:font="{font}"' if font else ""
    runs = f'<w:r><w:t xml:space="preserve">p </w:t><w:sym{font_attr} w:char="{char}"/>'
    runs += '<w:t xml:space="preserve"> 0.05</w:t></w:r>'
    assert convert("s.docx", _one_paragraph_docx(runs)).text == f"p {written} 0.05"


def test_a_docx_row_inside_a_cell_is_written_once() -> None:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def cell(inner: str) -> str:
        return f"<w:tc>{inner}</w:tc>"

    def para(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    nested = (
        "<w:tbl><w:tr>" + cell(para("GENEA")) + cell(para("c.1A&gt;G")) + "</w:tr>"
        "<w:tr>" + cell(para("GENEB")) + cell(para("c.2T&gt;C")) + "</w:tr></w:tbl>"
    )
    doc = (
        f'<w:document xmlns:w="{w}"><w:body><w:tbl>'
        "<w:tr>" + cell(para("Family 1")) + cell(para("Carriers:") + nested) + "</w:tr>"
        "<w:sdt><w:sdtContent><w:tr>" + cell(para("Family 2")) + cell(para("none"))
        + "</w:tr></w:sdtContent></w:sdt>"
        "</w:tbl></w:body></w:document>"
    )  # fmt: skip
    c = convert("s.docx", _zip([("[Content_Types].xml", _CT), ("word/document.xml", doc.encode())]))
    assert c.text.splitlines() == [
        "---row",
        "Family 1 | Carriers:",
        "---row",
        "GENEA | c.1A>G",
        "---row",
        "GENEB | c.2T>C",
        "---row",
        "Family 2 | none",
    ]


@pytest.mark.parametrize("junk", [b"\r\n", b"\x00" * 1020])
def test_a_pdf_marker_within_the_first_1024_bytes_is_a_pdf(junk: bytes) -> None:
    c = convert("paper.bin", junk + _pdf(["one two three"]))
    assert (c.kind, c.words) == ("pdf", 3)


def test_a_pdf_marker_past_the_first_1024_bytes_or_inside_a_zip_is_not_a_pdf() -> None:
    with pytest.raises(NotText, match="unrecognised type"):
        convert("paper.bin", b"\x00" * 1021 + _pdf(["one two three"]))
    stored = io.BytesIO()
    with zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("paper.pdf", _pdf(["one two three"]))
    assert b"%PDF" in stored.getvalue()[:1024]
    with pytest.raises(NotText, match="a zip archive"):
        convert("bundle.zip", stored.getvalue())


def test_an_html_page_with_a_stray_byte_decodes_with_a_replacement_character() -> None:
    page = b"<!DOCTYPE html><html><body><p>don\x92t stop</p></body></html>"
    assert convert("page.html", page).text == "don�t stop"


@pytest.mark.parametrize(
    "page",
    [
        b"  \n<!-- generated --> <!DOCTYPE html><html><body><p>x</p></body></html>",
        b'<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>',
        b"<?xml version='1.0'?>\n<!-- a --><!-- b -->\n<html><body><p>x</p></body></html>",
    ],
)
def test_html_is_found_past_whitespace_comments_and_an_xml_declaration(page: bytes) -> None:
    c = convert("record", page)
    assert (c.kind, c.text) == ("html", "x")


def test_jats_is_still_found_after_an_xml_declaration_and_a_comment() -> None:
    xml = b"<?xml version='1.0'?><!-- served --><article><body><p>Text.</p></body></article>"
    assert convert("record", xml).kind == "jats"


def test_an_office_file_that_yields_no_text_is_not_text() -> None:
    strict = dict(_xlsx_parts())
    for part, data in strict.items():
        strict[part] = data.replace(
            _SS.encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main"
        ).replace(_REL.encode(), b"http://purl.oclc.org/ooxml/officeDocument/relationships")
    with pytest.raises(NotText, match="^an xlsx in Strict Open XML"):
        convert("s.xlsx", _zip(list(strict.items())))
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    empty = f'<w:document xmlns:w="{w}"><w:body><w:p/></w:body></w:document>'.encode()
    with pytest.raises(NotText, match="^a docx that yields no text$"):
        convert("s.docx", _zip([("[Content_Types].xml", _CT), ("word/document.xml", empty)]))


@pytest.mark.parametrize(
    "xml",
    [
        b"<article><body/></article>",
        b"<article><body><!-- the publisher does not allow its full text here --></body></article>",
    ],
)
def test_a_jats_body_with_no_element_is_no_body_text(xml: bytes) -> None:
    c = convert("p.xml", xml)
    assert c.has_body is False
    assert full_text_verdict(c) == NO_BODY


def test_a_record_nested_past_the_recursion_limit_is_not_text() -> None:
    """3,000 nested sections pass Python's default recursion limit of 1,000 in the JATS walk;
    the RecursionError is a NotText for that file, not a crash of the run."""
    xml = (
        b"<article><body>" + b"<sec>" * 3000 + b"<p>x</p>" + b"</sec>" * 3000 + b"</body></article>"
    )
    with pytest.raises(NotText, match="^p.xml: nested too deeply to convert$"):
        convert("p.xml", xml)


# --- Stage B residual fix -------------------------------------------------------------------


@pytest.mark.parametrize(
    "charset", ["hex", "base64", "rot13", "zlib", "uu", "quopri", "bz2", "idna"]
)
def test_a_page_declaring_a_codec_that_is_not_a_text_encoding_is_not_text(charset: str) -> None:
    """N1: these codecs raised LookupError (and idna UnicodeError) out of `convert`."""
    page = f'<!DOCTYPE html><html><head><meta charset="{charset}"></head><body><p>a b</p>'
    with pytest.raises(NotText, match=f"HTML that declares the charset '{charset}', which "):
        convert("page.html", page.encode())


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
@pytest.mark.parametrize("name", ["Table S1.html", "record"])
def test_a_utf16_or_utf32_page_with_a_byte_order_mark_is_read_by_it(
    encoding: str, name: str
) -> None:
    """N3: decoded as UTF-8, the page came out with a NUL between characters."""
    page = "\ufeff<!DOCTYPE html><html><body><p>bom page</p></body></html>".encode(encoding)
    c = convert(name, page)
    assert (c.kind, c.text) == ("html", "bom page")
    # Python's "utf-16" and "utf-32" codecs write a mark of their own, so this page has two
    twice = "\ufeff<!DOCTYPE html><p>bom page</p>".encode(encoding[:6])
    assert convert(name, twice).text == "bom page"


def _package(members: list[tuple[str, bytes]]) -> bytes:
    return _zip(members)


@pytest.mark.parametrize(
    ("members", "kind"),
    [
        (
            [
                ("[Content_Types].xml", _CT),
                ("ppt/presentation.xml", b"<p/>"),
                ("ppt/s.xml", b"<s/>"),
            ],
            "pptx",
        ),
        (
            [
                ("mimetype", b"application/vnd.oasis.opendocument.spreadsheet"),
                ("content.xml", b"<office:document-content/>"),
                ("meta.xml", b"<m/>"),
            ],
            "ods",
        ),
        (
            [("[Content_Types].xml", _CT), ("xl/workbook.bin", b"\x00"), ("xl/s.bin", b"\x00")],
            "xlsb",
        ),
    ],
)
def test_an_office_or_opendocument_package_is_one_file_not_an_archive(
    members: list[tuple[str, bytes]], kind: str
) -> None:
    """R-P2-52: opened as an archive, a pptx gave one loss per part."""
    data = _package(members)
    assert cv.is_archive(data) is False
    with pytest.raises(NotText, match=f"^Figure S1: a package of type {kind}, not read$"):
        convert("Figure S1", data)
    assert cv.is_archive(_zip([("a.txt", b"x"), ("b.csv", b"y")])) is True
    assert cv.is_archive(b"PK\x03\x04damaged") is False


def test_a_negative_shared_string_index_is_refused() -> None:
    parts = dict(_xlsx_parts())
    parts["xl/worksheets/sheet1.xml"] = (
        f'<worksheet xmlns="{_SS}"><sheetData><row r="1"><c r="C3" t="s"><v>-1</v></c></row>'
        "</sheetData></worksheet>"
    ).encode()
    with pytest.raises(NotText, match="C3: shared string index '-1' is not in the 3 strings"):
        convert("s.xlsx", _zip(list(parts.items())))


def test_an_xlsx_with_no_sheet_read_says_so_and_names_strict_open_xml() -> None:
    strict = dict(_xlsx_parts())
    for part, data in strict.items():
        strict[part] = data.replace(
            _SS.encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main"
        ).replace(_REL.encode(), b"http://purl.oclc.org/ooxml/officeDocument/relationships")
    with pytest.raises(NotText, match="^an xlsx in Strict Open XML, a form this converter"):
        convert("s.xlsx", _zip(list(strict.items())))
    no_sheets = dict(_xlsx_parts())
    no_sheets["xl/workbook.xml"] = f'<workbook xmlns="{_SS}"><sheets/></workbook>'.encode()
    with pytest.raises(NotText, match="^an xlsx in which no sheet was read$"):
        convert("s.xlsx", _zip(list(no_sheets.items())))
    empty = dict(_xlsx_parts())
    for sheet in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
        empty[sheet] = f'<worksheet xmlns="{_SS}"><sheetData/></worksheet>'.encode()
    with pytest.raises(NotText, match="^an xlsx that yields no text$"):
        convert("s.xlsx", _zip(list(empty.items())))
