"""Compare live and batch mode through the real DictationApp code, without a mic.

TTS audio is fed into the app's own audio callback, then the app's own
start/stop code runs. Nothing is pasted (paste is captured) and no hotkey is
registered, so it is safe to run while the real app is running.

    uv run python eval/mode_check.py                # TTS sentences, live vs batch
    uv run python eval/mode_check.py --wav my.m4a   # your own recording
    uv run python eval/mode_check.py --checks       # also: empty, order, bad key
    uv run python eval/mode_check.py --long         # ~80 s live recording: one paste, at stop
"""

import argparse
import asyncio
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

# Importing dictation registers global hotkeys; swap in a no-op so this script
# never touches Hyper+D while the real app is running.
import quickmachotkey  # noqa: E402

quickmachotkey.quickHotKey = lambda **_: (lambda handler: handler)

from ab_eval import to_pcm16k, tts  # noqa: E402

import dictation  # noqa: E402

SENTENCES = [
    "嗯，我待會要用 Claude Code 改一下這個腳本，呃，然後 omarchy 上面的 Hyprland 設定待會再改。",
    "我想要比較一下即時版跟一次送出的版本，看哪一個比較穩定。",
    "Yeah, as expected, which is a good thing. 那我們就繼續往下做。",
    "你去 research 一下這個 API，然後 deploy 到 Jason server 上面，記得開無線偵錯。",
]

pasted: list[str] = []
notices: list[str] = []
dictation.paste_text = pasted.append
dictation.play_sound = lambda *a, **k: None
dictation.show_overlay_notice = notices.append
dictation.TRANSCRIPT_DEBUG_LOG = False


class FakeStream:
    """Stands in for the microphone stream."""

    active = False

    def is_active(self):
        return self.active

    def start_stream(self):
        self.active = True

    def stop_stream(self):
        self.active = False

    def close(self):
        pass


def make_app(mode: str, model: str) -> "dictation.DictationApp":
    app = dictation.DictationApp(model=model, mode=mode)
    app.audio_stream = FakeStream()
    return app


async def speak(app, pcm: bytes, realtime: bool):
    """Feed pcm through the app's audio callback, like the mic would."""
    step = dictation.CHUNK_SIZE * 2
    for i in range(0, len(pcm), step):
        app.audio_callback(pcm[i : i + step], dictation.CHUNK_SIZE, None, None)
        # Live needs real-time pacing, otherwise the 30 s queue drops audio.
        await asyncio.sleep(dictation.CHUNK_SIZE / dictation.SAMPLE_RATE if realtime else 0)


async def settle(app):
    """Wait until every background job of this app has finished."""
    for _ in range(600):
        busy = [t for t in (app.cleanup_task, app.batch_tail) if t is not None and not t.done()]
        if not busy and not app.paste_lock.locked():
            await asyncio.sleep(0.3)  # live: the paste is scheduled just after the commit event
            if not app.paste_lock.locked():
                return
        await asyncio.sleep(0.05)


async def one(app, pcm: bytes) -> tuple[str, float]:
    """One recording. Returns (pasted text, seconds from stop to paste)."""
    before = len(pasted)
    await app.start_recording()
    await speak(app, pcm, realtime=app.mode == "live")
    stopped = time.monotonic()
    await app.stop_recording()
    for _ in range(1200):
        if len(pasted) > before:
            took = time.monotonic() - stopped
            await settle(app)
            return pasted[-1], took
        tail = app.batch_tail if app.mode == "batch" else app.cleanup_task
        if tail is None or tail.done():
            await asyncio.sleep(0.4)
            if len(pasted) > before:
                return pasted[-1], time.monotonic() - stopped
            break
        await asyncio.sleep(0.02)
    await settle(app)
    return "", time.monotonic() - stopped


async def compare(clips, model: str):
    live = make_app("live", model)
    batch = make_app("batch", model)
    for ref, pcm in clips:
        print(f"\n=== 原句: {ref}  ({len(pcm) / 32000:.1f}s)")
        for name, app in (("live", live), ("batch", batch)):
            text, took = await one(app, pcm)
            print(f"  {name:5} [停止後 {took:.2f}s] {text!r}")


async def checks(model: str):
    print("\n=== checks (batch) ===")
    app = make_app("batch", model)
    long_pcm = tts(SENTENCES[0])
    short_pcm = tts("好。")

    before = len(pasted)
    await app.start_recording()
    await app.stop_recording()
    await settle(app)
    print(f"  empty recording -> pasted {len(pasted) - before} (want 0)")

    before = len(pasted)
    await one(app, bytes(32000 * 2))
    print(f"  2 s of silence  -> pasted {len(pasted) - before} (want 0)")

    # Long recording, then a short one right away: paste order must stay long, short.
    before = len(pasted)
    await app.start_recording()
    await speak(app, long_pcm, realtime=False)
    await app.stop_recording()
    await app.start_recording()
    await speak(app, short_pcm, realtime=False)
    await app.stop_recording()
    await settle(app)
    got = pasted[before:]
    print(f"  order           -> {got} (want long sentence first)")

    # Mode switch during a recording must not change that recording.
    before = len(pasted)
    await app.start_recording()
    app.mode = "live"
    await speak(app, short_pcm, realtime=False)
    await app.stop_recording()
    await settle(app)
    print(f"  switch mid-rec  -> {pasted[before:]} (want one paste, done as batch)")
    app.mode = "batch"

    # Bad key: no paste, a notice instead.
    real = app.elevenlabs
    app.elevenlabs = dictation.ElevenLabs(api_key="sk_invalid_for_test")
    before, notices_before = len(pasted), len(notices)
    await one(app, short_pcm)
    print(f"  bad key         -> pasted {len(pasted) - before} (want 0), notices {notices[notices_before:]}")
    app.elevenlabs = real

    leftovers = {
        name: getattr(app, name)
        for name in ("batch_sessions", "stopping_sessions", "commit_events", "audio_callback_counts")
        if getattr(app, name)
    }
    print(f"  leftover state  -> {leftovers or 'none'} (want none); "
          f"is_recording={app.is_recording}, active={app.active_session_id}")


LONG_SENTENCES = [
    "我現在在測試一個比較長的錄音，看看它中間會不會自己把文字貼出去。",
    "我有時候在講話的時候，還沒有要貼，可能在看其他東西。",
    "然後我會用 Claude Code 去改 omarchy 上面的 Hyprland 設定。",
    "The original design was to save memory, and I think that is a good thing.",
    "接下來我想要把這個工具做成一個完整的 Mac app，可以看歷史紀錄。",
    "如果第一次沒有貼到，我可以回去把那一段文字找回來。",
    "你去 research 一下這個 API，然後 deploy 到 Jason server 上面。",
    "最後再確認一次，所有的文字都要等我按結束之後才一起貼出去。",
]


def join_checks():
    cases = [
        (["我想要——", "去看一下"], "我想要去看一下"),
        (["這個東西。", "然後我們"], "這個東西。然後我們"),
        (["用 Claude Code", "改一下"], "用 Claude Code 改一下"),
        (["這是一個", "API 的問題"], "這是一個 API 的問題"),
        (["a good thing.", "Then we go"], "a good thing. Then we go"),
        (["a good thing.", "然後"], "a good thing. 然後"),
        (["數字是 3", "5 個"], "數字是 3 5 個"),
        (["只有一段"], "只有一段"),
        (["", "  ", "空的略過——"], "空的略過——"),
        (["第一段——", "", "第二段"], "第一段第二段"),
    ]
    bad = 0
    for segments, want in cases:
        got = dictation.join_live_segments(segments)
        if got != want:
            bad += 1
            print(f"  JOIN MISMATCH {segments} -> {got!r}, want {want!r}")
    print(f"  join_live_segments -> {len(cases) - bad}/{len(cases)} cases match")


async def long_live(model: str):
    """A recording long enough for Scribe to commit on its own (about every 35 s)."""
    print("\n=== long live recording ===")
    join_checks()
    gap = bytes(int(dictation.SAMPLE_RATE * 0.6) * 2)
    pcm = gap.join(tts(s) for s in LONG_SENTENCES * 2)
    print(f"  audio {len(pcm) / 32000:.0f}s, feeding in real time...")
    app = make_app("live", model)
    before = len(pasted)
    await app.start_recording()
    await speak(app, pcm, realtime=True)
    during = len(pasted) - before
    held = len(app.live_segments.get(app.active_session_id, []))
    await app.stop_recording()
    await settle(app)
    got = pasted[before:]
    print(f"  pasted while recording -> {during} (want 0); pieces held at stop -> {held}")
    print(f"  pasted after stop      -> {len(got)} (want 1)")
    for text in got:
        print(f"  text ({len(text)} chars): {text}")
    leftovers = {
        name: getattr(app, name)
        for name in ("live_segments", "stopping_sessions", "commit_events", "paste_tasks")
        if getattr(app, name)
    }
    print(f"  leftover state         -> {leftovers or 'none'} (want none)")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", help="use your own recording instead of TTS")
    ap.add_argument("--model", default="v3", choices=sorted(dictation.MODEL_PROFILES))
    ap.add_argument("--checks", action="store_true", help="also run the edge-case checks")
    ap.add_argument("--long", action="store_true", help="only run the long live recording check")
    args = ap.parse_args()

    dictation.event_loop = asyncio.get_running_loop()
    if args.long:
        await long_live(args.model)
        return
    if args.wav:
        clips = [("(your recording)", to_pcm16k(args.wav))]
    else:
        clips = [(s, tts(s)) for s in SENTENCES]
    await compare(clips, args.model)
    if args.checks:
        await checks(args.model)


if __name__ == "__main__":
    asyncio.run(main())
