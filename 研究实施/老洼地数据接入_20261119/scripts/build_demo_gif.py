"""Build an animated GitHub showcase GIF from live MineGrid-Agent workbench screenshots.

Renders sleek 1120x700 slides with step headers & progress bars, blends 6-frame
cross-fade transitions between scenes, and encodes an optimized 256-color GIF
via ffmpeg two-pass palettegen/paletteuse (< 3.5 MB).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont


REPO_ROOT = Path(__file__).resolve().parents[3]
SCREENSHOTS_DIR = REPO_ROOT / "docs" / "screenshots"
DEFAULT_OUT = SCREENSHOTS_DIR / "minegrid_demo.gif"

WIDTH = 1120
HEIGHT = 700
BAR_H = 54


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        p = Path(path)
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render_slide(img_path: Path, step_idx: int, total_steps: int, title: str, subtitle: str) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (15, 23, 42))
    draw = ImageDraw.Draw(canvas)

    # Top header bar (dark slate + emerald MineGrid accent)
    draw.rectangle([0, 0, WIDTH, BAR_H], fill=(15, 23, 42))
    draw.rectangle([0, BAR_H - 3, WIDTH, BAR_H], fill=(30, 41, 59))
    prog_w = int(WIDTH * (step_idx / total_steps))
    draw.rectangle([0, BAR_H - 3, prog_w, BAR_H], fill=(16, 185, 129))

    # Step pill
    pill_text = f"STEP {step_idx}/{total_steps}"
    f_pill = _load_font(13, bold=True)
    f_title = _load_font(18, bold=True)
    f_sub = _load_font(13)

    draw.rounded_rectangle([18, 12, 104, 38], radius=6, fill=(16, 185, 129))
    draw.text((28, 17), pill_text, fill=(255, 255, 255), font=f_pill)
    draw.text((120, 14), title, fill=(248, 250, 252), font=f_title)

    # Right-aligned subtitle
    bbox = draw.textbbox((0, 0), subtitle, font=f_sub)
    sub_w = bbox[2] - bbox[0]
    draw.text((max(460, WIDTH - sub_w - 20), 18), subtitle, fill=(148, 163, 184), font=f_sub)

    # Fit screenshot into viewport area below header bar
    body_h = HEIGHT - BAR_H
    src = Image.open(img_path).convert("RGB")
    scale = min(WIDTH / src.width, body_h / src.height)
    new_w = max(1, int(round(src.width * scale)))
    new_h = max(1, int(round(src.height * scale)))
    resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
    ox = (WIDTH - new_w) // 2
    oy = BAR_H + (body_h - new_h) // 2
    canvas.paste(resized, (ox, oy))
    return canvas


def build_demo_gif(out_path: Path, home_capture: Path | None = None) -> None:
    scenes: list[tuple[Path, str, str]] = []
    if home_capture and home_capture.is_file() and home_capture.stat().st_size > 20_000:
        scenes.append((
            home_capture,
            "Dual-Mode Operator Workbench (Autonomous vs. Strict Audit)",
            "Agent-First Workspace · FastAPI + React 19",
        ))
    scenes.extend([
        (
            SCREENSHOTS_DIR / "autonomous_sandbox_chat.png",
            "Read-Only Python Sandbox, Plan Tree & Inline SVG Charts",
            "AST-Audited Subprocess · Verified Provenance Badge",
        ),
        (
            SCREENSHOTS_DIR / "topology_and_powerflow.png",
            "pandapower AC Power Flow, Tap Sweep & VoI Tornado Chart",
            "Newton-Raphson Solver · Parameter Uncertainty Ranking",
        ),
        (
            SCREENSHOTS_DIR / "debate_and_tickets.png",
            "3-Way Parallel Subagent Debate & Approval Workflow",
            "Telemetry ⇄ Physics ⇄ Safety Arbitration",
        ),
    ])

    for p, _, _ in scenes:
        if not p.is_file():
            raise FileNotFoundError(f"Missing screenshot frame: {p}")

    total = len(scenes)
    slides = [
        _render_slide(p, idx + 1, total, title, sub)
        for idx, (p, title, sub) in enumerate(scenes)
    ]

    fps = 10
    hold_frames = 28      # 2.8s per slide
    fade_frames = 5       # 0.5s crossfade

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        frame_idx = 0

        def _save_frame(img: Image.Image) -> None:
            nonlocal frame_idx
            img.save(tmp / f"frame_{frame_idx:04d}.png", optimize=False)
            frame_idx += 1

        for i, slide in enumerate(slides):
            for _ in range(hold_frames):
                _save_frame(slide)
            next_slide = slides[(i + 1) % len(slides)]
            for f in range(1, fade_frames + 1):
                alpha = f / (fade_frames + 1)
                _save_frame(Image.blend(slide, next_slide, alpha))

        palette = tmp / "palette.png"
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            # Pure Pillow fallback if ffmpeg is unavailable
            out_frames = [Image.open(p) for p in sorted(tmp.glob("frame_*.png"))]
            out_frames[0].save(
                out_path,
                save_all=True,
                append_images=out_frames[1:],
                duration=int(1000 / fps),
                loop=0,
                optimize=True,
            )
            return

        subprocess.run(
            [
                ffmpeg_bin, "-y", "-framerate", str(fps),
                "-i", str(tmp / "frame_%04d.png"),
                "-vf", "palettegen=max_colors=192:stats_mode=diff",
                str(palette),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                ffmpeg_bin, "-y", "-framerate", str(fps),
                "-i", str(tmp / "frame_%04d.png"),
                "-i", str(palette),
                "-lavfi", "paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
                "-loop", "0",
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build_demo_gif(args.output, args.home)
    size_kb = args.output.stat().st_size / 1024.0
    print(f"[OK] Built demo GIF: {args.output} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
