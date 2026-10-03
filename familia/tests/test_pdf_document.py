import json

import pytest
from PIL import Image
from pypdf import PdfReader

from familia.pdf_document import build_pdf


def manifest(tmp_path, images, required):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "title": "Уход за растениями",
                "required_images": required,
                "sections": [
                    {
                        "heading": "Мандарин",
                        "paragraphs": ["Русский текст без транслитерации"],
                        "images": images,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_embeds_real_photo_and_unicode_font(tmp_path):
    Image.new("RGB", (80, 120), "green").save(tmp_path / "plant.jpg")
    output = tmp_path / "plan.pdf"
    result = build_pdf(
        manifest(tmp_path, [{"path": "plant.jpg", "caption": "Фото растения"}], 1),
        output,
    )
    reader = PdfReader(output)
    assert result["embedded_images"] == 1
    assert len(reader.pages[0].images) == 1
    assert "Мандарин" in reader.pages[0].extract_text()
    assert "Русский текст без транслитерации" in reader.pages[0].extract_text()
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object().values()
    descriptors = [f.get_object().get("/FontDescriptor") for f in fonts]
    assert any(d is not None and "/FontFile2" in d.get_object() for d in descriptors)


@pytest.mark.parametrize("images", [[], [{"path": "missing.jpg", "caption": "Фото"}]])
def test_missing_photos_do_not_replace_existing_pdf(tmp_path, images):
    output = tmp_path / "plan.pdf"
    output.write_bytes(b"original file")
    with pytest.raises((ValueError, FileNotFoundError)):
        build_pdf(manifest(tmp_path, images, 1), output)
    assert output.read_bytes() == b"original file"
