"""Inject build/data.json into template.html and write a complete site/index.html."""
import json
import shutil
import struct
from pathlib import Path


def favicon():
    """A 16x16 .ico matching the inline SVG icon.

    The page declares an SVG data: URI icon, but browsers probe /favicon.ico anyway and
    the 404 shows up in every visitor's console.
    """
    bg, green, red = (0x10, 0x0E, 0x0D), (0x8A, 0xC2, 0x6C), (0x6B, 0x74, 0xE2)  # BGR
    px = []
    for y in range(15, -1, -1):                      # .ico rows run bottom-up
        for x in range(16):
            c = bg
            if (x - 11) ** 2 + (y - 5) ** 2 <= 7:
                c = green
            elif (x - 5) ** 2 + (y - 11) ** 2 <= 7:
                c = red
            px.append(bytes((c[0], c[1], c[2], 0xFF)))
    bmp = struct.pack("<IiiHHIIiiII", 40, 16, 32, 1, 32, 0, len(px) * 4, 0, 0, 0, 0)
    bmp += b"".join(px) + b"\x00" * 64           # AND mask, unused at 32bpp
    head = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 16, 16, 0, 0, 1, 32, len(bmp), 22)
    return head + bmp

ROOT = Path(__file__).resolve().parent.parent
tpl = (ROOT / "template.html").read_text()
data = (ROOT / "build" / "data.json").read_text()
asof = json.loads(data)["asof"]

head, body = tpl.split('<div class="app">', 1)
body = '<div class="app">' + body.replace("__DATA__", data.replace("</", "<\\/"))

desc = "Relative rotation graph of the AI trade: stack layers, semiconductor and AI-ecosystem subsectors, and their stocks."
page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="{desc}">
<meta property="og:title" content="AI Rotation Graph">
<meta property="og:description" content="{desc} Prices through {asof}.">
<meta name="theme-color" content="#0d0e10">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230d0e10'/%3E%3Ccircle cx='21' cy='11' r='4' fill='%236cc28a'/%3E%3Ccircle cx='11' cy='21' r='4' fill='%23e2746b'/%3E%3C/svg%3E">
{head.strip()}
</head>
<body>
{body.strip()}
</body>
</html>
"""
out = ROOT / "site" / "index.html"
out.parent.mkdir(exist_ok=True)
out.write_text(page)
(ROOT / "site" / "favicon.ico").write_bytes(favicon())
fonts = ROOT / "site" / "fonts"
fonts.mkdir(exist_ok=True)
for f in sorted((ROOT / "assets" / "fonts").glob("*.woff2")):
    shutil.copy2(f, fonts / f.name)
print(f"wrote {out} ({len(page) / 1024:.0f} KB, prices through {asof}) + favicon + {len(list(fonts.glob('*.woff2')))} fonts")
