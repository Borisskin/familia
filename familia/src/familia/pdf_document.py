"""Build and verify illustrated, Unicode PDFs from a local JSON manifest."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape


def find_font(explicit: str | None = None) -> Path:
    candidates = (
        [Path(explicit)]
        if explicit
        else [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"),
            Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    raise ValueError(
        "A Unicode TTF font is required; install fonts-dejavu-core or pass --font"
    )


def build_pdf(manifest_path: Path, output: Path, *, font: str | None = None) -> dict:
    from PIL import Image as PILImage
    from PIL import ImageOps
    from pypdf import PdfReader
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        Image,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = manifest["required_images"]
    if type(required) is not int or required < 0:
        raise ValueError("required_images must be a nonnegative integer")
    sections = manifest["sections"]
    if not sections:
        raise ValueError("At least one document section is required")
    pdfmetrics.registerFont(TTFont("FamiliaUnicode", str(find_font(font))))
    text_style = ParagraphStyle(
        "Body",
        fontName="FamiliaUnicode",
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#25352e"),
        spaceAfter=8,
    )
    heading_style = ParagraphStyle(
        "Heading", parent=text_style, fontSize=19, leading=25, spaceAfter=16
    )
    caption_style = ParagraphStyle("Caption", parent=text_style, fontSize=8, leading=11)
    width = A4[0] - 88
    story = []
    image_paths: set[Path] = set()
    buffers = []
    for index, section in enumerate(sections):
        if index:
            story.append(PageBreak())
        story.append(Paragraph(escape(section["heading"]), heading_style))
        images = section.get("images", [])
        if len(images) > 2:
            raise ValueError(
                "Use at most two images per section; add another section for more"
            )
        if images:
            cells = []
            cell_width = width / len(images)
            for item in images:
                path = (manifest_path.parent / item["path"]).resolve()
                with PILImage.open(path) as original:
                    picture = ImageOps.exif_transpose(original).convert("RGB")
                    picture.thumbnail((1600, 1600))
                    buffer = BytesIO()
                    picture.save(buffer, format="JPEG", quality=90)
                buffers.append(buffer)
                scale = min((cell_width - 14) / picture.width, 205 / picture.height)
                image = Image(
                    buffer, width=picture.width * scale, height=picture.height * scale
                )
                caption = item["caption"]
                if item.get("source"):
                    caption += "\n" + item["source"]
                cells.append(
                    [
                        image,
                        Spacer(1, 7),
                        Paragraph(
                            escape(caption).replace("\n", "<br/>"), caption_style
                        ),
                    ]
                )
                image_paths.add(path)
            table = Table([cells], colWidths=[cell_width] * len(cells))
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ]
                )
            )
            story.extend([table, Spacer(1, 16)])
        for paragraph in section.get("paragraphs", []):
            story.append(
                Paragraph(escape(paragraph).replace("\n", "<br/>"), text_style)
            )
    if len(image_paths) < required:
        raise ValueError(
            f"Requested {required} distinct images; manifest embeds only {len(image_paths)}"
        )

    def footer(canvas, doc):
        canvas.setFont("FamiliaUnicode", 8)
        canvas.setFillColor(colors.HexColor("#69786d"))
        canvas.drawRightString(A4[0] - 44, 27, str(doc.page))

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".pdf", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        SimpleDocTemplate(
            str(temporary),
            pagesize=A4,
            rightMargin=44,
            leftMargin=44,
            topMargin=42,
            bottomMargin=44,
            title=manifest["title"],
            author="Familia",
        ).build(story, onFirstPage=footer, onLaterPages=footer)
        reader = PdfReader(temporary)
        try:
            embedded = sum(len(page.images) for page in reader.pages)
            if embedded < len(image_paths):
                raise ValueError(
                    f"PDF verification failed: {embedded} embedded images for {len(image_paths)} files"
                )
            if not any((page.extract_text() or "").strip() for page in reader.pages):
                raise ValueError("PDF verification failed: no readable text")
            pages = len(reader.pages)
        finally:
            reader.close()
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "output": str(output),
        "pages": pages,
        "embedded_images": embedded,
        "distinct_image_files": len(image_paths),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font")
    args = parser.parse_args()
    print(
        json.dumps(
            build_pdf(args.manifest, args.output, font=args.font), ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
