"""Run TTS audio through the exact SDK path dictation.py uses (patched URL,
model profile, keyterms.json, OpenCC config, replacements) without a mic.

    uv run python eval/app_path_check.py v3
"""

import asyncio
import base64
import json
import os
import sys
import urllib.parse

import opencc
from elevenlabs import AudioFormat, CommitStrategy, ElevenLabs, RealtimeAudioOptions, RealtimeEvents

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from ab_eval import tts  # noqa: E402

import dictation  # noqa: E402  (module import only; the app does not start)

CLIPS = [
    "我待會要用 Claude Code 改一下這個腳本。",
    "omarchy 上面的 Hyprland 設定待會再改。",
    "帳號裡面的資料我才剛剛整理完，裡面還有一些東西。",
]


async def one(model: str, pcm: bytes) -> str:
    profile = dictation.MODEL_PROFILES[model]
    keyterms = json.load(open(dictation.KEYTERMS_PATH)) if profile["use_keyterms"] else []
    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"]).speech_to_text.realtime
    original = client._build_websocket_url
    extra = [("no_verbatim", "true")] + [("keyterms", t) for t in keyterms]
    client._build_websocket_url = lambda *a, **k: f"{original(*a, **k)}&{urllib.parse.urlencode(extra)}"

    conn = await client.connect(RealtimeAudioOptions(
        model_id=profile["model_id"], audio_format=AudioFormat.PCM_16000,
        sample_rate=16000, commit_strategy=CommitStrategy.MANUAL, include_timestamps=False,
    ))
    done = asyncio.get_running_loop().create_future()
    conn.on(RealtimeEvents.COMMITTED_TRANSCRIPT, lambda d: done.done() or done.set_result(d.get("text", "")))
    conn.on(RealtimeEvents.ERROR, lambda e: done.done() or done.set_result(f"ERROR {e}"))
    for i in range(0, len(pcm), 3200):
        await conn.send({"audio_base_64": base64.b64encode(pcm[i:i + 3200]).decode()})
        await asyncio.sleep(0.02)
    await conn.commit()
    text = await asyncio.wait_for(done, 8)
    await conn.close()
    return text


async def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "v3"
    profile = dictation.MODEL_PROFILES[model]
    cc = opencc.OpenCC(profile["opencc_config"])
    reps = sorted(json.load(open(dictation.CHARACTER_REPLACEMENTS_PATH)).items(), key=lambda kv: -len(kv[0]))
    print(f"model={profile['model_id']} opencc={profile['opencc_config']}")
    for ref in CLIPS:
        raw = await one(model, tts(ref))
        out = cc.convert(raw)
        for s, t in reps:
            out = out.replace(s, t)
        print(f"  原句: {ref}\n  raw : {raw}\n  貼上: {out}\n")


asyncio.run(main())
