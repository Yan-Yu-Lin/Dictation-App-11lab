"""Can Scribe v3 Live be pushed toward Traditional output? And does keyterm_library work?"""

import asyncio
import base64
import json
import os
import sys
import urllib.parse

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_eval import tts  # noqa: E402

CLIP = "帳號裡面的資料我才剛剛整理完，omarchy 上面的 Hyprland 設定待會再改。"
PREV_TW = "這是台灣繁體中文的聽寫紀錄。我們剛剛討論了軟體、網路、資訊跟帳號設定。"


async def run(label, query, first_extra=None):
    params = [("model_id", "scribe_v3_live"), ("audio_format", "pcm_16000"),
              ("commit_strategy", "manual"), ("no_verbatim", "true")] + query
    url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + urllib.parse.urlencode(params)
    pcm = PCM
    try:
        async with websockets.connect(url, additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}) as ws:
            for n, i in enumerate(range(0, len(pcm), 3200)):
                msg = {"message_type": "input_audio_chunk",
                       "audio_base_64": base64.b64encode(pcm[i:i + 3200]).decode(),
                       "commit": False, "sample_rate": 16000}
                if n == 0 and first_extra:
                    msg.update(first_extra)
                await ws.send(json.dumps(msg))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"message_type": "input_audio_chunk", "audio_base_64": "",
                                      "commit": True, "sample_rate": 16000}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 8))
                if m["message_type"] == "committed_transcript":
                    print(f"  {label:28} {m['text']}")
                    return
                if m["message_type"] not in ("session_started", "partial_transcript"):
                    print(f"  {label:28} {m['message_type']}: {m.get('error') or m}")
                    return
    except Exception as e:
        print(f"  {label:28} EXC {e}")


async def main():
    global PCM
    PCM = tts(CLIP)
    print("原句:", CLIP)
    await asyncio.gather(
        run("baseline", []),
        run("language_code=zh", [("language_code", "zh")]),
        run("language_code=zh-TW", [("language_code", "zh-TW")]),
        run("language_code=zh-Hant", [("language_code", "zh-Hant")]),
        run("previous_text (繁體)", [], {"previous_text": PREV_TW}),
        run("keyterm_library", [], {"keyterm_library": ["omarchy", "Hyprland", "Claude Code", "Codex"]}),
        run("keyterms 繁體詞", [("keyterms", "帳號"), ("keyterms", "裡面"), ("keyterms", "設定")]),
    )


asyncio.run(main())
