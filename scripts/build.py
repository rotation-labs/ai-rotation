"""Inject build/data.json into template.html and write a complete site/index.html."""
import json
from pathlib import Path

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
print(f"wrote {out} ({len(page) / 1024:.0f} KB, prices through {asof})")
