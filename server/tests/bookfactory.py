"""Livros de teste: EPUB à mão (zip) e PDF à mão (sem bibliotecas), sempre minúsculos."""

import zipfile
from pathlib import Path

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

# Texto de cada capítulo: o suficiente para passar o limite de «capítulo quase vazio».
LOREM = (
    "A biblioteca guarda livros abertos e livros privados, cada um com a sua classe de licença. "
    "O capítulo diz de onde veio, quem o escreveu e onde se pode ler no leitor web. "
)


def chapter_body(title: str, topic: str, repeat: int = 6) -> str:
    paragraphs = "".join(
        f"<p>{topic} é o assunto do parágrafo {n}. {LOREM}</p>" for n in range(1, repeat + 1)
    )
    return f"<h1>{title}</h1>{paragraphs}"


def make_epub(
    path: Path,
    chapters: list[tuple[str, str]],
    *,
    title: str = "Livro de Teste",
    author: str = "Ana Autora",
    language: str = "pt",
    toc: str | None = "nav",  # 'nav' (EPUB3), 'ncx' (EPUB2) ou None
    front: bool = True,
    drm: str | None = None,  # 'adept' | 'aes' | 'fonts' (ofuscação de fontes: não é DRM)
    same_file: bool = False,  # todos os capítulos num só ficheiro, com âncoras
    toc_only: list[int] | None = None,  # só estes capítulos (a contar de 1) entram no índice
    translator: str | None = None,  # `dc:contributor opf:role="trl"`
    wikidata: str | None = None,  # `dc:identifier` `wikidata:Q…`
) -> Path:
    """`chapters`: (título, HTML do corpo). O 1.º ficheiro é a capa (`front`)."""
    items: list[tuple[str, str]] = []  # (ficheiro, html)
    if front:
        items.append(("cover.xhtml", "<h1>Livro de Teste</h1><p>Capa.</p>"))
    anchors: list[tuple[str, str, str]] = []  # (título, ficheiro, âncora)
    if same_file:
        body = ""
        for n, (name, html) in enumerate(chapters, start=1):
            body += f'<div id="c{n}">{html}</div>'
            anchors.append((name, "all.xhtml", f"c{n}"))
        items.append(("all.xhtml", body))
    else:
        for n, (name, html) in enumerate(chapters, start=1):
            items.append((f"ch{n}.xhtml", html))
            anchors.append((name, f"ch{n}.xhtml", ""))

    if toc_only is not None:
        anchors = [a for n, a in enumerate(anchors, start=1) if n in toc_only]

    def xhtml(body: str, name: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>{name}</title></head><body>{body}</body></html>"
        )

    manifest = "".join(
        f'<item id="i{n}" href="{name}" media-type="application/xhtml+xml"/>'
        for n, (name, _) in enumerate(items)
    )
    spine = "".join(f'<itemref idref="i{n}"/>' for n in range(len(items)))
    extra_manifest = ""
    spine_attr = ""
    if toc == "nav":
        extra_manifest = (
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
    elif toc == "ncx":
        extra_manifest = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        spine_attr = ' toc="ncx"'
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">'
        f'<dc:identifier id="uid">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>'
        f'<dc:title>{title}</dc:title><dc:creator opf:role="aut">{author}</dc:creator>'
        + (f'<dc:contributor opf:role="trl">{translator}</dc:contributor>' if translator else "")
        + (f"<dc:identifier>wikidata:{wikidata}</dc:identifier>" if wikidata else "")
        + f"<dc:language>{language}</dc:language><dc:publisher>Editora de Teste</dc:publisher>"
        "<dc:date>2026-01-01</dc:date></metadata>"
        f"<manifest>{manifest}{extra_manifest}</manifest><spine{spine_attr}>{spine}</spine></package>"
    )
    links = "".join(
        f'<li><a href="{file}{"#" + anchor if anchor else ""}">{name}</a></li>'
        for name, file, anchor in anchors
    )
    nav = xhtml(
        f'<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><ol>{links}</ol></nav>',
        "Índice",
    )
    ncx = (
        '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        "<navMap>"
        + "".join(
            f'<navPoint id="p{n}" playOrder="{n}"><navLabel><text>{name}</text></navLabel>'
            f'<content src="{file}{"#" + anchor if anchor else ""}"/></navPoint>'
            for n, (name, file, anchor) in enumerate(anchors, start=1)
        )
        + "</navMap></ncx>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zipped:

        class Archive:
            """Data fixa nos membros: o mesmo livro dá sempre os mesmos bytes (e o mesmo sha256)."""

            @staticmethod
            def writestr(name: str, data: str, compress_type: int = zipfile.ZIP_DEFLATED) -> None:
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = compress_type
                zipped.writestr(info, data)

        archive = Archive()
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        for name, html in items:
            archive.writestr(f"OEBPS/{name}", xhtml(html, name))
        if toc == "nav":
            archive.writestr("OEBPS/nav.xhtml", nav)
        elif toc == "ncx":
            archive.writestr("OEBPS/toc.ncx", ncx)
        if drm == "adept":
            archive.writestr(
                "META-INF/rights.xml",
                '<rights xmlns="http://ns.adobe.com/adept"><licenseToken/></rights>',
            )
        elif drm in ("aes", "fonts"):
            algorithm = (
                "http://www.w3.org/2001/04/xmlenc#aes128-cbc"
                if drm == "aes"
                else "http://www.idpf.org/2008/embedding"
            )
            archive.writestr(
                "META-INF/encryption.xml",
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
                f'<enc:EncryptionMethod Algorithm="{algorithm}"/>'
                '<enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData>'
                "</enc:EncryptedData></encryption>",
            )
    return path


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(
    path: Path,
    pages: list[list[str]],
    *,
    outline: list[tuple[str, int]] | None = None,
    title: str = "PDF de Teste",
    author: str = "Ana Autora",
    encrypted: bool = False,
) -> Path:
    """PDF de texto com Helvetica; `pages` são as linhas de cada página (só latim-1). `outline`:
    marcadores (título, página a contar de 1). `encrypted` põe um /Encrypt no trailer (o que basta
    para o detetor de DRM)."""
    objects: list[str | bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add("")  # preenchido no fim
    pages_root = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids: list[int] = []
    for lines in pages:
        ops = ["BT", "/F1 11 Tf", "72 760 Td", "14 TL"]
        for line in lines:
            ops.append(f"({_pdf_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        content = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_root} 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
            )
        )
    outline_ref = ""
    if outline:
        root = add("")
        item_ids = [add("") for _ in outline]
        for n, (name, page) in enumerate(outline):
            prev = f" /Prev {item_ids[n - 1]} 0 R" if n else ""
            nxt = f" /Next {item_ids[n + 1]} 0 R" if n + 1 < len(outline) else ""
            objects[item_ids[n] - 1] = (
                f"<< /Title ({_pdf_escape(name)}) /Parent {root} 0 R{prev}{nxt} "
                f"/Dest [{page_ids[page - 1]} 0 R /Fit] >>"
            )
        objects[root - 1] = (
            f"<< /Type /Outlines /First {item_ids[0]} 0 R /Last {item_ids[-1]} 0 R "
            f"/Count {len(outline)} >>"
        )
        outline_ref = f" /Outlines {root} 0 R"
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects[pages_root - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_root} 0 R{outline_ref} >>"
    info = add(f"<< /Title ({_pdf_escape(title)}) /Author ({_pdf_escape(author)}) >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        data = body if isinstance(body, bytes) else body.encode("latin-1")
        out += f"{number} 0 obj\n".encode() + data + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    encrypt = ""
    if encrypted:
        o = "<" + "00" * 32 + ">"
        encrypt = f" /Encrypt << /Filter /Standard /V 1 /R 2 /O {o} /U {o} /P -4 >>"
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R{encrypt} >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return path


def make_mobi_stub(path: Path, *, encrypted: bool = False) -> Path:
    """Um ficheiro com o cabeçalho de um MOBI (Palm DB) e o tipo de cifra no registo 0: o que o
    detetor de DRM lê. Não é um livro legível."""
    header = bytearray(78)
    header[60:68] = b"BOOKMOBI"
    header[76:78] = (1).to_bytes(2, "big")  # um registo
    record_list = (78 + 8).to_bytes(4, "big") + b"\x00\x00\x00\x00"  # offset do registo 0
    record0 = bytearray(32)
    record0[12:14] = (2 if encrypted else 0).to_bytes(2, "big")
    # O offset do registo 0 está nos 4 primeiros bytes da lista, que começa no byte 78.
    path.write_bytes(bytes(header) + record_list + bytes(record0))
    return path
