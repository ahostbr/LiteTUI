"""Registered action lifecycle with fake inventories and effects, never real installers/audio."""

import os
import runpy
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

shared = runpy.run_path(str(Path(__file__).with_name("voice_preferences_runtime.py")))
base = shared["serve"]
root = Path(os.environ["LITETUI_DATA_ROOT"])
real_popen = subprocess.Popen


async def serve(app):
    from litetui import native_voice_actions

    script = root / "private_actions.py"
    script.write_text(
        r"""import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);v=json.loads((p/'parameters.json').read_text(encoding='utf8'));kind=v['action'];cfg=v['preferences']
if v['phase']=='review':
    fallback=kind=='fallback';stt=kind=='stt';exe=sys.executable;prefix=sys.prefix
    packages=[] if fallback else ['faster-whisper'] if stt else ['edge-tts','playsound==1.2.2']
    plan={'version':1,'action':kind,'interpreter':exe,'launcher':exe,'install':not fallback,'argv':[] if fallback else [exe,'-E','-B','-m','pip','install','--prefix',prefix,*packages],'packages':packages,'prefix':prefix,'cache':str(p.parent/'reviewed-cache') if stt else None,'model':cfg['model'] if stt else None,'engine':cfg['engine'] if fallback else None,'voice':cfg['voice'] if fallback else None,'timeout':cfg['timeout'] if fallback else 600 if stt else 240,'phrase':'This is the LiteTUI voice test.' if fallback else None,'fingerprints':{exe:'1'*64},'modules':['faster_whisper'] if stt else ['edge_tts','playsound'] if not fallback or cfg['engine']=='edge' else ['pyttsx3'],'network':not fallback or cfg['engine']=='edge'}
    result={'plan':plan}
else:
    plan=v['plan'];effects=p.parent.parent/'voice-action-effects.json';items=json.loads(effects.read_text()) if effects.exists() else [];items.append(kind);effects.write_text(json.dumps(items))
    result={'result':{'kind':{'fallback':'fallback_finished','edge':'edge_ready','stt':'stt_cache_ready'}[kind],'interpreter':plan['interpreter'],'cache':plan['cache'],'model':plan['model'],'dependencies_verified':True}}
(p/'result.json').write_text(json.dumps(result),encoding='utf8')
""",
        encoding="utf8",
    )

    def command(directory):
        return [sys.executable, "-B", str(script), str(directory)]

    def launch(argv, **kwargs):
        if argv != command(app._native_voice_action.directory):
            raise AssertionError("Unexpected Voice action subprocess")
        return real_popen(argv, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(native_voice_actions, "_command", command))
        stack.enter_context(patch.object(subprocess, "Popen", launch))
        await base(app)


shared["shared"]["history"].serve = serve
if __name__ == "__main__":
    shared["shared"]["history"].main()
