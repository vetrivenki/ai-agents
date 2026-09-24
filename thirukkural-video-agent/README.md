# Thirukkural AI Video Agent

Daily automation that creates one vertical Thirukkural video and uploads it to YouTube.

## Pipeline

1. Fetch the exact Thirukkural text and meanings from the public Thirukkural API.
2. Use Meta Model API / Muse Spark to create a short-form narration, scene plan, title, description, and hashtags.
3. Use Meta Muse Image to generate vertical visuals for each scene.
4. Generate Tamil narration with Edge TTS.
5. Assemble a 1080x1920 MP4 with FFmpeg.
6. Upload the result to YouTube with the YouTube Data API.
7. Store progress in `state.json` so the next run uses the next Kural.

Meta Model API base URL: `https://api.meta.ai/v1`

Models:
- Text: `muse-spark-1.3`
- Image: `muse-image-1.0`

> Muse Video generation is not currently exposed in the public Meta Model API, so this project creates motion-style videos from Muse Image scenes with FFmpeg. The renderer can later be replaced with a Muse Video generator when Meta exposes one.

## GitHub Secrets

Create these repository secrets:

- `MODEL_API_KEY`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

Optional repository variables/secrets:

- `YOUTUBE_PRIVACY_STATUS` — defaults to `private`
- `YOUTUBE_CATEGORY_ID` — defaults to `27`

Do not commit API keys or OAuth credentials.

## YouTube OAuth

The uploader requires the scope:

`https://www.googleapis.com/auth/youtube.upload`

A refresh token is used by GitHub Actions so the daily job can upload without interactive login.

## Daily schedule

The included GitHub Actions workflow runs once per day at 8:00 AM Central Time equivalent during CDT (13:00 UTC). GitHub cron uses UTC. You can change the cron expression in `.github/workflows/thirukkural-daily.yml`.

## Manual test

```bash
cd thirukkural-video-agent
python -m pip install -r requirements.txt
python main.py --kural 9 --no-upload
```

To upload:

```bash
python main.py --kural 9
```

## Output

Each run creates:

```
output/kural-0009/
  metadata.json
  narration.mp3
  scene-01.png
  scene-02.png
  scene-03.png
  scene-04.png
  final.mp4
```

## Character/style continuity

The prompt keeps Saint Thiruvalluvar visually consistent:
- elderly Tamil sage
- white beard and hair
- traditional white clothing
- palm-leaf manuscript
- classical Tamil setting
- cinematic devotional lighting
- 9:16 vertical composition

For even tighter continuity, later add a fixed reference image to the Muse Image request chain.

## Source text

The exact Kural is retrieved by number from:

`https://kural.codewithram.dev/api/kural/{id}`

This avoids asking an LLM to invent or recall the canonical couplet.
