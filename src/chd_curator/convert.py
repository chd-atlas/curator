"""Convert fetched papers and supplements to text, and judge whether a text is a full paper.

Tables use one convention everywhere: a ``---row`` line, then the row's cells joined by
`` | ``; a table footnote is a line prefixed ``[TABLE FOOTNOTE] ``. In JATS, a table-footnote
mark in a cell is written `` [a]`` and its footnote ``[TABLE FOOTNOTE] [a] ...``, so a row
keeps its footnote (de novo, say). ``quotes.normalise_text``
reads that convention (it turns ` | ` into a space and drops ``---row``), so a quote that spans
table cells is found. Nothing is filtered by gene or by sheet: a reader gets the whole file.
"""

from __future__ import annotations

import codecs
import io
import lzma
import re
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Final, Literal
from xml.etree import ElementTree as ET

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError

Kind = Literal["jats", "pdf", "html", "xlsx", "docx", "text"]

MAX_MEMBER_BYTES: Final = 50 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 200 * 1024 * 1024
ROW_MARK: Final = "---row"
FOOTNOTE_PREFIX: Final = "[TABLE FOOTNOTE] "
HTML_MIN_WORDS: Final = 2000
PDF_MIN_WORDS: Final = 1000
_METHODS_HEADING: Final = re.compile(r"method|patients|subjects|materials", re.IGNORECASE)
_RESULTS_HEADING: Final = re.compile(
    r"result|case report|case presentation|clinical report", re.IGNORECASE
)


class NotText(Exception):
    """The bytes are not a format this module converts; the message names the type."""


@dataclass(frozen=True)
class Converted:
    """``words`` is ``len(text.split())``. ``has_body`` is False only for a JATS record with no
    ``<body>``, or whose ``<body>`` holds no element: ``<body/>``, a body holding only a comment
    (ElementTree drops comments) or only bare text."""

    text: str
    kind: Kind
    words: int
    has_body: bool = True


def _converted(text: str, kind: Kind, has_body: bool = True) -> Converted:
    return Converted(text=text, kind=kind, words=len(text.split()), has_body=has_body)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _row(cells: Sequence[str]) -> str:
    return f"{ROW_MARK}\n" + " | ".join(_squash(c) for c in cells)


# --- zip handling ---------------------------------------------------------------------------


# What zipfile raises on a damaged or unusual archive, measured: a bad CRC or header
# (BadZipFile), an encrypted member (RuntimeError), an unsupported compression method or zip
# version (NotImplementedError), a damaged deflate, bzip2 or LZMA stream (zlib.error, OSError,
# LZMAError), a directory offset that points before the archive or a name that does not
# decode (ValueError); and, read in zipfile's source, a member whose bytes end early
# (EOFError).
_ZIP_ERRORS: Final = (
    zipfile.BadZipFile,
    RuntimeError,
    NotImplementedError,
    zlib.error,
    OSError,
    ValueError,
    EOFError,
    lzma.LZMAError,
)


class _TooBig(Exception):
    pass


def _read_capped(zf: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """The member's bytes; ``_TooBig`` when they exceed ``limit``.

    The size is checked on the bytes actually read, not only on the header, which an archive
    can understate.
    """
    if info.file_size > limit:
        raise _TooBig
    with zf.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise _TooBig
    return data


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """An Office part's bytes, refusing one over ``MAX_MEMBER_BYTES`` uncompressed or unreadable."""
    try:
        return _read_capped(zf, info, MAX_MEMBER_BYTES)
    except _TooBig:
        raise NotText(
            f"zip member {info.filename!r} is over {MAX_MEMBER_BYTES} bytes uncompressed"
        ) from None
    except _ZIP_ERRORS as e:
        raise NotText(f"zip member {info.filename!r} cannot be read: {type(e).__name__}") from e


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except _ZIP_ERRORS as e:
        raise NotText(f"not a readable zip archive: {type(e).__name__}: {e}") from e


@dataclass(frozen=True)
class Unpacked:
    """A zip's members: ``files`` read, as ``(name, bytes)``, and ``skipped`` as
    ``(name, reason)``, each in archive order."""

    files: tuple[tuple[str, bytes], ...]
    skipped: tuple[tuple[str, str], ...]


def unpack(zip_bytes: bytes) -> Unpacked:
    """The members of a zip, named, read or skipped with the reason.

    A directory entry, and a member whose last path part is empty, ``.`` or ``..``, is left out.
    A member with a unique base name is named by it (``../evil.txt`` becomes ``evil.txt``). Members
    that share a base name are each named by their relative path with ``/`` replaced by ``__``
    (``a/T.xlsx`` becomes ``a__T.xlsx``), after empty, ``.`` and ``..`` parts are dropped; a name
    still taken after that is prefixed with the member's position among those named, ``<n>__``.

    A member is skipped, and listed with its reason, when it is over ``MAX_MEMBER_BYTES``
    uncompressed, when reading it would take the members read past ``MAX_TOTAL_BYTES``
    uncompressed in total, or when it cannot be read (a bad CRC or offset, an encrypted member,
    an unsupported compression method, a damaged stream). An archive that does not open raises
    ``NotText``.
    """
    with _open_zip(zip_bytes) as zf:
        entries: list[tuple[zipfile.ZipInfo, list[str]]] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.replace("\\", "/").split("/")
            if parts[-1] in ("", ".", ".."):
                continue
            entries.append((info, [q for q in parts if q not in ("", ".", "..")]))
        bases = [parts[-1] for _, parts in entries]
        files: list[tuple[str, bytes]] = []
        skipped: list[tuple[str, str]] = []
        seen: set[str] = set()
        total = 0
        for position, (info, parts) in enumerate(entries, 1):
            name = "__".join(parts) if bases.count(parts[-1]) > 1 else parts[-1]
            if name in seen:
                name = f"{position}__{name}"
            seen.add(name)
            limit = min(MAX_MEMBER_BYTES, MAX_TOTAL_BYTES - total)
            try:
                data = _read_capped(zf, info, limit)
            except _TooBig:
                over_member = info.file_size > MAX_MEMBER_BYTES or limit == MAX_MEMBER_BYTES
                skipped.append(
                    (
                        name,
                        f"over {MAX_MEMBER_BYTES} bytes uncompressed"
                        if over_member
                        else f"past the archive's total of {MAX_TOTAL_BYTES} bytes uncompressed",
                    )
                )
                continue
            except _ZIP_ERRORS as e:
                skipped.append((name, f"cannot be read: {type(e).__name__}: {e}"))
                continue
            total += len(data)
            files.append((name, data))
    return Unpacked(tuple(files), tuple(skipped))


def is_archive(data: bytes) -> bool:
    """True for a zip that opens and is not an Office Open XML or OpenDocument package (one
    holding ``[Content_Types].xml`` or a ``mimetype`` member: an xlsx, a docx, a pptx, an odt
    and the like, each one file); False for anything else, a zip that does not open included."""
    if not data.startswith(b"PK"):
        return False
    try:
        with _open_zip(data) as zf:
            return _package_kind(zf) is None
    except NotText:
        return False


def _office_kind(members: set[str]) -> Literal["xlsx", "docx"] | None:
    if "[Content_Types].xml" in members and "xl/workbook.xml" in members:
        return "xlsx"
    if "[Content_Types].xml" in members and "word/document.xml" in members:
        return "docx"
    return None


_OOXML_FOLDERS: Final = {"ppt/": "pptx", "xl/": "Excel", "word/": "Word", "visio/": "vsdx"}
_MIMETYPE_BYTES: Final = 200
_ODF_PREFIX: Final = "application/vnd.oasis.opendocument."
_ODF_TYPES: Final = {
    "text": "odt",
    "spreadsheet": "ods",
    "presentation": "odp",
    "graphics": "odg",
    "formula": "odf",
}


def _package_kind(zf: zipfile.ZipFile) -> str | None:
    """What package a zip is: ``xlsx`` or ``docx``; for another Office Open XML package (with
    ``[Content_Types].xml``) ``xlsb`` for a binary workbook, else a name from its top folder
    (``pptx``, ``Excel``, ``Word``, ``vsdx``), else ``Office Open XML``; for a package with a
    ``mimetype`` member, ``odt``, ``ods``... from an OpenDocument mimetype, the quoted mimetype
    for any other (an EPUB's), ``OpenDocument`` when the member cannot be read; None for a zip
    that is neither."""
    members = set(zf.namelist())
    kind = _office_kind(members)
    if kind is not None:
        return kind
    if "[Content_Types].xml" in members:
        if "xl/workbook.bin" in members:
            return "xlsb"
        for folder, label in _OOXML_FOLDERS.items():
            if any(m.startswith(folder) for m in members):
                return label
        return "Office Open XML"
    if "mimetype" in members:
        try:
            raw = _read_capped(zf, zf.getinfo("mimetype"), _MIMETYPE_BYTES)
        except (_TooBig, *_ZIP_ERRORS):
            return "OpenDocument"
        mimetype = raw.decode("ascii", errors="replace").strip()
        if mimetype.startswith(_ODF_PREFIX):
            return _ODF_TYPES.get(mimetype.removeprefix(_ODF_PREFIX), "OpenDocument")
        return repr(mimetype)
    return None


# --- JATS -----------------------------------------------------------------------------------


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _flat(el: ET.Element) -> str:
    return _squash("".join(el.itertext()))


def _is_table_fn_ref(el: ET.Element) -> bool:
    return _local(el.tag) == "xref" and el.get("ref-type") == "table-fn"


def _fn_labels(wrap: ET.Element) -> dict[str, str]:
    """Each table footnote's mark by its ``id``: the footnote's own ``<label>``, else the
    text of the first table-footnote reference that points at it."""
    labels: dict[str, str] = {}
    for el in wrap.iter():
        if _local(el.tag) == "fn" and el.get("id"):
            label = next((c for c in el if _local(c.tag) == "label"), None)
            if label is not None and _flat(label):
                labels[el.get("id", "")] = _flat(label)
    for el in wrap.iter():
        if _is_table_fn_ref(el) and _flat(el):
            labels.setdefault(el.get("rid", ""), _flat(el))
    return labels


def _cell_text(cell: ET.Element, labels: dict[str, str]) -> str:
    """A cell's text, each table-footnote reference written `` [mark]``."""
    parts: list[str] = []

    def walk(el: ET.Element) -> None:
        if _is_table_fn_ref(el):
            mark = _flat(el) or labels.get(el.get("rid", ""), el.get("rid", ""))
            parts.append(f" [{mark}] ")
        else:
            parts.append(el.text or "")
            for child in el:
                walk(child)
                parts.append(child.tail or "")

    walk(cell)
    return _squash("".join(parts))


def _jats_table(table: ET.Element, lines: list[str], labels: dict[str, str]) -> None:
    for tr in table.iter():
        if _local(tr.tag) == "tr":
            cells = [_cell_text(c, labels) for c in tr if _local(c.tag) in ("td", "th")]
            lines.append(_row(cells))


def _jats_footnotes(el: ET.Element, lines: list[str], labels: dict[str, str]) -> None:
    """One ``[TABLE FOOTNOTE]`` line per footnote, ``[mark] `` after the prefix when it has
    one, and one per paragraph standing outside a footnote."""
    for child in el:
        tag = _local(child.tag)
        if tag == "fn":
            mark = labels.get(child.get("id", ""))
            if mark is None:
                own = next((c for c in child if _local(c.tag) == "label"), None)
                mark = _flat(own) if own is not None else None
            parts = [child.text or ""]
            for part in child:
                if _local(part.tag) != "label":
                    parts.append(f" {_flat(part)} ")
                parts.append(part.tail or "")
            text = _squash("".join(parts))
            if text:
                lines.append(FOOTNOTE_PREFIX + (f"[{mark}] " if mark else "") + text)
        elif tag == "p":
            text = _flat(child)
            if text:
                lines.append(FOOTNOTE_PREFIX + text)
        else:
            _jats_footnotes(child, lines, labels)


def _jats_block(el: ET.Element, lines: list[str]) -> None:
    tag = _local(el.tag)
    if tag == "ref-list":
        return
    if tag == "title":
        lines.append(f"## {_flat(el)}")
    elif tag == "table-wrap":
        labels = _fn_labels(el)
        for child in el:
            ctag = _local(child.tag)
            if ctag in ("label", "caption"):
                lines.append(_flat(child))
            elif ctag == "table":
                _jats_table(child, lines, labels)
            elif ctag == "table-wrap-foot":
                _jats_footnotes(child, lines, labels)
            elif ctag == "alternatives":
                for alt in child.iter():
                    if _local(alt.tag) == "table":
                        _jats_table(alt, lines, labels)
    elif tag in ("fig", "supplementary-material"):
        text = _flat(el)
        if text:
            lines.append(text)
    elif tag == "table":
        _jats_table(el, lines, _fn_labels(el))
    elif tag == "p":
        if any(_local(d.tag) == "table-wrap" for d in el.iter()):
            if el.text and el.text.strip():
                lines.append(_squash(el.text))
            for child in el:
                _jats_block(child, lines)
                if child.tail and child.tail.strip():
                    lines.append(_squash(child.tail))
        else:
            text = _flat(el)
            if text:
                lines.append(text)
    elif len(el) == 0:
        text = _flat(el)
        if text:
            lines.append(text)
    else:
        for child in el:
            _jats_block(child, lines)


def _jats_article(article: ET.Element, lines: list[str]) -> bool:
    """Append the article's text to ``lines``; True when it has a ``<body>`` holding an
    element."""
    has_body = False
    for part in article:
        ptag = _local(part.tag)
        if ptag in ("front", "front-stub"):
            for el in part.iter():
                etag = _local(el.tag)
                if etag == "article-title":
                    lines.append(_flat(el))
                elif etag == "abstract":
                    if not any(_local(c.tag) == "title" for c in el):
                        lines.append("## Abstract")
                    _jats_block(el, lines)
        elif ptag in ("body", "back", "floats-group"):
            has_body = has_body or (ptag == "body" and len(part) > 0)
            for child in part:
                _jats_block(child, lines)
        elif ptag == "sub-article":
            _jats_article(part, lines)
    return has_body


def _convert_jats(data: bytes) -> Converted:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise NotText(f"XML that does not parse: {e}") from e
    article = root if _local(root.tag) == "article" else None
    if article is None:
        article = next((e for e in root.iter() if _local(e.tag) == "article"), None)
    if article is None:
        raise NotText(f"XML whose root is <{_local(root.tag)}>, not a JATS article")
    lines: list[str] = []
    has_body = _jats_article(article, lines)
    return _converted("\n".join(lines), "jats", has_body=has_body)


# --- HTML -----------------------------------------------------------------------------------

_SKIP_TAGS: Final = frozenset({"script", "style", "noscript", "svg", "head", "template"})
_BLOCK_TAGS: Final = frozenset(
    {"p", "div", "li", "br", "section", "article", "ul", "ol", "table", "blockquote", "pre",
     "figure", "figcaption", "header", "footer", "main", "nav", "aside", "form", "dd", "dt"}
)  # fmt: skip
_HEADINGS: Final = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._cells: list[str] = []
        self._cell: list[str] | None = None
        self._skip = 0
        self._heading = False
        self._foot = 0

    def _flush(self) -> None:
        text = _squash("".join(self._buf))
        self._buf = []
        if not text:
            return
        if self._heading:
            self.lines.append(f"## {text}")
        else:
            self.lines.append(text)

    def _end_cell(self) -> None:
        if self._cell is not None:
            self._cells.append("".join(self._cell))
        self._cell = None

    def _end_row(self) -> None:
        self._end_cell()
        if self._foot:
            text = _squash(" ".join(self._cells))
            if text:
                self.lines.append(FOOTNOTE_PREFIX + text)
        elif self._cells:
            self.lines.append(_row(self._cells))
        self._cells = []

    def finish(self) -> None:
        """Close a cell, a row and a line left open by omitted end tags."""
        self._end_row()
        self._flush()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self._skip = 0  # a <head> whose end tag was omitted
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _HEADINGS:
            self._flush()
            self._heading = True
        elif tag == "tfoot":
            self._end_row()
            self._foot += 1
        elif tag in ("td", "th"):
            self._end_cell()
            self._cell = []
        elif tag == "tr":
            self._flush()
            self._end_row()
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in _HEADINGS:
            self._flush()
            self._heading = False
        elif tag == "tfoot":
            self._end_row()
            self._foot = max(0, self._foot - 1)
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag in ("tr", "table"):
            self._end_row()
            if tag == "table":
                self._flush()
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._buf.append(data)


_META_CHARSET: Final = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:\-]+)""", re.IGNORECASE
)
_XML_ENCODING: Final = re.compile(rb"""^<\?xml[^>]*encoding\s*=\s*["']([\w.:\-]+)""")


_BOMS: Final = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _bom_codec(data: bytes) -> str | None:
    """``utf-32`` or ``utf-16`` when ``data`` opens with that byte-order mark, else None."""
    return next((codec for bom, codec in _BOMS if data.startswith(bom)), None)


def _decode_html(data: bytes) -> str:
    """Decode by a UTF-16 or UTF-32 byte-order mark, else by the charset the page declares
    (meta tag or XML declaration), else as UTF-8; a byte that does not decode becomes U+FFFD,
    and a byte-order mark left at the start of the text is dropped.
    A declared charset that is unknown, not a text encoding (hex, base64...) or that cannot
    replace a bad byte (idna) raises ``NotText`` naming it."""
    label = _bom_codec(data)
    if label is None:
        head = data[:4096]
        found = _XML_ENCODING.match(head.lstrip()) or _META_CHARSET.search(head)
        label = found.group(1).decode("ascii") if found else "utf-8"
    try:
        info = codecs.lookup(label)
        if not info._is_text_encoding:
            raise NotText(f"HTML that declares the charset {label!r}, which is not a text encoding")
        return data.decode(info.name, errors="replace").removeprefix("\ufeff")
    except LookupError as e:
        raise NotText(f"HTML that declares the charset {label!r}, which is unknown") from e
    except UnicodeError as e:
        raise NotText(f"HTML that declares the charset {label!r}, which cannot read it: {e}") from e


def _convert_html(data: bytes) -> Converted:
    parser = _HtmlText()
    parser.feed(_decode_html(data))
    parser.close()
    parser.finish()
    return _converted("\n".join(parser.lines), "html")


# --- PDF ------------------------------------------------------------------------------------


def _convert_pdf(data: bytes) -> Converted:
    """The text of every page, pages separated by a blank line.

    An encrypted PDF opens when its user password is empty (pypdf tries the empty password;
    AES needs the ``cryptography`` package, which ``pypdf[crypto]`` brings); one that needs a
    user password raises ``NotText`` saying so.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except FileNotDecryptedError as e:
        raise NotText("a PDF that needs a password to open") from e
    except Exception as e:  # pypdf raises several unrelated types on damaged files
        raise NotText(f"a PDF that pypdf cannot read: {type(e).__name__}: {e}") from e
    return _converted("\n\n".join(pages), "pdf")


# --- OOXML ----------------------------------------------------------------------------------

_W: Final = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"


_MAX_COLUMN: Final = 16384  # XFD, the last column Excel allows


class _Office:
    """An Office zip whose parts together may not exceed ``MAX_TOTAL_BYTES`` when read."""

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self.zf = zf
        self.total = 0

    def has(self, name: str) -> bool:
        return name in self.zf.namelist()

    def xml(self, name: str) -> ET.Element:
        try:
            info = self.zf.getinfo(name)
        except KeyError as e:
            raise NotText(f"an Office file with no {name}") from e
        data = _read_member(self.zf, info)
        self.total += len(data)
        if self.total > MAX_TOTAL_BYTES:
            raise NotText(f"an Office file whose parts exceed {MAX_TOTAL_BYTES} bytes in total")
        try:
            return ET.fromstring(data)
        except ET.ParseError as e:
            raise NotText(f"{name} does not parse: {e}") from e


# The Symbol font's characters, by the code Word writes in ``w:sym w:char`` (less the 0xF000
# offset Word adds for a symbol font): the Greek letters and the common mathematical signs.
_SYMBOL_FONT: Final = {
    **dict(zip(range(0x41, 0x5B), "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ", strict=True)),
    **dict(zip(range(0x61, 0x7B), "αβχδεφγηιϕκλμνοπθρστυϖωξψζ", strict=True)),
    0x2B: "+", 0x2D: "−", 0x3C: "<", 0x3D: "=", 0x3E: ">", 0x7E: "∼",
    0xA1: "ϒ", 0xA2: "′", 0xA3: "≤", 0xA5: "∞", 0xAB: "↔", 0xAC: "←", 0xAD: "↑", 0xAE: "→",
    0xAF: "↓", 0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂",
    0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xD7: "⋅",
}  # fmt: skip


def _docx_symbol(el: ET.Element) -> str:
    """A ``w:sym``: its character when the font is Symbol and the code is mapped, else
    ``[symbol <font> <code as written>]``, so a sign is never silently dropped."""
    font, code = el.get(f"{_W}font") or "", el.get(f"{_W}char") or ""
    try:
        number = int(code, 16)
    except ValueError:
        number = -1
    if font.lower() == "symbol" and number >= 0:
        char = _SYMBOL_FONT.get(number - 0xF000 if number >= 0xF000 else number)
        if char is not None:
            return char
    return f"[symbol {font or '(no font)'} {code}]"


def _docx_para(p: ET.Element) -> str:
    parts: list[str] = []
    for el in p.iter():
        if el.tag == f"{_W}t":
            parts.append(el.text or "")
        elif el.tag in (f"{_W}tab", f"{_W}br"):
            parts.append(" ")
        elif el.tag == f"{_W}noBreakHyphen":
            parts.append("-")
        elif el.tag == f"{_W}sym":
            parts.append(_docx_symbol(el))
        # w:softHyphen marks where a word may break and is written as nothing
    return _squash("".join(parts))


_DOCX_WRAPPERS: Final = frozenset(
    f"{_W}{t}" for t in ("sdt", "sdtContent", "ins", "moveTo", "customXml", "smartTag")
)


def _docx_children(parent: ET.Element, tag: str) -> list[ET.Element]:
    """The ``tag`` elements under ``parent``, looking inside wrappers but not inside anything
    else (so a row's cells, not the cells of a table nested in one of them)."""
    found: list[ET.Element] = []
    for el in parent:
        if el.tag == tag:
            found.append(el)
        elif el.tag in _DOCX_WRAPPERS:
            found += _docx_children(el, tag)
    return found


def _docx_table(table: ET.Element, lines: list[str]) -> None:
    """One row line per row; a table inside a cell follows its row, each of its rows once."""
    for tr in _docx_children(table, f"{_W}tr"):
        cells: list[str] = []
        nested: list[ET.Element] = []
        for tc in _docx_children(tr, f"{_W}tc"):
            own: list[str] = []
            _docx_blocks(tc, own, nested)
            cells.append(" ".join(own))
        lines.append(_row(cells))
        for inner in nested:
            _docx_table(inner, lines)


def _docx_blocks(
    parent: ET.Element, lines: list[str], tables: list[ET.Element] | None = None
) -> None:
    """Paragraph lines of ``parent`` in order; a table is written as rows where it stands, or,
    given ``tables``, set aside there for the caller to write."""
    for el in parent:
        if el.tag == f"{_W}p":
            text = _docx_para(el)
            if text:
                lines.append(text)
        elif el.tag == f"{_W}tbl":
            if tables is None:
                _docx_table(el, lines)
            else:
                tables.append(el)
        elif el.tag in _DOCX_WRAPPERS:
            _docx_blocks(el, lines, tables)


def _convert_docx(office: _Office) -> Converted:
    body = office.xml("word/document.xml").find(f"{_W}body")
    if body is None:
        raise NotText("a docx with no body")
    lines: list[str] = []
    _docx_blocks(body, lines)
    if not any(_docx_para(p) for p in body.iter(f"{_W}p")):
        raise NotText("a docx that yields no text")
    return _converted("\n".join(lines), "docx")


def _column(ref: str) -> int:
    letters = re.match(r"[A-Za-z]+", ref)
    if letters is None:
        raise NotText(f"an xlsx cell with reference {ref!r}")
    n = 0
    for ch in letters.group().upper():
        n = n * 26 + ord(ch) - 64
    if n > _MAX_COLUMN:
        raise NotText(f"an xlsx cell {ref!r} beyond column XFD")
    return n - 1


def _shared_strings(office: _Office) -> list[str]:
    if not office.has("xl/sharedStrings.xml"):
        return []
    root = office.xml("xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in si.iter(f"{_S}t")) for si in root.iter(f"{_S}si")]


def _sheet_targets(office: _Office) -> list[tuple[str, str]]:
    workbook = office.xml("xl/workbook.xml")
    rels = {
        r.get("Id", ""): r.get("Target", "")
        for r in office.xml("xl/_rels/workbook.xml.rels").iter(f"{_PKG_REL}Relationship")
    }
    out: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_S}sheet"):
        name, rid = sheet.get("name", ""), sheet.get(f"{_R}id", "")
        if rid not in rels:
            raise NotText(f"xlsx sheet {name!r} points at a relationship {rid!r} that is absent")
        target = rels[rid].lstrip("/")
        out.append((name, target if target.startswith("xl/") else f"xl/{target}"))
    return out


_STRICT_SPREADSHEET: Final = "{http://purl.oclc.org/ooxml/spreadsheetml/main}"


def _convert_xlsx(office: _Office) -> Converted:
    sheets = _sheet_targets(office)
    if not sheets:
        if office.xml("xl/workbook.xml").tag.startswith(_STRICT_SPREADSHEET):
            raise NotText(
                "an xlsx in Strict Open XML, a form this converter does not read: no sheet was read"
            )
        raise NotText("an xlsx in which no sheet was read")
    strings = _shared_strings(office)
    lines: list[str] = []
    values = 0
    for name, path in sheets:
        lines.append(f"## Sheet: {name}")
        for row in office.xml(path).iter(f"{_S}row"):
            cells: list[str] = []
            for c in row.findall(f"{_S}c"):
                at = _column(c.get("r", "A")) if c.get("r") else len(cells)
                cells.extend([""] * (at - len(cells)))
                kind = c.get("t", "n")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{_S}t"))
                else:
                    v = c.find(f"{_S}v")
                    value = (v.text or "") if v is not None else ""
                    if kind == "s" and value:
                        try:
                            index = int(value)
                            if index < 0:
                                raise IndexError(index)
                            value = strings[index]
                        except (IndexError, ValueError) as e:
                            raise NotText(
                                f"xlsx sheet {name!r} cell {c.get('r', '?')}: shared string "
                                f"index {value!r} is not in the {len(strings)} strings present"
                            ) from e
                cells.append(value)
                values += bool(value.strip())
            if cells:
                lines.append(_row(cells))
    if not values:
        raise NotText("an xlsx that yields no text")
    return _converted("\n".join(lines), "xlsx")


# --- dispatch -------------------------------------------------------------------------------

_IMAGE_MAGIC: Final = (
    (b"\x89PNG", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF8", "GIF image"),
    (b"II*\x00", "TIFF image"),
    (b"MM\x00*", "TIFF image"),
)
_TEXT_EXTENSIONS: Final = frozenset({".txt", ".md", ".csv", ".tsv"})
PDF_MARKER_WINDOW: Final = 1024


def _markup_start(head: bytes) -> bytes:
    """``head`` past leading whitespace, comments and processing instructions (an XML
    declaration among them), lowered: where an HTML or JATS document's first tag is."""
    rest = head.lstrip()
    while rest.startswith((b"<!--", b"<?")):
        close = b"-->" if rest.startswith(b"<!--") else b"?>"
        end = rest.find(close)
        if end < 0:
            return b""
        rest = rest[end + len(close) :].lstrip()
    return rest[:256].lower()


def convert(name: str, data: bytes) -> Converted:
    """Text of ``data``, its type found from its bytes where they show it, else from ``name``.

    The bytes decide, in this order: ``%PDF`` at the start is a PDF; an image, an old binary
    Office file and a zip (an xlsx, a docx, another Office or OpenDocument package refused as
    one file, or an archive refused here) are known by their first bytes; a first tag, past a
    UTF-16 or UTF-32 byte-order mark, whitespace, comments and processing instructions, of
    ``<!doctype html`` or ``<html`` is HTML, and one of ``<article`` or ``<pmc-articleset``, or
    a document opening ``<?xml``, is JATS; ``%PDF`` within the first ``PDF_MARKER_WINDOW``
    bytes is a PDF. Only when the bytes show none of these does the extension decide:
    ``.html`` and ``.htm`` are HTML, ``.xml`` is JATS, and ``.txt``, ``.md``, ``.csv`` and
    ``.tsv`` are UTF-8 text.

    Raises ``NotText`` naming the type for old Word and Excel files, images, other Office and
    OpenDocument packages, other archives, an xlsx or docx that yields no text, HTML declaring a
    charset that cannot decode it, a file nested too deeply to walk, and anything unrecognised.
    """
    try:
        return _dispatch(name, data)
    except RecursionError as e:
        raise NotText(f"{name}: nested too deeply to convert") from e


def _dispatch(name: str, data: bytes) -> Converted:
    ext = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    data = data.removeprefix(b"\xef\xbb\xbf")  # a UTF-8 byte-order mark, before any sniffing
    if data.startswith(b"%PDF"):
        return _convert_pdf(data)
    for magic, label in _IMAGE_MAGIC:
        if data.startswith(magic):
            raise NotText(f"{name}: a {label}, not text")
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise NotText(f"{name}: an old binary Office file ({ext or 'no extension'}), not text")
    if data.startswith(b"PK"):
        with _open_zip(data) as zf:
            kind = _package_kind(zf)
            if kind == "xlsx":
                return _convert_xlsx(_Office(zf))
            if kind == "docx":
                return _convert_docx(_Office(zf))
        if kind is not None:
            raise NotText(f"{name}: a package of type {kind}, not read")
        raise NotText(f"{name}: a zip archive, not opened here")
    head = data[:2048]
    bom = _bom_codec(head)
    if bom is not None:  # sniff the text a UTF-16 or UTF-32 byte-order mark announces
        head = head.decode(bom, errors="ignore").lstrip("\ufeff").encode("utf-8", errors="ignore")
    first = _markup_start(head)
    if first.startswith((b"<!doctype html", b"<html")):
        return _convert_html(data)
    if head.lstrip().startswith(b"<?xml") or first.startswith((b"<article", b"<pmc-articleset")):
        return _convert_jats(data)
    if b"%PDF" in data[:PDF_MARKER_WINDOW]:
        return _convert_pdf(data)
    if ext in (".html", ".htm"):
        return _convert_html(data)
    if ext == ".xml":
        return _convert_jats(data)
    if ext in _TEXT_EXTENSIONS:
        try:
            return _converted(data.decode("utf-8"), "text")
        except UnicodeDecodeError as e:
            raise NotText(f"{name}: not UTF-8 text: {e}") from e
    raise NotText(f"{name}: an unrecognised type ({ext or 'no extension'}), not text")


# --- verdict --------------------------------------------------------------------------------


def full_text_verdict(c: Converted, headings: Sequence[str] = ()) -> str | None:
    """None when ``c`` is accepted as a paper's full text, else the reason it is not.

    HTML needs at least ``HTML_MIN_WORDS`` words and a methods-like and a results-like heading;
    ``headings`` defaults to the ``## `` lines of the converted text. A PDF needs at least
    ``PDF_MIN_WORDS`` words. A JATS record needs ``has_body``. Any other kind is a supplement
    format and is refused here rather than judged.
    """
    if c.kind == "html":
        found = list(headings) or [
            line[3:] for line in c.text.splitlines() if line.startswith("## ")
        ]
        joined = " ".join(found)
        problems = [f"{c.words} words"]
        if not _METHODS_HEADING.search(joined):
            problems.append("no methods heading")
        if not _RESULTS_HEADING.search(joined):
            problems.append("no results heading")
        if c.words < HTML_MIN_WORDS or len(problems) > 1:
            return "a landing page: " + ", ".join(problems)
        return None
    if c.kind == "pdf":
        if c.words < PDF_MIN_WORDS:
            return f"too short for a paper: {c.words} words, under {PDF_MIN_WORDS:,}"
        return None
    if c.kind == "jats":
        if c.has_body:
            return None
        return "no body text: the record has no <body>, or its <body> holds no element"
    return f"not judged as full text: a {c.kind} file is a supplement format"
