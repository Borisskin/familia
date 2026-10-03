## PDF documents

For PDF creation use `python -m familia.pdf_document manifest.json output.pdf`.
The UTF-8 JSON manifest contains `title`, `sections` (each with `heading`,
`paragraphs`, and optional `images`), and `required_images` (the number of
distinct image files the user requested). Each image has `path`, `caption`,
and optional `source` (attribution and a file-page URL). Paths are local,
relative to the manifest. Download reference images first; a web page,
Markdown image tag, category URL, or filename is not an embedded image.
Never replace a requested image with a link silently. Include real local
images in the manifest and require them through `required_images`.

The builder uses an embedded Unicode font, validates all images, and reopens
the generated PDF to count actual embedded images before publishing it.
Keep the user's language; never transliterate Russian to work around fonts.
Keep reference-photo attribution next to the actual photo. A representative
species photograph does not confirm the user's cultivar or identification.

Render and visually inspect the resulting pages with PyMuPDF before sending.
If execution, rendering, fonts, or image loading fails, report the failure;
an existing file or a successful write alone does not prove the PDF is ready.
Do not claim that a PDF contains photos without checking its image resources.
