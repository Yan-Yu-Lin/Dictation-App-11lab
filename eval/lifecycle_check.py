"""Offline checks of the delivery lifecycle (no network, no mic, no desktop).

Covers the scenarios from the 2026-11-28 astra review:
  R3  live stop: an in-flight auto-commit must not end the wait early, and a
      late final piece must not cause a second paste
  R4  paste order across live and batch recordings
  R8  the overlay is hidden only when nothing is pending
  R2  shutdown waits for stopped recordings (finalizers) to paste
  R5/R7 MediaPauser keeps ownership when D-Bus calls fail; failed Pause is not owned
  R10 4xx is not retried
Exits non-zero when a check fails.

    uv run python eval/lifecycle_check.py
"""

import asyncio
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import dictation  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail=""):
    print(("✅" if ok else "❌"), name, detail)
    if not ok:
        failures.append(name)


pasted: list[str] = []
overlay: list[str] = []
dictation.play_sound = lambda *a, **k: None
dictation.TRANSCRIPT_DEBUG_LOG = False
dictation.hide_overlay = lambda: overlay.append("hide")
dictation.set_overlay_finalizing = lambda: overlay.append("finalizing")
dictation.set_overlay_recording = lambda: overlay.append("recording")
dictation.show_overlay_notice = lambda t: overlay.append("notice " + t)


async def fake_paste(text, session_id):
    pasted.append(text)


class FakeConnection:
    """Answers each commit like Scribe: one committed_transcript per commit, in order.

    `answers` is a list of (delay_seconds, text), one per commit the app will send.
    """

    def __init__(self, app, sid, answers):
        self.app, self.sid, self.answers = app, sid, list(answers)
        self.closed = False
        self.commits = 0
        self.sent_seconds = 0.0

    async def send(self, data):
        import base64 as b64
        self.sent_seconds += len(b64.b64decode(data["audio_base_64"])) / 32000

    async def commit(self):
        self.commits += 1
        self.sent_at_commit = getattr(self, "sent_at_commit", []) + [self.sent_seconds]
        if self.answers:
            delay, text = self.answers.pop(0)
            loop = asyncio.get_running_loop()
            # Scribe answers commits in order: never before the previous answer.
            at = max(loop.time() + delay, getattr(self, "last_answer_at", 0.0) + 0.01)
            self.last_answer_at = at
            loop.call_at(at, self.app.on_committed_transcript, {"text": text}, self.sid)

    async def close(self):
        self.closed = True


class FakeStream:
    active = False

    def is_active(self):
        return self.active

    def start_stream(self):
        self.active = True

    def stop_stream(self):
        self.active = False

    def close(self):
        pass


def make_app(mode):
    app = dictation.DictationApp(model="v3", mode=mode)
    app.audio_stream = FakeStream()
    app._process_final_transcript = fake_paste
    return app


async def live_session(app, answers, held=(), audio_seconds=0.0):
    """Run one live recording through the real start/stop code with a fake socket."""
    real_connect = app.elevenlabs.speech_to_text.realtime.connect

    async def no_connect(*a, **k):
        raise RuntimeError("offline test")

    app.elevenlabs.speech_to_text.realtime.connect = no_connect
    sid = app.session_id + 1
    # Start without connecting, then attach the fake connection by hand.
    app.mode = "batch"
    await app.start_recording()
    app.batch_sessions.discard(sid)
    app.mode = "live"
    conn = FakeConnection(app, sid, answers)
    app.connection = conn
    for text in held:  # pieces from earlier commits that were already answered
        app.commits_sent[sid] += 1
        app.on_committed_transcript({"text": text}, sid)
    # Audio sent during the recording, through the real counting code.
    chunk = bytes(32000)  # 1 s
    for _ in range(int(audio_seconds)):
        await conn.send({"audio_base_64": dictation.base64.b64encode(chunk).decode()})
        await app._count_sent_audio(conn, sid, chunk)
    app.last_connection = conn
    await app.stop_recording()
    app.elevenlabs.speech_to_text.realtime.connect = real_connect
    return sid


async def settle(app, limit=10):
    end = time.monotonic() + limit
    while app.finalizers and time.monotonic() < end:
        await asyncio.sleep(0.05)


async def r3():
    print("\n== R3 live stop barrier ==")
    app = make_app("live")
    # 33 s of audio: the app itself commits at 25 s (so Scribe never auto-commits),
    # then the stop commits the rest. The 25 s answer is fast, the tail slow (3 s):
    # the wait must cover both answers (B1).
    pasted.clear()
    await live_session(app, [(0.1, "first 25 s,"), (3.0, "real tail")], audio_seconds=33)
    await settle(app, limit=15)
    conn = app.last_connection
    check("app commits at 25 s itself (2 commits for 33 s)", conn.commits == 2, conn.commits)
    check("auto-commit-sized recording keeps a slow tail", pasted == ["first 25 s, real tail"], pasted)

    # 60 s, answers arrive late and out of the "35 s" rhythm: still all of it, once.
    pasted.clear()
    await live_session(app, [(2.5, "a"), (0.2, "b"), (1.0, "c")], audio_seconds=60)
    await settle(app, limit=15)
    conn = app.last_connection
    check("60 s: 3 commits, every answer kept, in order", conn.commits == 3 and pasted == ["abc"], (conn.commits, pasted))

    # The stop always sends >= 0.3 s of padding before its commit (no commit_throttled).
    pasted.clear()
    await live_session(app, [(0.05, "x")], audio_seconds=25)
    await settle(app)
    conn = app.last_connection
    gaps = [b - a for a, b in zip([0.0] + conn.sent_at_commit, conn.sent_at_commit)]
    check("every commit has >= 0.3 s new audio", all(g >= 0.3 for g in gaps), gaps)

    # Held pieces + a final tail that arrives late (3 s): included, one paste.
    pasted.clear()
    await live_session(app, [(3.0, "last words")], held=["First words——"])
    await settle(app, limit=15)
    check("slow tail is kept, still one paste", pasted == ["First words last words"], pasted)

    # The answer never comes (connection dead): the held pieces are still pasted, once.
    pasted.clear()
    old = dictation.FINAL_TRANSCRIPT_MAX_SECONDS
    dictation.FINAL_TRANSCRIPT_MAX_SECONDS = 0.5
    await live_session(app, [(2.0, "too late")], held=["held piece"])
    await settle(app)
    await asyncio.sleep(2.0)  # the answer arrives after the session is closed
    dictation.FINAL_TRANSCRIPT_MAX_SECONDS = old
    check("no answer: held text pasted once, late answer ignored", pasted == ["held piece"], pasted)

    # Short recording, no auto-commit risk: ends right after the one answer.
    pasted.clear()
    t0 = time.monotonic()
    await live_session(app, [(0.1, "short one")])
    await settle(app)
    took = time.monotonic() - t0
    check("normal stop waits only for its own answer", pasted == ["short one"] and took < 1.5, f"{pasted} {took:.2f}s")

    # An empty answer still counts as the answer.
    pasted.clear()
    await live_session(app, [(0.1, "")], held=["held only"])
    await settle(app)
    check("empty final answer still ends the wait", pasted == ["held only"], pasted)
    left = {n: getattr(app, n) for n in ("live_segments", "commit_counts", "commits_sent", "commit_signal", "finalizers", "commit_events") if getattr(app, n)}
    check("no leftover live state", not left, left)


async def r4_r8():
    print("\n== R4 order across modes, R8 overlay ==")
    app = make_app("batch")
    pasted.clear()
    overlay.clear()
    release = threading.Event()

    def slow_batch(pcm, timeout):
        release.wait(5)
        return "first batch"

    app._batch_request = slow_batch
    await app.start_recording()
    app.audio_callback(bytes(32000), 1, None, None)
    await app.stop_recording()
    app.mode = "live"
    await live_session(app, [(0.05, "second live")])
    await asyncio.sleep(0.5)
    check("live waits for the earlier batch", pasted == [], pasted)
    check("overlay not hidden while the batch is pending", "hide" not in overlay, overlay)
    release.set()
    await settle(app)
    check("pastes in recording order", pasted == ["first batch", "second live"], pasted)
    check("overlay hidden once everything is done", overlay[-1:] == ["hide"], overlay[-3:])


async def b5():
    print("\n== B5 indicator transitions ==")
    app = make_app("batch")
    overlay.clear()
    release = threading.Event()

    def slow_batch(pcm, timeout):
        release.wait(5)
        return "older"

    app._batch_request = slow_batch
    await app.start_recording()
    app.audio_callback(bytes(32000), 1, None, None)
    await app.stop_recording()  # older recording, still finishing
    app.mode = "live"

    async def broken(*a, **k):
        raise RuntimeError("offline")

    app.elevenlabs.speech_to_text.realtime.connect = broken
    await app.start_recording()  # a new live recording that fails to connect
    await asyncio.sleep(0.2)
    check("failed start while older one finishes -> back to finalizing",
          overlay[-2:] == ["finalizing", "notice 連線失敗"] and not app.is_recording, overlay[-4:])
    release.set()
    await settle(app)
    check("hidden once the older one pasted", overlay[-1] == "hide", overlay[-3:])


def b6():
    print("\n== B6 dot dies mid-recording ==")
    shown = []

    class Notes:
        def show_recording(self): shown.append("recording")
        def show_finalizing(self): shown.append("finalizing")
        def show_notice(self, t): shown.append("notice")
        def hide(self): shown.append("hide")

    class DeadProcess:
        def poll(self): return 1

    dot = dictation.DotOverlay.__new__(dictation.DotOverlay)
    dot.send_lock = threading.Lock()
    dot.fallback = None
    dot.mode = "hidden"
    dot.process = DeadProcess()
    real = dictation.StatusNotifier
    dictation.StatusNotifier = Notes
    try:
        dot.mode = "recording"  # the dot got "recording", then died
        dot.show_partial("hello")
    finally:
        dictation.StatusNotifier = real
    check("first failed partial switches to notifications and shows recording",
          dot.fallback is not None and shown == ["recording"], shown)


async def r2():
    print("\n== R2 shutdown waits for stopped recordings ==")
    app = make_app("batch")
    pasted.clear()

    def slowish(pcm, timeout):
        time.sleep(0.8)
        return "pending at shutdown"

    app._batch_request = slowish
    await app.start_recording()
    app.audio_callback(bytes(32000), 1, None, None)
    await app.stop_recording()
    # What run_daemon's finally block does:
    done, not_done = await asyncio.wait(set(app.finalizers), timeout=dictation.SHUTDOWN_FINISH_SECONDS)
    check("shutdown wait lets the batch paste", pasted == ["pending at shutdown"] and not not_done, pasted)


def r10():
    print("\n== R10 no retry on 4xx ==")
    app = make_app("batch")
    for status, want in ((400, 1), (408, 1), (429, 1), (500, 2), (None, 2)):
        calls = []

        def failing(pcm, timeout, status=status):
            calls.append(1)
            err = RuntimeError("boom")
            if status is not None:
                err.status_code = status
            raise err

        app._batch_request = failing
        result = asyncio.run(app._transcribe_batch(bytes(3200), 1))
        check(f"status {status}: {want} attempt(s)", len(calls) == want and result is None, f"got {len(calls)}")


class FakeBus:
    """MediaPauser D-Bus stand-in: players with states, and switchable failures."""

    def __init__(self, states):
        self.states = dict(states)
        self.fail_pause = set()
        self.fail_play = set()
        self.calls = []

    def install(self, pauser):
        pauser._players = lambda: list(self.states)
        pauser._status = lambda p: self.states.get(p, "")

        def call(p, method):
            self.calls.append((p, method))
            if method == "Pause":
                if p in self.fail_pause:
                    return False
                self.states[p] = "Paused"
            if method == "Play":
                if p in self.fail_play:
                    return False
                self.states[p] = "Playing"
            return True

        pauser._call = call


def wait_idle(pauser, limit=5.0):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        with pauser.cond:
            if not pauser.busy:
                return
        time.sleep(0.02)


def media():
    print("\n== R5/R6/R7 media pauser ==")
    A, B = "org.mpris.MediaPlayer2.a", "org.mpris.MediaPlayer2.b"
    if not dictation.shutil.which("gdbus"):
        dictation.shutil.which = lambda name: "/usr/bin/" + name

    # Normal: pause the playing one, leave the paused one, resume only ours.
    p = dictation.MediaPauser()
    bus = FakeBus({A: "Playing", B: "Paused"})
    bus.install(p)
    p.set_recording(True)
    time.sleep(0.2); wait_idle(p)
    check("pauses only what was playing", bus.states == {A: "Paused", B: "Paused"} and p.owned == [A], (bus.states, p.owned))
    p.set_recording(False)
    time.sleep(0.2); wait_idle(p)
    check("resumes only what it paused", bus.states == {A: "Playing", B: "Paused"} and p.owned == [], (bus.states, p.owned))

    # R7: a failed Pause is not owned, so a manual pause later is not undone.
    bus.fail_pause.add(A)
    mark = len(bus.calls)
    p.set_recording(True)
    time.sleep(0.2); wait_idle(p)
    bus.states[A] = "Paused"  # you paused it yourself during the recording
    p.set_recording(False)
    time.sleep(0.2); wait_idle(p)
    later = bus.calls[mark:]
    check("failed Pause is not owned (manual pause kept)", bus.states[A] == "Paused" and (A, "Play") not in later and p.owned == [], later)
    bus.fail_pause.clear()

    # R5: Play fails once -> ownership kept and retried, then succeeds.
    bus.states[A] = "Playing"
    p.set_recording(True)
    time.sleep(0.2); wait_idle(p)
    bus.fail_play.add(A)
    p.set_recording(False)
    time.sleep(0.3)
    check("failed Play keeps ownership", p.owned == [A], p.owned)
    bus.fail_play.clear()
    time.sleep(1.4); wait_idle(p)
    check("retried Play resumes it", bus.states[A] == "Playing" and p.owned == [], (bus.states, p.owned))

    # R6: stop+start quickly must not leave media playing during the new recording.
    bus.states[A] = "Playing"
    p.set_recording(True)
    p.set_recording(False)
    p.set_recording(True)
    time.sleep(0.4); wait_idle(p)
    check("quick stop+start ends paused", bus.states[A] == "Paused", bus.states)

    # (the previous check ended mid-recording)
    p.set_recording(False)
    time.sleep(0.4); wait_idle(p)

    # B2: resume interrupted by a new recording: what was resumed is not "owned
    # and paused" any more, and the new scan pauses it again.
    bus.states = {A: "Playing", B: "Playing"}
    p.set_recording(True)
    time.sleep(0.3); wait_idle(p)
    real_call = p._call

    def call_then_start(player, method):
        ok = real_call(player, method)
        if method == "Play" and player == A:
            p.set_recording(True)  # you start talking again right after A resumed
        return ok

    p._call = call_then_start
    p.set_recording(False)
    time.sleep(0.5); wait_idle(p)
    p._call = real_call
    check("interrupted resume: new recording pauses A again", bus.states[A] == "Paused" and A in p.owned, (bus.states, p.owned))
    p.set_recording(False)
    time.sleep(0.4); wait_idle(p)

    # B2: you resumed it yourself, then stop+start happen before the worker runs.
    bus.states = {A: "Playing", B: "Paused"}
    p.set_recording(True)
    time.sleep(0.3); wait_idle(p)
    bus.states[A] = "Playing"  # you pressed play
    p.set_recording(False)
    p.set_recording(True)
    time.sleep(0.4); wait_idle(p)
    check("owned but playing again gets paused by the next recording", bus.states[A] == "Paused", bus.states)
    p.set_recording(False)
    time.sleep(0.4); wait_idle(p)

    # B3: Play keeps failing past the retry budget -> still owned; restore at shutdown resumes it.
    bus.states = {A: "Playing", B: "Paused"}
    p.set_recording(True)
    time.sleep(0.3); wait_idle(p)
    bus.fail_play.add(A)
    p.set_recording(False)
    time.sleep(0.2 + p.MAX_RESUME_ATTEMPTS * p.RETRY_SECONDS + 0.5); wait_idle(p)
    check("after retries run out, A is still owned", A in p.owned and bus.states[A] == "Paused", (p.owned, bus.states))
    bus.fail_play.clear()

    # Shutdown restore while still owning a paused player.
    ok = p.restore(2.0)
    check("restore at shutdown resumes it", ok and bus.states[A] == "Playing", bus.states)
    p.set_recording(True)
    time.sleep(0.2)
    check("no new pauses after shutdown", bus.states[A] == "Playing", bus.states)


async def main():
    dictation.event_loop = asyncio.get_running_loop()
    await r3()
    await r4_r8()
    await b5()
    b6()
    await r2()


if __name__ == "__main__":
    asyncio.run(main())
    r10()
    media()
    print(f"\n{'ALL OK' if not failures else 'FAILED: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)
