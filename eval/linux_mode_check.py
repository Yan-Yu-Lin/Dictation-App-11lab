"""Check the Linux daemon's live/batch paths through the real DictationApp code, without a mic.

Audio comes from 16 kHz mono WAV files (make them anywhere, e.g. `say -v Meijia` on a Mac
then `afconvert -f WAVE -d LEI16@16000 -c 1`). They are fed into the app's own audio
callback, then the app's own start/stop code runs. Nothing is pasted (paste is captured),
no socket is opened, the floating dot and media pausing are off, so it is safe to run
while the real daemon is running.

    uv run python eval/linux_mode_check.py WAV_DIR             # live vs batch, every *.wav
    uv run python eval/linux_mode_check.py WAV_DIR --checks    # + empty / order / bad key / mid-rec switch
    uv run python eval/linux_mode_check.py WAV_DIR --long      # ~80 s live recording: one paste, at stop
"""

import argparse
import asyncio
import glob
import os
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dictation  # noqa: E402  (module import only; the daemon does not start)

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


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return w.readframes(w.getnframes())


def make_app(mode: str, model: str) -> "dictation.DictationApp":
    app = dictation.DictationApp(model=model, mode=mode)
    app.audio_stream = FakeStream()
    return app


async def speak(app, pcm: bytes, realtime: bool):
    step = dictation.CHUNK_SIZE * 2
    for i in range(0, len(pcm), step):
        app.audio_callback(pcm[i : i + step], dictation.CHUNK_SIZE, None, None)
        await asyncio.sleep(dictation.CHUNK_SIZE / dictation.SAMPLE_RATE if realtime else 0)


async def settle(app):
    for _ in range(600):
        busy = [t for t in (app.cleanup_task, app.batch_tail) if t is not None and not t.done()]
        if not busy and not app.paste_lock.locked():
            await asyncio.sleep(0.3)
            if not app.paste_lock.locked():
                return
        await asyncio.sleep(0.05)


async def one(app, pcm: bytes) -> tuple[str, float]:
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
    for name, pcm in clips:
        print(f"\n=== {name}  ({len(pcm) / 32000:.1f}s)")
        for label, app in (("live", live), ("batch", batch)):
            text, took = await one(app, pcm)
            print(f"  {label:5} [停止後 {took:.2f}s] {text!r}")


async def checks(clips, model: str):
    print("\n=== checks (batch) ===")
    app = make_app("batch", model)
    long_pcm = max((pcm for _, pcm in clips), key=len)
    short_pcm = min((pcm for _, pcm in clips), key=len)

    before = len(pasted)
    await app.start_recording()
    await app.stop_recording()
    await settle(app)
    print(f"  empty recording -> pasted {len(pasted) - before} (want 0)")

    before = len(pasted)
    await one(app, bytes(32000 * 2))
    print(f"  2 s of silence  -> pasted {len(pasted) - before} (want 0)")

    before = len(pasted)
    await app.start_recording()
    await speak(app, long_pcm, realtime=False)
    await app.stop_recording()
    await app.start_recording()
    await speak(app, short_pcm, realtime=False)
    await app.stop_recording()
    await settle(app)
    got = pasted[before:]
    print(f"  order           -> {len(got)} pastes, lengths {[len(t) for t in got]} (want long first)")

    before = len(pasted)
    await app.start_recording()
    app.mode = "live"
    await speak(app, short_pcm, realtime=False)
    await app.stop_recording()
    await settle(app)
    print(f"  switch mid-rec  -> {len(pasted) - before} paste(s) (want 1, done as batch)")
    app.mode = "batch"

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


async def long_live(clips, model: str):
    """A recording long enough for Scribe to commit on its own (about every 35 s)."""
    print("\n=== long live recording ===")
    gap = bytes(int(dictation.SAMPLE_RATE * 0.6) * 2)
    pcm = gap.join(pcm for _, pcm in clips)
    while len(pcm) < 80 * 32000:
        pcm = pcm + gap + pcm
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
        print(f"  text ({len(text)} chars): {text[:160]}...")
    leftovers = {
        name: getattr(app, name)
        for name in ("live_segments", "stopping_sessions", "commit_events", "paste_tasks")
        if getattr(app, name)
    }
    print(f"  leftover state         -> {leftovers or 'none'} (want none)")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav_dir")
    ap.add_argument("--model", default="v3", choices=sorted(dictation.MODEL_PROFILES))
    ap.add_argument("--checks", action="store_true")
    ap.add_argument("--long", action="store_true")
    args = ap.parse_args()

    dictation.event_loop = asyncio.get_running_loop()
    clips = [(os.path.basename(p), read_wav(p)) for p in sorted(glob.glob(os.path.join(args.wav_dir, "*.wav")))]
    if not clips:
        sys.exit(f"no .wav files in {args.wav_dir}")
    if args.long:
        await long_live(clips, args.model)
        return
    await compare(clips, args.model)
    if args.checks:
        await checks(clips, args.model)


if __name__ == "__main__":
    asyncio.run(main())
