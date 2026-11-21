"""Repeat runs to see how stable each config is (TTS clips, 3 runs each)."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_eval import transcribe, tts  # noqa: E402

CLIPS = [
    "我待會要用 Claude Code 改一下這個腳本。",
    "我把 Claude Code 跟 Codex 都打開了，待會再測。",
    "omarchy 上面的 Hyprland 設定要再改一下。",
]
CONFIGS = {
    "v3": dict(model_id="scribe_v3_live", no_verbatim=True),
    "v3+kt(en)": dict(model_id="scribe_v3_live", no_verbatim=True,
                     keyterms=["Claude Code", "Codex", "omarchy", "Hyprland"]),
    "v3+kt(en+zh)": dict(model_id="scribe_v3_live", no_verbatim=True,
                        keyterms=["Claude Code", "Codex", "omarchy", "Hyprland", "待會", "設定", "腳本"]),
    "v3 lang=zh": dict(model_id="scribe_v3_live", no_verbatim=True, language_code="zh"),
}


async def main():
    for ref in CLIPS:
        pcm = tts(ref)
        print(f"\n=== {ref}")
        for name, cfg in CONFIGS.items():
            runs = await asyncio.gather(*(transcribe(pcm, cfg) for _ in range(3)))
            for r in runs:
                print(f"  {name:13} {r['raw'] if r['raw'] is not None else 'ERR ' + str(r['error'])}")


asyncio.run(main())
