# Third-party licenses

PDF Studio (public edition) uses only permissively licensed components. Each was checked against its
package metadata and upstream license file. CI re-checks the installed set on every push
(`tools/license_audit.py`), failing on GPL / AGPL / LGPL / unknown.

## Python libraries

| Component | License | Used for |
|---|---|---|
| pypdfium2 (PDFium) | BSD-3-Clause / Apache-2.0 (bundled deps: FreeType FTL, libjpeg-turbo, zlib, lcms2 MIT, OpenJPEG BSD-2, libpng, ICU) | rendering, text with positions, objects |
| pikepdf (QPDF) | MPL-2.0 (QPDF: Apache-2.0) | structure editing, content streams, encryption |
| pdfplumber / pdfminer.six | MIT | text, words, vector paths, tables |
| pypdf | BSD-3-Clause | forms helpers |
| ReportLab | BSD-3-Clause | overlays (stamps, headers, drawing sheets) |
| Pillow | MIT-CMU (HPND) | images |
| fontTools | MIT | font inspection and subsetting |
| PyYAML | MIT | recipes |
| openpyxl | MIT | XLSX output |
| NumPy | BSD-3-Clause | image diff |
| pytesseract | Apache-2.0 | OCR wrapper |
| pyHanko, pyhanko-certvalidator | MIT | signatures, PAdES |
| ezdxf | MIT | DXF read/write (SVG / Matplotlib backends only) |
| Matplotlib | Matplotlib License (PSF-style, permissive) | DXF rendering backend |
| typst (Python) | Apache-2.0 | typesetting, SVG → PDF |
| python-docx (+ lxml BSD-3, typing_extensions PSF) | MIT | PDF → Word (`docx` extra) |
| pyproj (bundles PROJ MIT, libtiff, libjpeg, libwebp, zstd, xz — permissive) | MIT | site plans: coordinate systems (`site` extra) |

The `site` extra adds only pyproj to the components above. Site-plan image classification and vectorization are
plain NumPy (`pdfcore/ops/plans/site/raster.py`); scikit-image, SciPy and Shapely are not used.

Optional, not installed by default: WeasyPrint (BSD-3-Clause), Camelot (MIT), Docling (MIT).

## External programs (called as separate processes, not linked)

| Program | License | Used for |
|---|---|---|
| Tesseract | Apache-2.0 | OCR |
| Typst CLI | Apache-2.0 | typesetting |
| veraPDF | GPL-3.0+ **or** MPL-2.0 — used under MPL-2.0 | PDF/A, PDF/UA validation |
| LibreOffice | MPL-2.0 | Office → PDF |

## Fonts (shipped unmodified with their license files in `templates/fonts/`)

| Font | License |
|---|---|
| Inter 4.1 | SIL OFL 1.1 |
| Source Serif 4.005 | SIL OFL 1.1 (Reserved Font Name "Source") |

## Not used (and blocked by the audit)

PyMuPDF / MuPDF (AGPL), Ghostscript (AGPL), Poppler-based tools (GPL), CairoSVG and svglib (LGPL),
Marker model weights. The private local edition may call some of these on its owner's machine only.
