"""A reviewed composer image queued behind a running native turn: exact bytes, durable identity, no replay."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image
from test_rust_background_input import command
from test_rust_claude_queue import invoke
from test_rust_claude_queue import running as claude_running
from test_rust_native_midturn import app, codex, switch_app

from litetui import background_input as work
from litetui import native_history, native_image, native_midturn

__all__ = ["app", "claude_running", "codex", "switch_app"]
asynchronous = pytest.mark.asyncio


def png(directory, name="chosen.png"):
    path = directory / name
    Image.new("RGB", (96, 48), (12, 99, 180)).save(path)
    return path


async def attach(app, path):
    review = await invoke(
        app,
        command(
            app,
            type=native_image.OPERATION,
            id=str(uuid.uuid4()),
            work_instance=work.bridge(app).instance,
            path=str(path),
            clear=None,
        ),
    )
    assert review["kind"] == "image_review", review
    return review


def request(cmd, operation, image, **extra):
    return cmd(operation, image=image, **extra)


@asynchronous
async def test_claude_queue_keeps_exact_image_as_a_conversation_file_and_a_durable_identity(
    claude_running, tmp_path
):
    app, cmd, calls = claude_running
    app.tools_enabled = True
    bridge = work.bridge(app)
    shown = await attach(app, png(tmp_path))
    assert shown["delivery"] == "file"
    review = await invoke(app, request(cmd, native_midturn.SEND_REVIEW, shown))
    assert review["kind"] == "midturn_review", review
    identity = {"name": "chosen.png", "png_bytes": shown["png_bytes"], "png_sha256": shown["png_sha256"]}
    payload = {"content": "Exact queued draft 雪", "text": "Exact queued draft 雪", "source": "rpc",
               "tool_profile": "strict", "image": identity}
    assert review["image"] == shown and review["payload_sha256"] == work.digest(work.encoded(payload))
    # Review is not consumption: the composer still owns the reviewed image.
    assert bridge.composer_image[0] == shown
    apply = request(cmd, native_midturn.SEND_APPLY, shown, review=review["review"], native=review["native"])
    result = await invoke(app, apply)
    assert result["kind"] == "midturn_queued", result
    stored = app.convo_dir / "images" / f"queued-{shown['png_sha256']}.png"
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == shown["png_sha256"]
    item = app._pending_input[0]
    native = item["_claude_entry"]
    assert str(stored) in native["content"] and native["content"].startswith("Exact queued draft 雪")
    assert base64.b64encode(stored.read_bytes()).decode() not in native["content"]
    entry = bridge.inputs[result["input"]["input_id"]]
    assert entry.payload == payload and item["content"] == "Exact queued draft 雪" and item["image"] == identity
    assert bridge.composer_image is None and bridge.clipboard_candidate is None
    assert calls == ["first controlled input"]
    document = work.strict_json(bridge.raw)
    work.validate_document(document)
    assert len(bridge.raw) < 65536 and shown["png_sha256"] in bridge.raw.decode()
    # A replay of the same review cannot queue the image twice.
    assert isinstance(await invoke(app, apply), ValueError)
    assert len(app._pending_input) == 1
    # A restart sees the identity as held evidence, never a replayable delivery.
    recovered = work.Bridge(app)
    assert recovered.recovered[0]["state"] == "held" and not recovered.inputs[result["input"]["input_id"]].admission
    assert recovered.inputs[result["input"]["input_id"]].payload["image"] == identity
    # The delivery content is rebuilt from verified bytes only.
    assert native_image.provider_payload(app, entry.payload)["content"] == native["content"]
    stored.write_bytes(stored.read_bytes()[:-1] + b"\x00")
    with pytest.raises(ValueError, match="differ|changed"):
        native_image.provider_payload(app, entry.payload)
    stored.unlink()
    with pytest.raises(ValueError, match="missing"):
        native_image.provider_payload(app, entry.payload)
    forged = work.strict_json(bridge.raw)
    forged["inputs"][0]["payload"]["image"]["name"] = "other.png"
    with pytest.raises(ValueError):
        work.validate_document(forged)


@asynchronous
async def test_claude_image_without_tools_or_after_the_image_changed_leaves_the_composer_image_and_queue_untouched(
    claude_running, tmp_path
):
    app, cmd, _ = claude_running
    app.tools_enabled = True
    bridge = work.bridge(app)
    shown = await attach(app, png(tmp_path))
    review = await invoke(app, request(cmd, native_midturn.SEND_REVIEW, shown))
    apply = request(cmd, native_midturn.SEND_APPLY, shown, review=review["review"], native=review["native"])
    app.tools_enabled = False
    assert "Enable tools" in str(await invoke(app, apply))
    app.tools_enabled = True
    # The first refusal consumed that review; reviewing again is required either way.
    review = await invoke(app, request(cmd, native_midturn.SEND_REVIEW, shown))
    apply = request(cmd, native_midturn.SEND_APPLY, shown, review=review["review"], native=review["native"])
    other = await attach(app, png(tmp_path, "replacement.png"))
    assert other["token"] != shown["token"]
    assert "changed" in str(await invoke(app, apply))
    assert bridge.composer_image[0] == other
    assert not app._pending_input and not bridge.inputs


@asynchronous
async def test_codex_queue_steers_the_exact_image_with_the_message_and_keeps_only_its_identity_in_work(
    codex, tmp_path
):
    app, cmd, calls, _, _, _ = codex
    bridge = work.bridge(app)
    shown = await attach(app, png(tmp_path))
    assert shown["delivery"] == "inline"
    review = await invoke(app, request(cmd, native_midturn.SEND_REVIEW, shown))
    assert review["kind"] == "midturn_review", review
    result = await invoke(
        app, request(cmd, native_midturn.SEND_APPLY, shown, review=review["review"], native=review["native"])
    )
    assert result["native_delivery"]["state"] == "queued", result
    for _ in range(200):
        entry = next(iter(bridge.inputs.values()))
        if calls and entry.state != "claiming":
            await asyncio.sleep(0.02)
            break
        await asyncio.sleep(0.01)
    assert len(calls) == 1
    sent = calls[0][1]["input"]
    assert [block["type"] for block in sent] == ["image", "text"] and sent[1]["text"] == "Native draft 雪"
    assert hashlib.sha256(base64.b64decode(sent[0]["url"].split(",", 1)[1])).hexdigest() == shown["png_sha256"]
    native = entry.item["_codex_entry"]
    assert native["item"]["content"][0]["image_url"]["url"] == sent[0]["url"]
    assert entry.payload["content"] == "Native draft 雪" and entry.payload["image"]["png_sha256"] == shown["png_sha256"]
    assert entry.admission["mode"] == "in_turn" and bridge.composer_image is None
    appended = [row for row in app.conversation if row.get("work_input_id") == entry.ref["input_id"]]
    assert len(appended) == 1 and appended[0]["content"][0]["image_url"]["url"] == sent[0]["url"]
    work.validate_document(work.strict_json(bridge.raw))
    assert len(bridge.raw) < 65536


@asynchronous
async def test_interrupt_with_an_image_queues_it_first_and_only_asks_the_turn_to_stop(codex, tmp_path):
    app, cmd, calls, _, _, _ = codex
    shown = await attach(app, png(tmp_path))
    first = request(cmd, native_midturn.SEND_REVIEW, shown)
    first["chord"] = "alternate"
    review = await invoke(app, first)
    assert review["behavior"] == "interrupt" and review["image"] == shown, review
    apply = {**first, "id": str(uuid.uuid4()), "type": native_midturn.SEND_APPLY,
             "review": review["review"], "native": review["native"]}
    result = await invoke(app, apply)
    assert result["kind"] == "midturn_queued" and result["producer"]["kind"] == "user_interrupt", result
    item = app._pending_input[0]
    assert app._stop_requested and not calls
    assert item["image"]["png_sha256"] == shown["png_sha256"]
    assert item["_codex_entry"]["item"]["content"][0]["type"] == "image_url"
    assert work.bridge(app).composer_image is None


@asynchronous
async def test_inline_image_that_cannot_fit_the_saved_transcript_is_refused_before_anything_is_queued(
    codex, tmp_path, monkeypatch
):
    app, cmd, calls, _, _, _ = codex
    bridge = work.bridge(app)
    shown = await attach(app, png(tmp_path))
    monkeypatch.setattr(native_history, "MAX_TRANSCRIPT", 1024)
    refused = await invoke(app, request(cmd, native_midturn.SEND_REVIEW, shown))
    assert isinstance(refused, ValueError) and "transcript" in str(refused)
    assert bridge.composer_image[0] == shown and not bridge.inputs and not app._pending_input and not calls


def test_image_identity_is_strict_and_only_user_queue_payloads_may_carry_it():
    good = {"name": "a.png", "png_bytes": 12, "png_sha256": "a" * 64}
    base = {"content": "hello", "text": "hello", "source": "rpc", "tool_profile": "strict"}
    work.validate_payload({**base, "image": good})
    for key, value in [("name", ""), ("name", "bad\nname"), ("png_bytes", 0), ("png_bytes", True),
                       ("png_bytes", 16 * 1024 * 1024 + 1), ("png_sha256", "A" * 64), ("png_sha256", "a" * 63)]:
        with pytest.raises(ValueError):
            work.validate_payload({**base, "image": {**good, key: value}})
    with pytest.raises(ValueError):
        work.validate_payload({**base, "image": {**good, "pixels": "AAAA"}})
    with pytest.raises(ValueError):
        work.validate_payload({**base, "image": {"name": "a.png"}})


def test_stored_bytes_are_content_addressed_exact_and_written_once(tmp_path):
    path = png(tmp_path)
    app = SimpleNamespace(convo_dir=tmp_path / "convo", backend=SimpleNamespace(owns_native_turns=False))
    details, encoded = native_image.read_image(None, str(path))
    review = {**details, "token": "t"}
    first = native_image.store_queued(app, review, encoded)
    stamp = (tmp_path / "convo" / "images" / f"queued-{details['png_sha256']}.png").stat().st_mtime_ns
    assert native_image.store_queued(app, review, encoded) == first
    assert (tmp_path / "convo" / "images" / f"queued-{details['png_sha256']}.png").stat().st_mtime_ns == stamp
    assert not list((tmp_path / "convo" / "images").glob(".queued-*"))
    with pytest.raises(ValueError, match="differ"):
        native_image.store_queued(app, {**review, "png_sha256": "0" * 64}, encoded)
    payload = {"content": "Look", "text": "Look", "source": "rpc", "tool_profile": "strict",
               "image": native_image.queued_descriptor(review)}
    content = native_image.provider_payload(app, payload)["content"]
    assert content[0]["image_url"]["url"] == "data:image/png;base64," + encoded and content[1] == {"type": "text", "text": "Look"}
    assert "image" not in native_image.provider_payload(app, payload)
    assert native_image.provider_payload(app, {k: v for k, v in payload.items() if k != "image"})["content"] == "Look"
