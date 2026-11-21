# ClipForge

Local tool that turns long videos, livestreams and uploaded files into 9:16 clips ranked by viral potential, with
word-highlight captions. Runs on your computer; your videos never leave it.

## Start
1. Install Python 3.10+ and (if you don't have it) ffmpeg from https://ffmpeg.org (a copy is also fetched automatically via `imageio-ffmpeg`).
2. Windows: double-click `run.bat`.  Mac/Linux: `./run.sh`.
3. Open http://localhost:8787, paste a link (or upload a file), press **Find the best clips**.

The first run downloads the speech model (about 150 MB for `base`) once. Then it works offline except for downloading videos.

## What it does
- **Any source**: YouTube, Twitch, Kick, Vimeo, X and anything else yt-dlp supports, plus uploaded files or a local file path.
- **Livestreams**: a live link is recorded for the number of minutes you choose (Advanced), then clipped. Finished streams are just videos.
- **Any length**: audio is transcribed as a stream and loudness is measured in chunks, so a 3-hour video works (it just takes longer).
- **Viral ranking**: every possible 20-60 s moment that starts and ends on a sentence is scored on hook (question, bold word,
  number), emotion, speaking pace, audio energy and word density. Optional: add a Hugging Face token and an LLM rates the
  best candidates for hook, emotion, value and shareability and writes the titles (final score = 65% AI, 35% built-in).
- **9:16 output**: *Fill* follows the speaker's face with smooth pans; *Fit* shows the whole frame over a blurred background (good for gameplay/slides).
- **Captions**: burned-in, 3-4 words at a time, the spoken word highlighted. Optional title banner for the first 3.5 s.
- **Downloads**: each clip in 480p, 720p or 1080p MP4. Other qualities render when you click download.

## Hugging Face (optional)
Create a token at https://huggingface.co/settings/tokens with **"Make calls to Inference Providers"** enabled, paste it under
Advanced, or set the environment variable `HF_TOKEN`. Default model order: `Qwen/Qwen2.5-72B-Instruct`, then
`meta-llama/Llama-3.3-70B-Instruct`, then `meta-llama/Llama-3.1-8B-Instruct`. You can type any chat model name instead.
Without a token everything still works using the built-in scoring.

## Saving space and saving to Google Drive
Open **Storage and saving** in the form.
- **Delete the source video when finished** (on by default) removes the big temporary files and keeps only the clips.
- **Clip qualities to export** are rendered up front (480p, 720p, 1080p). Once the source is deleted, only these can be downloaded.
- **Source quality to download** (720p or 480p) shrinks the temporary file.
- **Save clips to**, then optionally **Remove clips from this computer after saving**.
  - *Easiest, Google Drive:* install Google Drive for desktop, then enter the path of a folder inside it, for example
    `/Users/you/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive/ClipForge` (Mac) or `G:\My Drive\ClipForge` (Windows).
    The same works for an external disk, OneDrive or Dropbox folders.
  - *Without syncing a folder:* install rclone (https://rclone.org/install/), run `rclone config` once (new remote, name `gdrive`,
    type `drive`, accept the defaults and sign in in the browser), then enter `gdrive:ClipForge`. Many other services work the same way.
  - If saving fails, nothing is deleted locally and the error is shown.
- Processing always needs temporary space on this computer. ClipForge checks free space first and tells you if it is not enough.
  To use a different drive for the working files, set `CLIPFORGE_DATA` to a folder on it before starting.

## Models
Nothing to load by hand. The speech model (`base`, about 150 MB) downloads automatically the first time you process a video and is
reused after that. The Hugging Face AI rating is optional and runs online, so it downloads nothing.

## Notes
- Everything is stored in `data/jobs/`. Delete a folder to remove a project.
- Speech language is auto-detected. Captions use the Arial font if present; for Hindi or other non-Latin scripts install a font that covers them.
- YouTube changes often: if downloads fail, run `pip install -U yt-dlp`.
- If YouTube says "confirm you are not a bot", open Advanced and set **YouTube sign-in** to the browser where you are logged in to YouTube (Chrome or Firefox work best), or upload the video file instead.
- Only clip content you own or have permission to reuse.
- Test without internet: `python tests/e2e_test.py`, `python tests/llm_mock_test.py` and `python tests/transcribe_fallback_test.py`.
# clipforge
