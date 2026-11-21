"""Which keyterm setup makes v3 write 'jason' (the server) instead of 'JSON'?"""

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ab_eval import transcribe, tts  # noqa: E402

BASE = json.load(open(os.path.join(os.path.dirname(HERE), "keyterms.json")))
CLIPS = [
    "我又連回來到我的 jason server 上面。",
    "這個服務是跑在 jason 上面的。",
    "不會連回到 jason 上面。",
]
SETUPS = {
    "current (jason)": BASE,
    "+Jason server": BASE + ["Jason server"],
    "Jason (cap)": [t for t in BASE if t != "jason"] + ["Jason"],
}


async def main():
    for ref in CLIPS:
        pcm = tts(ref)
        print(f"\n=== {ref}")
        for name, kt in SETUPS.items():
            cfg = dict(model_id="scribe_v3_live", no_verbatim=True, keyterms=kt)
            runs = await asyncio.gather(*(transcribe(pcm, cfg) for _ in range(3)))
            print(f"  {name:16} " + " | ".join(r["raw"] or str(r["error"]) for r in runs))


asyncio.run(main())
