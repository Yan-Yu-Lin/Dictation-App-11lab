"""Does a Traditional-Chinese previous_text reliably make Scribe v3 Live output 繁體?"""

import asyncio
import base64
import json
import os
import sys
import urllib.parse

import opencc
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_eval import SENTENCES, tts  # noqa: E402

PREV = "這是台灣繁體中文的聽寫紀錄。我們剛剛討論了軟體、網路、資訊跟帳號設定。"
KT = ["Claude Code", "Codex", "omarchy", "Hyprland", "OpenCC", "keyterm"]
EXTRA = [
    "omarchy 上面的 Hyprland 設定待會再改。",
    "這個軟體的網路設定跟資訊安全要再確認一下。",
    "我把 Claude Code 跟 Codex 都打開了，待會再測。",
]


async def run(pcm, model, prev, kt):
    params = [("model_id", model), ("audio_format", "pcm_16000"),
              ("commit_strategy", "manual"), ("no_verbatim", "true")]
    params += [("keyterms", k) for k in kt]
    url = "wss://api.elevenlabs.io/v1/speech-to-text/realtime?" + urllib.parse.urlencode(params)
    partials = []
    async with websockets.connect(url, additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}) as ws:
        async def reader():
            while True:
                m = json.loads(await ws.recv())
                if m["message_type"] == "partial_transcript" and m.get("text"):
                    partials.append(m["text"])
                elif m["message_type"] == "committed_transcript":
                    return m["text"]
                elif m["message_type"] not in ("session_started",):
                    return f"{m['message_type']}: {m.get('error')}"
        task = asyncio.create_task(reader())
        for n, i in enumerate(range(0, len(pcm), 3200)):
            msg = {"message_type": "input_audio_chunk",
                   "audio_base_64": base64.b64encode(pcm[i:i + 3200]).decode(),
                   "commit": False, "sample_rate": 16000}
            if n == 0 and prev:
                msg["previous_text"] = prev
            await ws.send(json.dumps(msg))
            await asyncio.sleep(0.1)  # real-time pace so partials appear
        await ws.send(json.dumps({"message_type": "input_audio_chunk", "audio_base_64": "",
                                  "commit": True, "sample_rate": 16000}))
        final = await asyncio.wait_for(task, 10)
    return final, (partials[len(partials) // 2] if partials else "")


async def main():
    s2twp = opencc.OpenCC("s2twp")
    for ref in SENTENCES + EXTRA:
        pcm = tts(ref)
        print(f"\n=== {ref}")
        jobs = {
            "v3 +prev +kt #1": run(pcm, "scribe_v3_live", PREV, KT),
            "v3 +prev +kt #2": run(pcm, "scribe_v3_live", PREV, KT),
            "v2 +prev": run(pcm, "scribe_v2_realtime", PREV, []),
        }
        res = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for name, r in zip(jobs, res):
            if isinstance(r, Exception):
                print(f"  {name:17} EXC {r}")
                continue
            final, mid = r
            print(f"  {name:17} final : {final}")
            print(f"  {'':17} s2twp : {s2twp.convert(final)}")
            print(f"  {'':17} partial(mid): {mid}")


asyncio.run(main())
