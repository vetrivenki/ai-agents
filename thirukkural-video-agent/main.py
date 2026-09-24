import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import unicodedata
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
    STATE_FILE.write_text(json.dumps({"last_published_kural": number}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    return OpenAI(base_url=META_BASE_URL, api_key=os.environ["MODEL_API_KEY"])


def create_content_plan(client, kural):
    canonical = f'{kural["line1"]}\n{kural["line2"]}'
    prompt = f"""
Create a premium vertical Tamil YouTube Short for Thirukkural {kural['number']}.
Canonical Kural (must never be changed):
{canonical}
Tamil meaning (must be spoken in full):
{kural['meaning_ta']}
English meaning (must be spoken in full):
{kural['meaning_en']}

Create exactly 4 visual scenes. Return ONLY valid JSON with title, description, hashtags and four visual prompts.
Do NOT create or paraphrase the spoken Kural or meanings; those are assembled separately from the canonical source.
{{"title":"...","description":"...","hashtags":["#திருக்குறள்","#Thirukkural","#Tamil"],"scenes":[{{"visual_prompt":"..."}},{{"visual_prompt":"..."}},{{"visual_prompt":"..."}},{{"visual_prompt":"..."}}]}}
"""
    response = client.responses.create(model=TEXT_MODEL, input=prompt, text={"format": {"type": "json_object"}})
    return json.loads(response.output_text)


def build_spoken_sections(kural):
    # Canonical source text is used verbatim. AI is never allowed to shorten these three sections.
    return {
        "kural_ta": f'{kural["line1"]} {kural["line2"]}',
        "meaning_ta": kural["meaning_ta"],
        "meaning_en": kural["meaning_en"],
    }


def generate_image(client, prompt, destination):
    response = client.responses.create(model=IMAGE_MODEL, input=prompt)
    image_item = next(item for item in response.output if getattr(item, "type", None) == "image_generation_call")
    destination.write_bytes(base64.b64decode(image_item.result))


async def generate_voice(text, destination, voice):
    communicate = edge_tts.Communicate(text, voice=voice, rate="-5%")
    await communicate.save(str(destination))


def probe_duration(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    return float(subprocess.check_output(cmd, text=True).strip())


def normalize_for_check(text):
    text = unicodedata.normalize("NFC", text or "").casefold()
    return "".join(ch for ch in text if ch.isalnum())


def transcribe_audio(client, audio_path):
    # Muse Voice Transcribe is used as the post-generation QA listener.
    with open(audio_path, "rb") as audio:
        transcript = client.audio.transcriptions.create(model="muse-voice-transcribe-1.0", file=audio)
    return getattr(transcript, "text", str(transcript))


def validate_section(client, label, expected_text, audio_path, report, max_attempts=3):
    expected = normalize_for_check(expected_text)
    if not expected:
        raise RuntimeError(f"{label}: canonical expected text is empty")
    if not audio_path.exists() or audio_path.stat().st_size < 1000 or probe_duration(audio_path) < 1.0:
        raise RuntimeError(f"{label}: audio is missing or too short")

    transcript = transcribe_audio(client, audio_path)
    heard = normalize_for_check(transcript)
    # Require the complete normalized source to be present, not just a percentage.
    passed = expected in heard
    report[label] = {"expected": expected_text, "transcript": transcript, "passed": passed, "duration": probe_duration(audio_path)}
    return passed


async def create_and_validate_audio(client, sections, workdir):
    specs = [
        ("kural_ta", sections["kural_ta"], os.environ.get("TAMIL_TTS_VOICE", "ta-IN-ValluvarNeural")),
        ("meaning_ta", sections["meaning_ta"], os.environ.get("TAMIL_TTS_VOICE", "ta-IN-ValluvarNeural")),
        ("meaning_en", sections["meaning_en"], os.environ.get("ENGLISH_TTS_VOICE", "en-US-GuyNeural")),
    ]
    report = {}
    paths = []
    for label, text, voice in specs:
        path = workdir / f"{label}.mp3"
        passed = False
        for attempt in range(1, 4):
            await generate_voice(text, path, voice)
            try:
                passed = validate_section(client, label, text, path, report)
            except Exception as exc:
                report[label] = {"passed": False, "attempt": attempt, "error": str(exc)}
                passed = False
            if passed:
                report[label]["attempt"] = attempt
                break
        if not passed:
            (workdir / "audio-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"AUDIO QA FAILED: {label} was not heard completely after 3 attempts. Upload blocked.")
        paths.append(path)

    (workdir / "audio-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def join_audio(audio_paths, destination):
    list_file = destination.parent / "audio-concat.txt"
    list_file.write_text("\n".join(f"file '{p.name}'" for p in audio_paths) + "\n", encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c:a", "libmp3lame", "-b:a", "192k", str(destination)], check=True, cwd=destination.parent)


def render_video(scene_paths, audio_path, output_path):
    audio_duration = probe_duration(audio_path)
    per_scene = max(audio_duration / len(scene_paths), 1.0)
    clips = []
    for idx, image in enumerate(scene_paths, 1):
        clip = output_path.parent / f"clip-{idx:02d}.mp4"
        frames = max(int(per_scene * 25), 25)
        vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,zoompan=z='min(zoom+0.0008,1.08)':d={frames}:s=1080x1920:fps=25,format=yuv420p"
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-t", f"{per_scene:.3f}", "-vf", vf, "-r", "25", "-an", str(clip)], check=True)
        clips.append(clip)
    concat_file = output_path.parent / "concat.txt"
    concat_file.write_text("\n".join(f"file '{c.name}'" for c in clips) + "\n", encoding="utf-8")
    silent_video = output_path.parent / "silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(silent_video)], check=True, cwd=output_path.parent)
    subprocess.run(["ffmpeg", "-y", "-i", str(silent_video), "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output_path)], check=True)
    # Final container check: video must not end before narration.
    if probe_duration(output_path) + 0.25 < probe_duration(audio_path):
        raise RuntimeError("FINAL VIDEO QA FAILED: video ends before the complete narration. Upload blocked.")


def upload_youtube(video_path, plan, kural):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    creds = Credentials(token=None, refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token", client_id=os.environ["YOUTUBE_CLIENT_ID"], client_secret=os.environ["YOUTUBE_CLIENT_SECRET"], scopes=["https://www.googleapis.com/auth/youtube.upload"])
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    hashtags = " ".join(plan.get("hashtags", []))
    description = f'{plan["description"]}\n\nTamil meaning: {kural["meaning_ta"]}\n\nEnglish meaning: {kural["meaning_en"]}\n\n{hashtags}\n\nThirukkural #{kural["number"]}'
    body = {"snippet": {"title": plan["title"][:100], "description": description, "tags": [x.lstrip("#") for x in plan.get("hashtags", [])][:20], "categoryId": os.environ.get("YOUTUBE_CATEGORY_ID", "27"), "defaultLanguage": "ta"}, "status": {"privacyStatus": os.environ.get("YOUTUBE_PRIVACY_STATUS", "private"), "selfDeclaredMadeForKids": False}}
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True))
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
    sections = build_spoken_sections(kural)
    (workdir / "metadata.json").write_text(json.dumps({"kural": kural, "plan": plan, "spoken_sections": sections}, ensure_ascii=False, indent=2), encoding="utf-8")

    scene_paths = []
    style_anchor = "Same recurring Saint Thiruvalluvar character: elderly Tamil sage, dignified face, long white beard, traditional tied white hair, simple white clothing, palm-leaf manuscript. Cinematic realistic illustration, classical ancient Tamil setting, warm devotional light, high detail, vertical 9:16, no text, logos, or watermark. "
    for idx, scene in enumerate(plan["scenes"], 1):
        path = workdir / f"scene-{idx:02d}.png"
        generate_image(client, style_anchor + scene["visual_prompt"], path)
        scene_paths.append(path)

    audio_paths = asyncio.run(create_and_validate_audio(client, sections, workdir))
    narration_path = workdir / "narration.mp3"
    join_audio(audio_paths, narration_path)
    final_path = workdir / "final.mp4"
    render_video(scene_paths, narration_path, final_path)

    if args.no_upload:
        print(f"Created and audio-QA passed: {final_path}")
        return
    video_id = upload_youtube(final_path, plan, kural)
    save_state(number)
    print(f"Audio QA PASSED. Uploaded Kural {number}: https://youtu.be/{video_id}")


if __name__ == "__main__":
    main()
