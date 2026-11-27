# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Exercise the production PTT class without audio, API calls or real keys."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

source = ast.parse(Path(__file__).with_name('dictation.py').read_text())
node = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'RightShiftPTT')

class Device:
    def __init__(self, name):
        self.name = name
        self.queue = asyncio.Queue()
        self.closed = False

    def capabilities(self):
        return {1: [54]}

    def close(self):
        self.closed = True

    async def async_read_loop(self):
        while True:
            value = await self.queue.get()
            if value is None:
                raise OSError('removed')
            yield NS(type=1, code=54, value=value)

class App:
    is_recording = False
    starts = 0
    stops = 0

    async def start_recording(self):
        self.starts += 1
        self.is_recording = True

    async def stop_recording(self):
        self.stops += 1
        self.is_recording = False

class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.devices = {}
        namespace = dict(asyncio=asyncio, DictationApp=App,
                         evdev=NS(list_devices=lambda: list(self.devices),
                                  InputDevice=lambda path: self.devices[path]),
                         ecodes=NS(EV_KEY=1, KEY_RIGHTSHIFT=54))
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'production-ptt', 'exec'), namespace)
        self.app = App()
        self.ptt = namespace['RightShiftPTT'](self.app)

    async def asyncTearDown(self):
        self.ptt.stop()
        await asyncio.sleep(0.01)

    async def event(self, dev, value):
        await dev.queue.put(value)
        await asyncio.sleep(0.02)

    async def test_device_added_after_start_is_watched(self):
        self.assertEqual(self.ptt.start(), 0)
        dev = self.devices['output'] = Device('DRG SOCD virtual keyboard')
        await asyncio.sleep(0.6)
        await self.event(dev, 1)
        await self.event(dev, 0)
        self.assertEqual((self.app.starts, self.app.stops), (1, 1))

    async def test_recreated_device_at_same_path(self):
        old = self.devices['output'] = Device('old output')
        self.ptt.start()
        await self.event(old, 1)
        del self.devices['output']
        await self.event(old, None)
        self.assertTrue(old.closed)
        self.assertEqual(self.app.stops, 1)
        new = self.devices['output'] = Device('new output')
        self.ptt._scan_devices()
        await self.event(new, 1)
        await self.event(new, 0)
        self.assertEqual((self.app.starts, self.app.stops), (2, 2))

    async def test_two_sources_do_not_double_start_or_prematurely_stop(self):
        a = self.devices['a'] = Device('a')
        b = self.devices['b'] = Device('b')
        self.ptt.start()
        await self.event(a, 1)
        await self.event(b, 1)
        await self.event(a, 0)
        self.assertEqual((self.app.starts, self.app.stops), (1, 0))
        await self.event(b, 2)
        await self.event(b, 0)
        self.assertEqual((self.app.starts, self.app.stops), (1, 1))

    async def test_shutdown_closes_devices(self):
        a = self.devices['a'] = Device('a')
        self.ptt.start()
        await asyncio.sleep(0.01)
        self.ptt.stop()
        await asyncio.sleep(0.01)
        self.assertTrue(a.closed)
        self.assertFalse(self.ptt.watch_tasks)

if __name__ == '__main__':
    unittest.main(verbosity=2)
