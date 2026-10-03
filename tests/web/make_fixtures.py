#!/usr/bin/env python3
# Copyright (C) 2026 Andrei Errapart
# SPDX-License-Identifier: GPL-2.0-or-later
"""Regenerate the fixtures the web tests read.

    python3 tests/web/make_fixtures.py

Run with an interpreter that has PyMuPDF.  The fixtures are synthetic: a
two-page vector PDF, and ZIP archives around it that exercise the archive
reader in web/input.js.  They are committed, so the JavaScript tests need
neither Python nor PyMuPDF.
"""
import struct
import zipfile
import zlib
from pathlib import Path

import fitz  # PyMuPDF

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Fixed so that regenerating the fixtures leaves the archives unchanged.
ZIP_DATE = (2026, 1, 1, 0, 0, 0)


def vector_pdf() -> bytes:
    """Two pages with a few strokes and text spans each."""
    doc = fitz.open()
    for index in range(2):
        page = doc.new_page(width=842, height=595)
        page.draw_line((100, 100), (200, 100), color=(0, 0, 1), width=0.5)
        page.draw_line((200, 100), (200, 180), color=(0, 0, 1), width=0.5)
        page.draw_rect((300, 300, 380, 360), color=(0.5, 0, 0), width=0.7)
        page.insert_text((110, 90), f"NET{index}", fontsize=8)
    doc.set_metadata({"producer": "pdf2kicad test fixture"})
    return doc.tobytes(no_new_id=True, deflate=True)


def write_zip(name: str, members, compression=zipfile.ZIP_DEFLATED) -> Path:
    path = FIXTURES / name
    with zipfile.ZipFile(path, "w", compression) as archive:
        for member, data in members:
            info = zipfile.ZipInfo(member, ZIP_DATE)
            info.compress_type = compression
            archive.writestr(info, data)
    return path


def corrupt_member_payload(path: Path, member: str, target: Path) -> None:
    """Flip one payload byte of ``member``, leaving its recorded CRC alone."""
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    name_length, extra_length = struct.unpack_from(
        "<HH", data, info.header_offset + 26
    )
    start = info.header_offset + 30 + name_length + extra_length
    # A byte well inside the payload: for a deflated member this still
    # inflates, to different bytes of the same length or to a damaged stream.
    data[start + info.compress_size // 2] ^= 0x01
    target.write_bytes(bytes(data))


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    pdf = vector_pdf()
    (FIXTURES / "vector.pdf").write_bytes(pdf)

    write_zip("one-pdf.zip", [("vector.pdf", pdf)])
    stored = write_zip(
        "stored.zip", [("vector.pdf", pdf)], zipfile.ZIP_STORED
    )
    write_zip(
        "design-folder.zip",
        [
            ("board/", b""),
            ("board/README.txt", b"notes\n"),
            ("board/Schematic Rev B.PDF", pdf),
            ("__MACOSX/board/._Schematic Rev B.PDF", b"\x00\x05\x16\x07"),
            ("board/.hidden.pdf", pdf),
        ],
    )
    write_zip("utf8-name.zip", [("基板 Rev2.pdf", pdf)])
    write_zip("no-pdf.zip", [("README.txt", b"notes\n")])
    write_zip("two-pdfs.zip", [("a.pdf", pdf), ("sub/b.pdf", pdf)])
    write_zip("not-a-pdf-inside.zip", [("fake.pdf", b"plain text, no magic")])
    # 40 MiB of zeros compresses to about 40 KiB: a neighbour that must
    # never be inflated, and a member that inflates past any small limit.
    zeros = bytes(40 * 1024 * 1024)
    write_zip("large-neighbour.zip", [("big.bin", zeros), ("vector.pdf", pdf)])
    write_zip("bomb.zip", [("bomb.pdf", b"%PDF-1.7\n" + zeros)])

    deflated = FIXTURES / "one-pdf.zip"
    corrupt_member_payload(
        stored, "vector.pdf", FIXTURES / "stored-corrupt.zip"
    )
    corrupt_member_payload(
        deflated, "vector.pdf", FIXTURES / "deflated-corrupt.zip"
    )

    # The encrypted flag set in both headers; the payload is left as it is,
    # since the reader must refuse before touching it.
    locked = bytearray(deflated.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = locked.find(signature)
        struct.pack_into(
            "<H", locked, at + flag_offset,
            struct.unpack_from("<H", locked, at + flag_offset)[0] | 0x0001,
        )
    (FIXTURES / "encrypted.zip").write_bytes(bytes(locked))

    # A member whose real size exceeds the size the archive declares for it.
    lying = bytearray((FIXTURES / "bomb.zip").read_bytes())
    with zipfile.ZipFile(FIXTURES / "bomb.zip") as archive:
        info = archive.getinfo("bomb.pdf")
    central = lying.rfind(b"PK\x01\x02")
    struct.pack_into("<I", lying, central + 24, 1024)
    struct.pack_into("<I", lying, info.header_offset + 22, 1024)
    (FIXTURES / "understated-size.zip").write_bytes(bytes(lying))

    # macOS-style archive: sizes deferred to a data descriptor.
    raw = zlib.compressobj(9, zlib.DEFLATED, -15)
    payload = raw.compress(pdf) + raw.flush()
    crc = zlib.crc32(pdf)
    name = b"vector.pdf"
    local = struct.pack(
        "<IHHHHHIIIHH", 0x04034B50, 20, 0x0008, 8, 0, 0x5C21, 0, 0, 0,
        len(name), 0,
    ) + name
    descriptor = struct.pack("<IIII", 0x08074B50, crc, len(payload), len(pdf))
    central_record = struct.pack(
        "<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x0008, 8, 0, 0x5C21, crc,
        len(payload), len(pdf), len(name), 0, 0, 0, 0, 0, 0,
    ) + name
    body = local + payload + descriptor
    end = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central_record), len(body), 0
    )
    (FIXTURES / "data-descriptor.zip").write_bytes(body + central_record + end)

    for path in sorted(FIXTURES.iterdir()):
        print(f"{path.stat().st_size:>9}  {path.name}")


if __name__ == "__main__":
    main()
