"""Jason (the server) vs JSON (the format): can keyterms get both right?"""

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ab_eval import transcribe, tts  # noqa: E402

BASE = json.load(open(os.path.join(os.path.dirname(HERE), "keyterms.json")))
CLIPS = [
    "JSON 資料格式在我的 Jason 伺服器上。",
    "我的 Jason 伺服器上面跑了一個服務。",
    "這個 API 會回傳 JSON。",
    "要寫成 Jason，而非 JSON。",
]
SETUPS = {
    "now (Jason)": BASE,
    "+JSON": BASE + ["JSON"],
    "+JSON 格式": BASE + ["JSON 格式", "JSON 資料"],
}


async def main():
    for ref in CLIPS:
        pcm = tts(ref)
        print(f"\n=== {ref}")
        for name, kt in SETUPS.items():
            cfg = dict(model_id="scribe_v3_live", no_verbatim=True, keyterms=kt)
            runs = await asyncio.gather(*(transcribe(pcm, cfg) for _ in range(3)))
            print(f"  {name:12} " + " | ".join(r["raw"] or str(r["error"]) for r in runs))


asyncio.run(main())
