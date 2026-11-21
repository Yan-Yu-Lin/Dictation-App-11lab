"""A/B test Scribe realtime models on the same audio.

Generates test sentences with macOS TTS (zh_TW voice), sends identical audio
to each config over the raw realtime WebSocket, and prints raw / OpenCC /
edited output plus commit latency.

    uv run python eval/ab_eval.py                 # all configs, TTS sentences
    uv run python eval/ab_eval.py --wav my.wav    # your own recording (any format afconvert reads)
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

import opencc
import websockets
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

SENTENCES = [
    "我待會要用 Claude Code 改一下這個腳本。",
    "只有這個問題比較麻煩，其他都還好。",
    "這個干擾太大了，我們需要干預一下。",
    "我剛剛用 Codex 跟 Claude 討論了一下 OpenCC 的問題。",
    "帳號裡面的資料我才剛剛整理完，裡面還有一些東西。",
    "你去 research 一下這個 API，然後 deploy 到 server 上面。",
    "我在軍營裡面，所以沒看到這個新聞，待會再看。",
    "這個 keyterm library 可以放一萬個詞，蠻好用的。",
]

KEYTERMS = ["Claude Code", "Claude", "Codex", "OpenCC", "keyterm", "Raycast"]
EDIT = (
    "Convert any Simplified Chinese characters to Traditional Chinese as written "
    "in Taiwan (zh-TW). Do not change wording, do not translate, keep English "
    "words exactly as they are."
)

CONFIGS = {
    "v2 (current)": dict(model_id="scribe_v2_realtime", no_verbatim=True),
    "v3": dict(model_id="scribe_v3_live", no_verbatim=True),
    "v3+keyterms": dict(model_id="scribe_v3_live", no_verbatim=True, keyterms=KEYTERMS),
    "v3+keyterms+edit": dict(
        model_id="scribe_v3_live", no_verbatim=True, keyterms=KEYTERMS, transcript_edit=EDIT
    ),
}


def to_pcm16k(src: str) -> bytes:
    out = tempfile.mktemp(suffix=".wav")
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, out],
        check=True,
    )
    with open(out, "rb") as f:
        data = f.read()
    os.remove(out)
    # strip WAV header: find "data" chunk
    idx = data.find(b"data")
    return data[idx + 8 :]


def tts(sentence: str) -> bytes:
    aiff = tempfile.mktemp(suffix=".aiff")
    subprocess.run(["say", "-v", "Meijia", "-o", aiff, sentence], check=True)
    pcm = to_pcm16k(aiff)
    os.remove(aiff)
    return pcm


def build_url(cfg: dict) -> str:
    params = [
        ("model_id", cfg["model_id"]),
        ("audio_format", "pcm_16000"),
        ("commit_strategy", "manual"),
    ]
    if cfg.get("no_verbatim"):
        params.append(("no_verbatim", "true"))
    for term in cfg.get("keyterms", []):
        params.append(("keyterms", term))
    if cfg.get("transcript_edit"):
        params.append(("transcript_edit", cfg["transcript_edit"]))
    if cfg.get("language_code"):
        params.append(("language_code", cfg["language_code"]))
    return "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + urllib.parse.urlencode(params)


async def transcribe(pcm: bytes, cfg: dict, timeout: float = 8.0) -> dict:
    url = build_url(cfg)
    headers = {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}
    result = {"raw": None, "edited": None, "latency": None, "edit_latency": None, "error": None}
    async with websockets.connect(url, additional_headers=headers) as ws:
        chunk = 3200  # 0.1 s
        for i in range(0, len(pcm), chunk):
            await ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(pcm[i : i + chunk]).decode(),
                "commit": False,
                "sample_rate": 16000,
            }))
            await asyncio.sleep(0.02)
        t0 = time.monotonic()
        await ws.send(json.dumps({
            "message_type": "input_audio_chunk", "audio_base_64": "",
            "commit": True, "sample_rate": 16000,
        }))
        want_edit = bool(cfg.get("transcript_edit"))
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            mt = msg.get("message_type")
            if mt == "committed_transcript" and result["raw"] is None:
                result["raw"] = msg.get("text", "")
                result["latency"] = time.monotonic() - t0
                if not want_edit:
                    break
            elif mt == "edited_transcript":
                result["edited"] = msg.get("edited_text", "")
                result["edit_latency"] = time.monotonic() - t0
                break
            elif mt not in ("session_started", "partial_transcript"):
                result["error"] = f"{mt}: {msg.get('error') or msg.get('warning')}"
                if mt != "warning":
                    break
    return result


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", help="use your own recording instead of TTS")
    ap.add_argument("--only", help="comma-separated config names")
    args = ap.parse_args()

    s2t = opencc.OpenCC("s2t")
    if args.wav:
        clips = [("(your recording)", to_pcm16k(args.wav))]
    else:
        clips = [(s, tts(s)) for s in SENTENCES]
    configs = {k: v for k, v in CONFIGS.items() if not args.only or k in args.only.split(",")}

    for ref, pcm in clips:
        print(f"\n=== 原句: {ref}  ({len(pcm) / 32000:.1f}s)")
        results = await asyncio.gather(*(transcribe(pcm, cfg) for cfg in configs.values()))
        for name, r in zip(configs, results):
            if r["error"] and r["raw"] is None:
                print(f"  {name:18} ERROR {r['error']}")
                continue
            lat = f"{r['latency']:.2f}s" if r["latency"] else "-"
            print(f"  {name:18} [{lat}] raw   : {r['raw']}")
            print(f"  {'':18}         opencc: {s2t.convert(r['raw'] or '')}")
            if r["edited"] is not None:
                print(f"  {'':18} [{r['edit_latency']:.2f}s] edit  : {r['edited']}")
            if r["error"]:
                print(f"  {'':18}         note  : {r['error']}")


if __name__ == "__main__":
    asyncio.run(main())
