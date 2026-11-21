"""Quick check: does a sentence come out right with and without keyterms.json?

    uv run python eval/term_check.py "感覺應該不太需要訂 Wispr Flow。"
"""

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ab_eval import transcribe, tts  # noqa: E402

KEYTERMS = json.load(open(os.path.join(os.path.dirname(HERE), "keyterms.json")))


async def main():
    for ref in sys.argv[1:]:
        pcm = tts(ref)
        print(f"\n=== {ref}")
        for name, kt in (("v3", []), ("v3+keyterms.json", KEYTERMS)):
            cfg = dict(model_id="scribe_v3_live", no_verbatim=True, keyterms=kt)
            runs = await asyncio.gather(*(transcribe(pcm, cfg) for _ in range(3)))
            for r in runs:
                print(f"  {name:17} {r['raw'] if r['raw'] is not None else r['error']}")


asyncio.run(main())
