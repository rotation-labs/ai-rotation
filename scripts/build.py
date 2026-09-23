"""Inject build/data.json into template.html and write a complete site/index.html."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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
full = json.loads(data)
asof = full["asof"]
# Build stamp: an open page polls version.json and offers a reload when a newer build lands.
full["built"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
data = json.dumps(full, separators=(",", ":"))

# Every view at the latest date needs about 60 weeks of history (a 26-week baseline for the
# stability check, smoothing warm-up, a 12-bar tail), but replay reaches back three years.
# Embed the recent window and let the page fetch the rest from the same site on first use:
# first load shrinks by more than half and no third-party request is added. Values at the
# latest date are unchanged - the smoothing's starting point decays away long before it.
WINDOW = 320
history = None
if len(full["dates"]) > WINDOW:
    history = {"dates": full["dates"], "px": full["px"]}
    data = json.dumps(dict(full, dates=full["dates"][-WINDOW:],
                           px={k: v[-WINDOW:] for k, v in full["px"].items()},
                           full_len=len(full["dates"]), win_start=full["dates"][-WINDOW]),
                      separators=(",", ":"))

head, body = tpl.split('<div class="app">', 1)
body = '<div class="app">' + body.replace("__DATA__", data.replace("</", "<\\/"))

# Counted from the data, so the link preview cannot drift from the page again.
desc = (f"Relative rotation graph of the AI trade across {len(json.loads(data)['groups'])} tiers of its value chain, "
        "from chips to applications, down to the individual stocks.")
page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="{desc}">
<meta property="og:title" content="Rotation">
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
# A syntax error in the page script blanks the whole site. Parse it with Node before writing
# anything, so a bad edit fails the build and the previous good page stays live - the same
# guarantee the data guards give. Skipped where Node is not installed.
if shutil.which("node"):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write("\n".join(re.findall(r"<script>(.*?)</script>", page, re.S)))
    chk = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
    Path(f.name).unlink()
    if chk.returncode:
        sys.exit("page script failed to parse - not publishing:\n" + chk.stderr)

out = ROOT / "site" / "index.html"
out.parent.mkdir(exist_ok=True)
out.write_text(page)
(ROOT / "site" / "favicon.ico").write_bytes(favicon())
if history:
    (ROOT / "site" / "history.json").write_text(json.dumps(history, separators=(",", ":")))
(ROOT / "site" / "version.json").write_text(json.dumps({"built": full["built"], "live": full.get("live")}))
fonts = ROOT / "site" / "fonts"
fonts.mkdir(exist_ok=True)
for f in sorted((ROOT / "assets" / "fonts").glob("*.woff2")):
    shutil.copy2(f, fonts / f.name)
print(f"wrote {out} ({len(page) / 1024:.0f} KB, prices through {asof}) + favicon + {len(list(fonts.glob('*.woff2')))} fonts")
