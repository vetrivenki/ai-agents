import argparse
import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

import edge_tts
import requests
from openai import OpenAI

META_BASE_URL = "https://api.meta.ai/v1"
TEXT_MODEL = "muse-spark-1.3"
IMAGE_MODEL = "muse-image-1.0"
KURAL_API = "https://kural.codewithram.dev/api/kural/{number}"

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
STATE_FILE = ROOT / "state.json"


def load_state():
    if not STATE_FILE.exists():
        return {"last_published_kural": 0}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(number):
    STATE_FILE.write_text(
        json.dumps({"last_published_kural": number}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_kural(number):
    r = requests.get(KURAL_API.format(number=number), timeout=30)
    r.raise_for_status()
    data = r.json()
    return {
        "number": data["number"],
        "section_ta": data["section"]["names"]["ta"],
        "section_en": data["section"]["names"]["en"],
        "chapter_ta": data["chapter"]["names"]["ta"],
        "chapter_en": data["chapter"]["names"]["en"],
        "line1": data["kural"][0],
        "line2": data["kural"][1],
        "meaning_ta": data["meaning"].get("ta_mu_va") or data["meaning"].get("ta_salamon") or "",
        "meaning_en": data["meaning"].get("en") or "",
    }


def meta_client():
    key = os.environ["MODEL_API_KEY"]
    return OpenAI(base_url=META_BASE_URL, api_key=key)


def create_content_plan(client, kural):
    canonical = f'{kural["line1"]}\n{kural["line2"]}'
    prompt = f"""
You are creating one premium 9:16 Tamil YouTube Short about Thirukkural.

Kural number: {kural["number"]}
Chapter: {kural["chapter_ta"]} / {kural["chapter_en"]}
Canonical Tamil text:
{canonical}

Reference Tamil meaning:
{kural["meaning_ta"]}

Reference English meaning:
{kural["meaning_en"]}

Rules:
- Never change, paraphrase, correct, or regenerate the canonical Kural text.
- Produce approximately 35-45 seconds total.
- Tamil narration must be natural, respectful, simple, and clearly audible.
- Include a concise English meaning for subtitles/description.
- Background music guidance must always say soft and lower than narration.
- Create exactly 4 scenes.
- Maintain the same Saint Thiruvalluvar character in every scene: elderly Tamil sage, dignified face, long white beard, white hair tied traditionally, simple white clothing, palm-leaf manuscript.
- Cinematic classical Tamil setting, warm devotional lighting, realistic illustration, vertical 9:16, no text rendered inside generated images.
- Scene 1 should introduce Thiruvalluvar and the Kural.
- Scenes 2-3 should visualize the meaning.
- Scene 4 should close with a reflective moral.
- Do not include modern political figures, brands, or copyrighted characters.

Return ONLY valid JSON:
{{
  "title": "...",
  "description": "...",
  "hashtags": ["#திருக்குறள்", "#Thirukkural", "#Tamil"],
  "narration_ta": "...",
  "meaning_en": "...",
  "scenes": [
    {{"duration": 9, "visual_prompt": "..."}},
    {{"duration": 9, "visual_prompt": "..."}},
    {{"duration": 9, "visual_prompt": "..."}},
    {{"duration": 9, "visual_prompt": "..."}}
  ]
}}
"""
    response = client.responses.create(
        model=TEXT_MODEL,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    return json.loads(response.output_text)


def generate_image(client, prompt, destination):
    response = client.responses.create(
        model=IMAGE_MODEL,
        input=prompt,
    )
    image_item = next(
        item for item in response.output
        if getattr(item, "type", None) == "image_generation_call"
    )
    destination.write_bytes(base64.b64decode(image_item.result))


async def generate_tamil_voice(text, destination):
    voice = os.environ.get("TAMIL_TTS_VOICE", "ta-IN-ValluvarNeural")
    try:
        communicate = edge_tts.Communicate(text, voice=voice, rate="-5%")
        await communicate.save(str(destination))
    except Exception:
        fallback = edge_tts.Communicate(text, voice="ta-IN-PallaviNeural", rate="-5%")
        await fallback.save(str(destination))


def probe_duration(audio_path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    return float(subprocess.check_output(cmd, text=True).strip())


def render_video(scene_paths, audio_path, output_path):
    audio_duration = probe_duration(audio_path)
    per_scene = max(audio_duration / len(scene_paths), 1.0)

    clips = []
    for idx, image in enumerate(scene_paths, 1):
        clip = output_path.parent / f"clip-{idx:02d}.mp4"
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "zoompan=z='min(zoom+0.0008,1.08)':d=225:s=1080x1920:fps=25,"
            "format=yuv420p"
        )
        subprocess.run([
            "ffmpeg", "-y", "-loop", "1", "-i", str(image),
            "-t", f"{per_scene:.3f}",
            "-vf", vf,
            "-r", "25", "-an", str(clip)
        ], check=True)
        clips.append(clip)

    concat_file = output_path.parent / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{c.name}'" for c in clips) + "\n",
        encoding="utf-8",
    )

    silent_video = output_path.parent / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_file), "-c", "copy", str(silent_video)
    ], check=True, cwd=output_path.parent)

    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(silent_video),
        "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(output_path)
    ], check=True)


def upload_youtube(video_path, plan, kural_number):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    hashtags = " ".join(plan.get("hashtags", []))
    description = (
        f'{plan["description"]}\n\n'
        f'English meaning: {plan["meaning_en"]}\n\n'
        f'{hashtags}\n\n'
        f'Thirukkural #{kural_number}'
    )

    body = {
        "snippet": {
            "title": plan["title"][:100],
            "description": description,
            "tags": [x.lstrip("#") for x in plan.get("hashtags", [])][:20],
            "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID", "27"),
            "defaultLanguage": "ta",
        },
        "status": {
            "privacyStatus": os.environ.get("YOUTUBE_PRIVACY_STATUS", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
    )

    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kural", type=int)
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()

    state = load_state()
    number = args.kural or state.get("last_published_kural", 0) + 1
    if not 1 <= number <= 1330:
        raise SystemExit("Kural number must be between 1 and 1330.")

    workdir = OUTPUT / f"kural-{number:04d}"
    workdir.mkdir(parents=True, exist_ok=True)

    kural = fetch_kural(number)
    client = meta_client()
    plan = create_content_plan(client, kural)

    metadata = {"kural": kural, "plan": plan}
    (workdir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    scene_paths = []
    style_anchor = (
        "Same recurring Saint Thiruvalluvar character: elderly Tamil sage, dignified face, "
        "long white beard, traditional tied white hair, simple white clothing, palm-leaf manuscript. "
        "Cinematic realistic illustration, classical ancient Tamil setting, warm devotional light, "
        "high detail, vertical 9:16 portrait composition, no words, letters, captions, logos, or watermark."
    )

    for idx, scene in enumerate(plan["scenes"], 1):
        path = workdir / f"scene-{idx:02d}.png"
        generate_image(client, style_anchor + " " + scene["visual_prompt"], path)
        scene_paths.append(path)

    narration_path = workdir / "narration.mp3"
    asyncio.run(generate_tamil_voice(plan["narration_ta"], narration_path))

    final_path = workdir / "final.mp4"
    render_video(scene_paths, narration_path, final_path)

    if args.no_upload:
        print(f"Created {final_path}")
        return

    video_id = upload_youtube(final_path, plan, number)
    save_state(number)
    print(f"Uploaded Kural {number}: https://youtu.be/{video_id}")


if __name__ == "__main__":
    main()
