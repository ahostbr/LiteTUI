"""A contained, device-free monitor child: it never imports a Chrome bridge."""
import os
import sys
import time
from pathlib import Path

from litetui import native_monitor_worker as worker
from litetui.monitor.probe import FakeProbe

folder = Path(sys.argv[1])
(folder / 'child.pid').write_text(str(os.getpid()))
if (folder / 'wait').exists():
    while True:
        time.sleep(0.05)

class FakeScreenshots(FakeProbe):
    def __init__(self, *, shot_dir):
        super().__init__()
        self.shot_dir = shot_dir
    def probe(self, target):
        result = super().probe(target)
        if target.screenshot:
            self.shot_dir.mkdir(parents=True, exist_ok=True)
            path = self.shot_dir / (target.name + '.png')
            path.write_bytes(b'controlled screenshot bytes')
            result.screenshot_path = str(path)
        return result

worker.ChromeProbe = FakeScreenshots
worker.run(folder)
