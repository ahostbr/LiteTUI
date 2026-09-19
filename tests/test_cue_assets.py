"""The recording cue wavs must ship in the package (package-data regression
guard). If these drop from the wheel, the mic start/stop blip goes silently
missing on installed copies — same class of bug as T135's schemas."""
import importlib.resources as ir
import wave

def test_cue_assets_present_and_valid():
    for name in ("rec-start.wav", "rec-stop.wav"):
        res = ir.files("litetui").joinpath("assets", name)
        with ir.as_file(res) as p:
            assert p.exists(), f"{name} missing from package"
            with wave.open(str(p), "rb") as w:
                assert w.getnframes() > 0, f"{name} is empty"
