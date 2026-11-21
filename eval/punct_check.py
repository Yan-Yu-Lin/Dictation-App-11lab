"""Check normalize_mixed_punctuation on hand-picked cases and on every past transcript in the log.

    uv run python eval/punct_check.py            # cases + diff of all changed past transcripts
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from dictation import normalize_mixed_punctuation as norm  # noqa: E402

CASES = [
    # (input, expected)
    ("Yeah，as expected，which is a good thing。", "Yeah, as expected, which is a good thing."),
    ("I don't understand why it's doing it. Well。", "I don't understand why it's doing it. Well."),
    ("還真的不明白為什麼。Why it's doing that? It's just weird. Testing, testing。",
     "還真的不明白為什麼。Why it's doing that? It's just weird. Testing, testing."),
    ("我剛剛選擇 GitHub Mobile，然後並沒有看到。", "我剛剛選擇 GitHub Mobile，然後並沒有看到。"),
    ("所以我現在我想要念 DGX，它也會變成 DGX？", "所以我現在我想要念 DGX，它也會變成 DGX？"),
    ("所以我現在我想要念 DGX, 它也會變成 DGX?", "所以我現在我想要念 DGX，它也會變成 DGX？"),
    ("Anyway，我現在可以測了嗎？", "Anyway，我現在可以測了嗎？"),
    ("沒有什麼問題 by the way。Anyway，我現在可以測了嗎？", "沒有什麼問題 by the way。Anyway，我現在可以測了嗎？"),
    ("Take your time。你可以慢慢地去思考。", "Take your time. 你可以慢慢地去思考。"),
    ("版本是 3.5，時間 10:30，用 Node.js 跑。", "版本是 3.5，時間 10:30，用 Node.js 跑。"),
    ("就是不要碰 DGX, Spark，跟他們的資產", "就是不要碰 DGX, Spark，跟他們的資產"),
    ("Claude Code，Codex 這些東西它都聽得懂", "Claude Code，Codex 這些東西它都聽得懂"),
    ("OK。", "OK."),
    ("Harness.", "Harness."),
    ("我在 omarchy 上面。Hyprland is great。", "我在 omarchy 上面。Hyprland is great."),
    ("然後我在想說上面—— Your agent needs your input. 上面有很多 sensitive 的東西嗎？",
     "然後我在想說上面—— Your agent needs your input. 上面有很多 sensitive 的東西嗎？"),
    ("我們要用 DGX. 然後再說", "我們要用 DGX。然後再說"),
]


def main():
    bad = 0
    for src, want in CASES:
        got = norm(src)
        ok = got == want
        bad += not ok
        print(("✅" if ok else "❌"), repr(src), "->", repr(got), "" if ok else f"(want {want!r})")
    print(f"\n{len(CASES) - bad}/{len(CASES)} cases ok\n")

    path = "/tmp/past_transcripts.txt"
    if os.path.exists(path):
        lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
        changed = [(l, norm(l)) for l in lines if norm(l) != l]
        print(f"past transcripts: {len(lines)}, changed: {len(changed)}")
        for before, after in changed:
            print("  -", before)
            print("  +", after)


if __name__ == "__main__":
    main()
