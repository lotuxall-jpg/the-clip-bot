import os
import re
import json
import asyncio
import tempfile
import subprocess
import numpy as np
from pathlib import Path

from telegram import Update
from telegram.ext import (
Application, CommandHandler, MessageHandler,
filters, ContextTypes, ConversationHandler
)
import yt_dlp
import whisper
import anthropic

# ── States ──────────────────────────────────────────────────────────────────

WAITING_FOR_URL, WAITING_FOR_DESCRIPTION = range(2)

# ── Config (set via environment variables) ───────────────────────────────────

TELEGRAM_TOKEN    = os.environ[“TELEGRAM_TOKEN”]
ANTHROPIC_API_KEY = os.environ[“ANTHROPIC_API_KEY”]
ALLOWED_USER_ID   = int(os.environ.get(“ALLOWED_USER_ID”, “0”))  # 0 = allow anyone

WHISPER_MODEL  = os.environ.get(“WHISPER_MODEL”, “base”)
MAX_CLIPS      = int(os.environ.get(“MAX_CLIPS”, “4”))
CLIP_PAD_SEC   = int(os.environ.get(“CLIP_PAD_SEC”, “3”))
MAX_CLIP_SEC   = int(os.environ.get(“MAX_CLIP_SEC”, “60”))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_youtube_url(text: str) -> bool:
return bool(re.search(r”(youtube.com/watch|youtu.be/)”, text))

def download_video(url: str, out_dir: str) -> tuple[str, str]:
video_path = os.path.join(out_dir, “video.mp4”)
audio_path = os.path.join(out_dir, “audio.wav”)

```
ydl_opts = {
    "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
    "outtmpl": video_path,
    "merge_output_format": "mp4",
    "quiet": True,
}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([url])

subprocess.run([
    "ffmpeg", "-y", "-i", video_path,
    "-ar", "16000", "-ac", "1", "-vn", audio_path
], check=True, capture_output=True)

return video_path, audio_path
```

def transcribe(audio_path: str) -> list[dict]:
model = whisper.load_model(WHISPER_MODEL)
result = model.transcribe(audio_path, verbose=False)
return [{“start”: s[“start”], “end”: s[“end”], “text”: s[“text”].strip()}
for s in result[“segments”]]

def detect_loud_moments(audio_path: str, top_n: int = 15) -> list[float]:
cmd = [“ffmpeg”, “-i”, audio_path, “-f”, “f32le”, “-ar”, “16000”, “-ac”, “1”, “pipe:1”]
result = subprocess.run(cmd, capture_output=True, check=True)
samples = np.frombuffer(result.stdout, dtype=np.float32)
sr = 16000
energies = []
for i in range(0, len(samples) - sr, sr):
rms = float(np.sqrt(np.mean(samples[i:i+sr]**2)))
energies.append((i / sr, rms))

```
energies.sort(key=lambda x: -x[1])
# Deduplicate: keep moments at least 10s apart
kept, result_ts = [], []
for ts, _ in energies:
    if all(abs(ts - k) > 10 for k in kept):
        kept.append(ts)
        result_ts.append(ts)
    if len(result_ts) >= top_n:
        break
return sorted(result_ts)
```

def ask_claude_for_clips(segments: list[dict], loud_moments: list[float],
description: str, video_duration: float) -> list[dict]:
transcript_text = “\n”.join(
f”[{s[‘start’]:.1f}s - {s[‘end’]:.1f}s] {s[‘text’]}” for s in segments
)
loud_text = “, “.join(f”{t:.1f}s” for t in loud_moments)

```
prompt = f"""You are an expert video editor. Analyze this video and find the best clips to extract.
```

VIDEO DURATION: {video_duration:.0f} seconds

USER REQUEST: “{description}”

LOUD/EXCITING MOMENTS (high audio energy timestamps):
{loud_text}

TRANSCRIPT (with timestamps):
{transcript_text}

Based on the user’s request, loud moments, and transcript, identify the {MAX_CLIPS} best clips to extract.

Rules:

- Each clip must be between {10} and {MAX_CLIP_SEC} seconds long
- Focus on what the user asked for
- Prioritize loud/exciting moments when relevant
- Make sure clips have complete sentences/thoughts

Respond ONLY with a JSON array like this:
[
{{“start”: 12.5, “end”: 35.0, “reason”: “Why this clip is great”}},
{{“start”: 88.0, “end”: 120.0, “reason”: “Why this clip is great”}}
]
No other text.”””

```
response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1000,
    messages=[{"role": "user", "content": prompt}]
)

raw = response.content[0].text.strip()
raw = re.sub(r"```json|```", "", raw).strip()
clips = json.loads(raw)

# Apply padding and clamp to video duration
padded = []
for c in clips:
    start = max(0, c["start"] - CLIP_PAD_SEC)
    end   = min(video_duration, c["end"] + CLIP_PAD_SEC)
    padded.append({"start": start, "end": end, "reason": c.get("reason", "")})
return padded
```

def cut_clip(video_path: str, start: float, end: float, out_path: str):
duration = end - start
subprocess.run([
“ffmpeg”, “-y”,
“-ss”, str(start),
“-i”, video_path,
“-t”, str(duration),
“-c:v”, “libx264”, “-c:a”, “aac”,
“-crf”, “28”,       # good quality, smaller file
“-preset”, “fast”,
out_path
], check=True, capture_output=True)

def get_video_duration(video_path: str) -> float:
result = subprocess.run([
“ffprobe”, “-v”, “quiet”, “-print_format”, “json”,
“-show_format”, video_path
], capture_output=True, check=True)
info = json.loads(result.stdout)
return float(info[“format”][“duration”])

# ── Bot Handlers ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
“👋 *AI Clip Bot*\n\n”
“Send me a YouTube URL and I’ll find the best clips for you!\n\n”
“Just paste a YouTube link to get started.”,
parse_mode=“Markdown”
)
return WAITING_FOR_URL

async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
url = update.message.text.strip()
if not is_youtube_url(url):
await update.message.reply_text(“⚠️ That doesn’t look like a YouTube URL. Please send a valid YouTube link.”)
return WAITING_FOR_URL

```
context.user_data["url"] = url
await update.message.reply_text(
    "✅ Got the link!\n\n"
    "Now tell me *what to clip*. For example:\n"
    "• _Find the funniest moments_\n"
    "• _Clip every time someone says 'let's go'_\n"
    "• _Find the most intense/exciting parts_\n"
    "• _Clip the best highlights_",
    parse_mode="Markdown"
)
return WAITING_FOR_DESCRIPTION
```

async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
description = update.message.text.strip()
url = context.user_data.get(“url”)

```
await update.message.reply_text("🎬 Processing your video... This may take a few minutes. Hang tight!")

try:
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Download
        await update.message.reply_text("⬇️ Downloading video...")
        video_path, audio_path = download_video(url, tmp)
        duration = get_video_duration(video_path)

        # 2. Transcribe
        await update.message.reply_text("🎙️ Transcribing audio...")
        segments = transcribe(audio_path)

        # 3. Detect loud moments
        await update.message.reply_text("🔊 Analyzing audio energy...")
        loud_moments = detect_loud_moments(audio_path)

        # 4. Claude picks best clips
        await update.message.reply_text("🤖 AI is selecting the best clips...")
        clips = ask_claude_for_clips(segments, loud_moments, description, duration)

        if not clips:
            await update.message.reply_text("😕 Couldn't find any matching clips. Try a different description!")
            return ConversationHandler.END

        # 5. Cut and send clips
        await update.message.reply_text(f"✂️ Cutting {len(clips)} clips and sending them to you...")
        for i, clip in enumerate(clips, 1):
            clip_path = os.path.join(tmp, f"clip_{i}.mp4")
            cut_clip(video_path, clip["start"], clip["end"], clip_path)

            caption = f"📎 *Clip {i}/{len(clips)}*\n_{clip.get('reason', '')}_ \n⏱ {clip['start']:.0f}s – {clip['end']:.0f}s"
            with open(clip_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=caption,
                    parse_mode="Markdown",
                    supports_streaming=True
                )

        await update.message.reply_text(
            "✅ All done! Send another YouTube link to clip a new video."
        )

except Exception as e:
    await update.message.reply_text(f"❌ Something went wrong:\n`{str(e)}`", parse_mode="Markdown")

return WAITING_FOR_URL
```

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(“Cancelled. Send a YouTube link whenever you’re ready!”)
return WAITING_FOR_URL

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
app = Application.builder().token(TELEGRAM_TOKEN).build()

```
conv = ConversationHandler(
    entry_points=[
        CommandHandler("start", start),
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
    ],
    states={
        WAITING_FOR_URL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
        ],
        WAITING_FOR_DESCRIPTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description)
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

app.add_handler(conv)
print("🤖 Bot is running...")
app.run_polling()
```

if **name** == “**main**”:
main()
