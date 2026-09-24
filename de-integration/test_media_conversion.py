import json
from pathlib import Path

from PIL import Image

from tmads_control_plane.media import LocalMediaProcessor, sniff_media

root = Path("/var/lib/tmads/conversion-test")
root.mkdir(exist_ok=True)
processor = LocalMediaProcessor(root, timeout_seconds=30)

svg = root / "test.svg"
svg.write_text(
    '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800">'
    '<rect width="600" height="800" fill="#0e7490"/>'
    '<circle cx="300" cy="400" r="200" fill="#facc15"/></svg>',
    encoding="utf-8",
)
pdf = root / "test.pdf"
Image.new("RGB", (600, 800), "#7c2d12").save(pdf, "PDF")

results = {}
for kind, source, header in (
    ("svg", svg, svg.read_bytes()[:64]),
    ("pdf", pdf, pdf.read_bytes()[:64]),
):
    classification = sniff_media(source.name, header)
    processed = processor.process(source, "image", f"test-{kind}")
    derivative = root / processed.derivative_name
    with Image.open(derivative) as image:
        image.verify()
    results[kind] = {
        "classification": classification,
        "derivative": processed.derivative_name,
        "hash": processed.derivative_hash,
        "bytes": derivative.stat().st_size,
        "source_retained": source.exists(),
    }

print(json.dumps(results, indent=2))
