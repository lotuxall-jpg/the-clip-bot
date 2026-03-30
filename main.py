# fix
import os
import re
import json
import asyncio
import tempfile
import subprocess
from pathlib import Path

from telegram import Update
from telegram.ext import (
Application, CommandHandler, MessageHandler,
filters, ContextTypes, ConversationHandler
)
import yt_dlp
import anthropic
from openai import OpenAI

# ── States ──────────────────────────────────────────────────────────────────

WAITING_FOR_URL, WAITING_FOR_DESCRIPTION = range(2)

# ── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN    = os.environ[“TELEGRAM_TOKEN”]
ANTHROPIC_API_KEY = os.environ[“ANTHROPIC_API_KEY”]
OPENAI_API_KEY    = os.environ[“OPENAI_API_KEY”]

MAX_CLIPS     = int(os.environ.get(“MAX_CLIPS”, “4”))
CLIP_PAD_SEC  = int(os.environ.get(“CLIP_PAD_SEC”, “3”))
MAX_CLIP_SEC  = int(os.environ.get(“MAX_CLIP_SEC”, “60”))

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_youtube_url(text: str) -> bool:
return bool(re.search(r”(youtube.com/watch|youtu.be/)”, text))

def download_video(url: str, out_dir: str) -> tuple[str, str]:
video_path = os.path.join(out_dir, “video.mp4”)
audio_path = os.path.join(out_dir, “audio.mp3”)

```
ydl_opts = {
    "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best",
    "outtmpl": video_path,
    "merge_output_format": "mp4",
    "quiet": True,
}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ydl.download([url])

# Extract audio as MP3 for Whisper API (max 25MB)
subprocess.run([
    "ffmpeg", "-y", "-i", video_path,
    "-ar", "16000", "-ac", "1", "-b:a", "32k",
    "-vn", audio_path
], check=True, capture_output=True)

return video_path, audio_path
```

def transcribe_audio(audio_path: str) -> list[dict]:
“”“Use OpenAI Whisper API — no local model needed.”””
with open(audio_path, “rb”) as f:
response = openai_client.audio.transcriptions.create(
model=“whisper-1”,
file=f,
response_format=“verbose_json”,
timestamp_granularities=[“segment”]
)
return [
{“start”: s.start, “end”: s.end, “text”: s.text.strip()}
for s in response.segments
]

def get_video_duration(video_path: str) -> float:
result = subprocess.run([
“ffprobe”, “-v”, “quiet”, “-print_format”, “json”,
“-show_format”, video_path
], capture_output=True, check=True)
info = json.loads(result.stdout)
return float(info[“format”][“duration”])

def ask_claude_for_clips(segments: list[dict], description: str, video_duration: float) -> list[dict]:
transcript_text = “\n”.join(
f”[{s[‘start’]:.1f}s - {s[‘end’]:.1f}s] {s[‘text’]}” for s in segments
)

```
prompt = f"""You are an expert video editor. Find the best clips to extract from this video.
```

VIDEO DURATION: {video_duration:.0f} seconds
USER REQUEST: “{description}”

TRANSCRIPT:
{transcript_text}

Find the {MAX_CLIPS} best clips based on the user’s request.

Rules:

- Each clip must be between 10 and {MAX_CLIP_SEC} seconds long
- Focus on what the user asked for
- Make sure clips contain complete thoughts/sentences

Respond ONLY with a JSON array:
[
{{“start”: 12.5, “end”: 35.0, “reason”: “Why this clip is great”}},
{{“start”: 88.0, “end”: 120.0, “reason”: “Why this clip is great”}}
]
No other text.”””

```
response = claude.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=1000,
    messages=[{"role": "user", "content": prompt}]
)

raw = response.content[0].text.strip()
raw = re.sub(r"```json|```", "", raw).strip()
clips = json.loads(raw)

padded = []
for c in clips:
    start = max(0, c["start"] - CLIP_PAD_SEC)
    end   = min(video_duration, c["end"] + CLIP_PAD_SEC)
    padded.append({"start": start, "end": end, "reason": c.get("reason", "")})
return padded
```

def cut_clip(video_path: str, start: float, end: float, out_path: str):
subprocess.run([
“ffmpeg”, “-y”,
“-ss”, str(start),
“-i”, video_path,
“-t”, str(end - start),
“-c:v”, “libx264”, “-c:a”, “aac”,
“-crf”, “30”, “-preset”, “fast”,
out_path
], check=True, capture_output=True)

# ── Bot Handlers ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
await update.message.reply_text(
“👋 *AI Clip Bot*\n\nSend me a YouTube URL and I’ll find the best clips for you!\n\nJust paste a YouTube link to get started.”,
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
    "✅ Got the link!\n\nNow tell me *what to clip*. For example:\n• _Find the funniest moments_\n• _Clip every time someone says 'let's go'_\n• _Find the most exciting parts_",
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
        await update.message.reply_text("⬇️ Downloading video...")
        video_path, audio_path = download_video(url, tmp)
        duration = get_video_duration(video_path)

        await update.message.reply_text("🎙️ Transcribing audio...")
        segments = transcribe_audio(audio_path)

        await update.message.reply_text("🤖 AI is selecting the best clips...")
        clips = ask_claude_for_clips(segments, description, duration)

        if not clips:
            await update.message.reply_text("😕 Couldn't find any matching clips. Try a different description!")
            return WAITING_FOR_URL

        await update.message.reply_text(f"✂️ Cutting {len(clips)} clips and sending them to you...")

        for i, clip in enumerate(clips, 1):
            clip_path = os.path.join(tmp, f"clip_{i}.mp4")
            cut_clip(video_path, clip["start"], clip["end"], clip_path)
            caption = f"📎 *Clip {i}/{len(clips)}*\n_{clip.get('reason', '')}_\n⏱ {clip['start']:.0f}s – {clip['end']:.0f}s"
            with open(clip_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=caption,
                    parse_mode="Markdown",
                    supports_streaming=True
                )

        await update.message.reply_text("✅ All done! Send another YouTube link to clip a new video.")

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
conv = ConversationHandler(
entry_points=[
CommandHandler(“start”, start),
MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)
],
states={
WAITING_FOR_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url)],
WAITING_FOR_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description)],
},
fallbacks=[CommandHandler(“cancel”, cancel)],
)
app.add_handler(conv)
print(“🤖 Bot is running…”)
app.run_polling()

if **name** == “**main**”:
main()
