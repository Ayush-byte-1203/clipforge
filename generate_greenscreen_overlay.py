"""Generate an animated YouTube Like, Share & Subscribe green screen overlay video."""
import math
import subprocess
import wave
import struct
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

# 1. Synthesize crisp SFX (click, subscribe click, bell ding)
rate = 44100
dur = 4.0
samples = [0] * int(rate * dur)

def add_click(t_start):
    idx = int(t_start * rate)
    for i in range(int(0.02 * rate)):
        if idx + i < len(samples):
            v = math.sin(2 * math.pi * 1400 * (i / rate)) * math.exp(-i / (0.004 * rate))
            samples[idx + i] += int(v * 16000)

def add_bell(t_start):
    idx = int(t_start * rate)
    for i in range(int(1.2 * rate)):
        if idx + i < len(samples):
            t = i / rate
            v = (0.75 * math.sin(2 * math.pi * 2450 * t) + 0.25 * math.sin(2 * math.pi * 4900 * t)) * math.exp(-t * 5.0)
            samples[idx + i] += int(v * 22000)

add_click(1.2)  # Like click
add_click(2.2)  # Subscribe click
add_bell(3.0)   # Bell chime

sfx_path = ASSETS / "sfx.wav"
with wave.open(str(sfx_path), "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(rate)
    data = struct.pack("<" + "h" * len(samples), *[max(-32767, min(32767, s)) for s in samples])
    wav.writeframes(data)

# 2. Render frames (30 fps, 120 frames total)
fps = 30
total_frames = int(dur * fps)
W, H = 1080, 360
GREEN = (0, 255, 0)

font_path = None
for f in ASSETS.glob("fonts/*.ttf"):
    font_path = str(f)
    break

# Attempt to load Poppins or default
try:
    if font_path:
        f_main = ImageFont.truetype(font_path, 36)
        f_sub = ImageFont.truetype(font_path, 32)
        f_emoji = ImageFont.truetype(font_path, 42)
    else:
        f_main = ImageFont.load_default()
        f_sub = ImageFont.load_default()
        f_emoji = ImageFont.load_default()
except Exception:
    f_main = f_sub = f_emoji = ImageFont.load_default()

frames_dir = ASSETS / "frames"
frames_dir.mkdir(exist_ok=True)

for i in range(total_frames):
    t = i / fps
    img = Image.new("RGB", (W, H), GREEN)
    draw = ImageDraw.Draw(img)

    # Slide in / out animation
    # 0.0 - 0.4s: slide up from y=380 to y=60
    # 3.6 - 4.0s: slide down from y=60 to y=380
    if t < 0.4:
        progress = t / 0.4
        card_y = int(360 - (360 - 70) * (1 - (1 - progress) ** 2))
    elif t > 3.5:
        progress = (t - 3.5) / 0.5
        card_y = int(70 + (360 - 70) * (progress ** 2))
    else:
        card_y = 70

    card_x = (W - 880) // 2
    card_w, card_h = 880, 180

    # Draw Dark Pill Container
    draw.rounded_rectangle([card_x, card_y, card_x + card_w, card_y + card_h], radius=90, fill=(24, 24, 24), outline=(70, 70, 70), width=4)

    # 1. YouTube Play Logo / Avatar
    logo_x = card_x + 35
    logo_y = card_y + (card_h - 70) // 2
    draw.rounded_rectangle([logo_x, logo_y, logo_x + 95, logo_y + 70], radius=20, fill=(255, 0, 0))
    # White triangle
    tx, ty = logo_x + 38, logo_y + 18
    draw.polygon([(tx, ty), (tx + 26, ty + 17), (tx, ty + 34)], fill=(255, 255, 255))

    # 2. LIKE button (t < 1.2 not liked, t >= 1.2 liked with blue/gold)
    liked = t >= 1.2
    like_x = card_x + 160
    like_y = card_y + 65
    like_col = (0, 215, 255) if liked else (200, 200, 200)
    like_text = "👍 LIKED" if liked else "👍 LIKE"
    draw.text((like_x, like_y), like_text, fill=like_col, font=f_main)

    # 3. SUBSCRIBE button (Red pill -> Grey pill)
    subbed = t >= 2.2
    btn_x = card_x + 390
    btn_y = card_y + (card_h - 68) // 2
    btn_w = 260
    btn_bg = (60, 60, 60) if subbed else (255, 0, 0)
    draw.rounded_rectangle([btn_x, btn_y, btn_x + btn_w, btn_y + 68], radius=34, fill=btn_bg)
    btn_text = "✓ SUBSCRIBED" if subbed else "SUBSCRIBE"
    draw.text((btn_x + 28, btn_y + 14), btn_text, fill=(255, 255, 255), font=f_sub)

    # 4. BELL Icon
    belled = t >= 3.0
    bell_x = card_x + 690
    bell_y = card_y + 65
    bell_col = (255, 215, 0) if belled else (180, 180, 180)
    bell_text = "🔔" if not belled else "🔔✨"
    draw.text((bell_x, bell_y - 8), bell_text, fill=bell_col, font=f_emoji)

    # 5. Animated cursor arrow (👆)
    # Cursor starts at bottom right, moves to LIKE at 1.2, SUBSCRIBE at 2.2, BELL at 3.0
    cur_x, cur_y = -100, -100
    if 0.5 <= t < 1.5:
        # Move to LIKE
        cur_x = like_x + 40
        cur_y = like_y + 35
    elif 1.5 <= t < 2.5:
        # Move to SUBSCRIBE
        cur_x = btn_x + 120
        cur_y = btn_y + 35
    elif 2.5 <= t < 3.4:
        # Move to BELL
        cur_x = bell_x + 25
        cur_y = bell_y + 35

    if cur_x > 0:
        draw.text((cur_x, cur_y), "👆", fill=(255, 255, 255), font=f_emoji)

    img.save(frames_dir / f"frame_{i:04d}.png")

print("Rendered all frames!")

# 3. Encode to MP4 with FFmpeg & add audio
from core import ffmpeg_bin
out_mp4 = ASSETS / "subscribe_greenscreen.mp4"
cmd = [
    ffmpeg_bin(), "-y",
    "-r", "30",
    "-i", str(frames_dir / "frame_%04d.png"),
    "-i", str(sfx_path),
    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k",
    "-shortest",
    str(out_mp4)
]
subprocess.run(cmd, check=True)
print(f"Generated {out_mp4} ({out_mp4.stat().st_size} bytes)")

# Clean up temp frames
for f in frames_dir.glob("*.png"):
    f.unlink()
frames_dir.rmdir()
