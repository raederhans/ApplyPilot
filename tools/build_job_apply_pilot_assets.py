"""Build deterministic Job Apply Pilot raster icons from the vector mark geometry."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

INK = "#0B1F3A"
PAPER = "#F7F9FC"
ROUTE = "#FF5A36"
VERIFIED = "#20B486"
ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    ROOT / "src" / "applypilot" / "frontend" / "assets" / "job-apply-pilot",
    ROOT / "docs" / "brand" / "job-apply-pilot" / "assets",
)


def _xy(values: tuple[float, ...], scale: float) -> tuple[int, ...]:
    return tuple(round(value * scale) for value in values)


def render_mark(size: int) -> Image.Image:
    """Render the compact mark at *size* using the SVG's 128-unit geometry."""
    scale = size / 128
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=round(28 * scale), fill=INK)
    draw.rounded_rectangle(_xy((26, 22, 94, 104), scale), radius=round(10 * scale), fill=PAPER)
    line_width = max(1, round(7 * scale))
    draw.line(_xy((42, 43, 78, 43), scale), fill=INK, width=line_width)
    draw.line(_xy((42, 57, 68, 57), scale), fill=INK, width=line_width)
    route = [(18, 91), (35, 76), (53, 82), (68, 74), (82, 61), (96, 49), (110, 51)]
    draw.line([_xy(point, scale) for point in route], fill=ROUTE, width=max(1, round(8 * scale)), joint="curve")
    for x, y in ((18, 91), (53, 82), (82, 61)):
        radius = 6 * scale
        draw.ellipse((x * scale - radius, y * scale - radius, x * scale + radius, y * scale + radius), fill=PAPER, outline=ROUTE, width=max(1, round(4 * scale)))
    radius = 13 * scale
    draw.ellipse((110 * scale - radius, 51 * scale - radius, 110 * scale + radius, 51 * scale + radius), fill=VERIFIED)
    draw.line([_xy((104, 51), scale), _xy((108, 55), scale), _xy((116, 46), scale)], fill="white", width=max(1, round(4 * scale)), joint="curve")
    return image


def main() -> None:
    for target in TARGETS:
        target.mkdir(parents=True, exist_ok=True)
        for size in (16, 32, 48, 192, 512):
            render_mark(size).save(target / f"job-apply-pilot-{size}.png", optimize=True)
        render_mark(256).save(
            target / "favicon.ico",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )


if __name__ == "__main__":
    main()
