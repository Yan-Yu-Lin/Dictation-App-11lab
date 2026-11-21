"""previous_text that also names the vocabulary: does it help keyterms that a plain 繁體 context hurt?"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_eval import tts  # noqa: E402
from prevtext_eval import run  # noqa: E402

KT = ["Claude Code", "Codex", "omarchy", "Hyprland", "OpenCC", "keyterm"]
PREVS = {
    "prev plain": "這是台灣繁體中文的聽寫紀錄。我們剛剛討論了軟體、網路、資訊跟帳號設定。",
    "prev +vocab": "這是台灣繁體中文的聽寫紀錄，常提到 Claude Code、Codex、omarchy、Hyprland、OpenCC。",
}
CLIPS = [
    "omarchy 上面的 Hyprland 設定待會再改。",
    "帳號裡面的資料我才剛剛整理完，裡面還有一些東西。",
    "我把 Claude Code 跟 Codex 都打開了，待會再測。",
]


async def main():
    for ref in CLIPS:
        pcm = tts(ref)
        print(f"\n=== {ref}")
        for name, prev in PREVS.items():
            res = await asyncio.gather(*(run(pcm, "scribe_v3_live", prev, KT) for _ in range(3)))
            for final, _ in res:
                print(f"  {name:12} {final}")


asyncio.run(main())
