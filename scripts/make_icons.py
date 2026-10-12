"""Build every favicon / home-screen icon from static/icon.svg (gomez-ui v1.0).

The header logo and the favicon are the SAME artwork: static/icon.svg.
Run after changing icon.svg, and commit the outputs:
    pip install cairosvg pillow
    python scripts/make_icons.py --name "Heating history" --short "Heating" --accent "#b4400f"
"""
import argparse, io, json, pathlib
import cairosvg
from PIL import Image

STATIC = pathlib.Path(__file__).resolve().parent.parent / "web"

def render(svg: bytes, size: int) -> Image.Image:
    return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg, output_width=size, output_height=size))).convert("RGBA")

def padded(svg: bytes, size: int, scale: float, bg: str) -> Image.Image:
    """Artwork shrunk to `scale` of the canvas on a solid background (iOS / Android maskable)."""
    canvas = Image.new("RGBA", (size, size), bg)
    inner = render(svg, round(size * scale))
    off = (size - inner.width) // 2
    canvas.alpha_composite(inner, (off, off))
    return canvas.convert("RGB")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", default=str(STATIC))
    ap.add_argument("--name", required=True)
    ap.add_argument("--short", required=True)
    ap.add_argument("--accent", required=True, help="light accent, e.g. #1d6b45")
    ap.add_argument("--bg", default="#f5f5f2", help="gomez-ui --bg")
    a = ap.parse_args()
    out = pathlib.Path(a.static)
    svg = (out / "icon.svg").read_bytes()
    render(svg, 32).save(out / "favicon-32.png")
    render(svg, 48).save(out / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    render(svg, 192).save(out / "icon-192.png")
    render(svg, 512).save(out / "icon-512.png")
    padded(svg, 180, 0.86, a.accent).save(out / "apple-touch-icon.png")    # iOS: no transparency
    padded(svg, 512, 0.72, a.accent).save(out / "icon-maskable-512.png")   # Android safe zone (inner 80%)
    (out / "manifest.webmanifest").write_text(json.dumps({
        "name": a.name, "short_name": a.short, "start_url": "/", "display": "standalone",
        "background_color": a.bg, "theme_color": a.accent,
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, indent=2) + "\n")
    print("icons written to", out)

if __name__ == "__main__":
    main()
