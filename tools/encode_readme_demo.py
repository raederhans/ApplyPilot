"""Encode real browser screencasts with captions and recorded pointer movements.

Requires Pillow and ffmpeg. Input is produced by readme-recorder.mjs.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT, FOOTER, FPS = 1120, 630, 82, 12


def font(size: int, lang: str) -> ImageFont.ImageFont:
    candidates = (["C:/Windows/Fonts/msyh.ttc"] if lang == "zh" else []) + [
        "C:/Windows/Fonts/segoeui.ttf",
        "DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def encode(root: Path, names: list[str], lang: str, output: Path, speed: float) -> None:
    clips = [(root / name, json.loads((root / name / "recording.json").read_text(encoding="utf-8"))) for name in names]
    total = sum(math.ceil(data["duration"] * FPS) for _, data in clips)
    with tempfile.TemporaryDirectory(prefix="jap-encode-") as temp:
        video = Path(temp) / "capture.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pixel_format",
            "rgb24",
            "-video_size",
            f"{WIDTH}x{HEIGHT + FOOTER}",
            "-framerate",
            str(FPS * speed),
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        frame_number = 0
        try:
            for clip_number, (directory, data) in enumerate(clips):
                times = [f["t"] for f in data["frames"]]
                pointer = data["pointer"]
                pointer_times = [p["t"] for p in pointer]
                chapters = data["chapters"]
                chapter_times = [c["t"] for c in chapters]
                cached_index, screenshot = -1, None
                for tick in range(math.ceil(data["duration"] * FPS)):
                    t = tick / FPS
                    index = max(0, bisect.bisect_right(times, t) - 1)
                    if index != cached_index:
                        with Image.open(directory / data["frames"][index]["file"]) as source:
                            screen = source.convert("RGB")
                            if screen.height > screen.width * 9 / 16 + 2:
                                screen = screen.crop((0, 0, screen.width, round(screen.width * 9 / 16)))
                            screenshot = ImageOps.contain(screen, (WIDTH, HEIGHT), Image.Resampling.LANCZOS)
                        cached_index = index
                    canvas = Image.new("RGB", (WIDTH, HEIGHT + FOOTER), "#0b1f3a")
                    left, top = (WIDTH - screenshot.width) // 2, (HEIGHT - screenshot.height) // 2
                    canvas.paste(screenshot, (left, top))
                    draw = ImageDraw.Draw(canvas)
                    meta = data["frames"][index]["metadata"]
                    viewport_width = data.get("viewport", {}).get("width", meta["deviceWidth"])
                    sx = sy = screenshot.width / viewport_width
                    if pointer and t >= pointer_times[0]:
                        point = pointer[max(0, bisect.bisect_right(pointer_times, t) - 1)]
                        x, y = left + point["x"] * sx, top + point["y"] * sy
                        for click in pointer:
                            age = t - click["t"]
                            if click.get("click") and 0 <= age < 0.6:
                                cx, cy = left + click["x"] * sx, top + click["y"] * sy
                                radius = 10 + 24 * age / 0.6
                                draw.ellipse(
                                    (cx - radius, cy - radius, cx + radius, cy + radius), outline="#ff662f", width=3
                                )
                        draw.polygon(
                            [
                                (x, y),
                                (x + 3, y + 23),
                                (x + 9, y + 17),
                                (x + 15, y + 27),
                                (x + 20, y + 24),
                                (x + 14, y + 14),
                                (x + 23, y + 13),
                            ],
                            fill="white",
                            outline="#0b1f3a",
                            width=2,
                        )
                    chapter = chapters[max(0, bisect.bisect_right(chapter_times, t) - 1)]
                    label = "操作演示" if lang == "zh" else "WORKFLOW WALKTHROUGH"
                    draw.text(
                        (24, HEIGHT + 10),
                        f"{label}  /  {clip_number + 1:02d}  /  {speed:g}x",
                        font=font(13, lang),
                        fill="#80dfcc",
                    )
                    draw.text((24, HEIGHT + 34), chapter[lang], font=font(23, lang), fill="white")
                    draw.rectangle(
                        (0, HEIGHT + FOOTER - 4, WIDTH * (frame_number + 1) / total, HEIGHT + FOOTER), fill="#80dfcc"
                    )
                    process.stdin.write(canvas.tobytes())
                    frame_number += 1
            process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError("ffmpeg video encoding failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-filter_complex",
                "split[a][b];[a]palettegen=stats_mode=diff:max_colors=192[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                "-loop",
                "0",
                str(output),
            ],
            check=True,
        )
    print(
        f"{output.name}: {frame_number} frames, {frame_number / (FPS * speed):.1f}s, {output.stat().st_size:,} bytes",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings", type=Path, default=Path("output/readme-motion"))
    parser.add_argument("--output", type=Path, default=Path("docs/assets/demo"))
    parser.add_argument("--kind", choices=["workflow", "setup", "materials", "all"], default="all")
    parser.add_argument("--speed", type=float, default=1.5, help="Playback speed multiplier (default: 1.5)")
    args = parser.parse_args()
    if not math.isfinite(args.speed) or not 0.25 <= args.speed <= 4:
        parser.error("--speed must be between 0.25 and 4")
    args.output.mkdir(parents=True, exist_ok=True)
    for lang in ("en", "zh"):
        recipes = {
            "workflow": [f"workbench-{lang}", "live", "form"],
            "setup": [f"setup-{lang}"],
            "materials": [f"materials-{lang}", f"resume-{lang}"],
        }
        for name, clips in recipes.items():
            if args.kind in (name, "all"):
                encode(args.recordings, clips, lang, args.output / f"{name}-{lang}.gif", args.speed)


if __name__ == "__main__":
    main()
