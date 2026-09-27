"""T1048: LiteTUI at narrow width (the user, 2026-09-26: "thers a large gap to
the right of the messages not being filled and also currently the lock icon is
doing something weird cutton off the bottom input box").

The pictures are .scratch/t1048/capture.py; the arms here fail if a defect
comes back.
"""
import pytest


async def _real_app(tmp_path, monkeypatch):
    from litetui.app import LiteTUI
    from litetui import plugins, hook_host
    monkeypatch.setenv('LITETUI_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('LITETUI_DISABLE_UPDATE_CHECK', '1')
    monkeypatch.setenv('LITETUI_HOOKS', 'off')
    monkeypatch.setattr(LiteTUI, '_connect', lambda self: None)
    monkeypatch.setattr(LiteTUI, '_mcp_connect', lambda self: None)
    monkeypatch.setattr(plugins, 'activate_plugins', lambda *args: None)
    monkeypatch.setattr(hook_host, 'queue_lifecycle', lambda *args: None)
    return LiteTUI()


@pytest.mark.asyncio
async def test_cards_fill_the_log_at_narrow_width_and_keep_their_indent_when_wide(tmp_path, monkeypatch):
    from litetui.widgets import UserMessage, AssistantMessage, ToolMessage
    app = await _real_app(tmp_path, monkeypatch)
    async with app.run_test(size=(45, 40)) as pilot:
        await pilot.pause(0.2)
        log = app.query_one('#chat-log')
        cards = [UserMessage('why is there a gap on the right'), ToolMessage('read'), AssistantMessage()]
        for card in cards:
            await log.mount(card)
        cards[2].set_answer('An answer long enough to wrap at forty-five columns, twice over at least.')
        await pilot.pause(0.2)

        area = log.scrollable_content_region
        for card in cards:
            assert (card.region.x, card.region.right) == (area.x, area.right), (
                f'{type(card).__name__} spans {card.region.x}..{card.region.right}, '
                f'the log offers {area.x}..{area.right} at 45 columns')
        # Only the SIDES changed: a Textual longhand (`margin-left`) zeroes its
        # rule's other edges, which took the cards' top margin and padding.
        for card in (cards[0], cards[2]):
            assert (card.styles.margin.top, card.styles.padding.top) == (1, 1)
        # The log's edges are the prompt box's edges.
        box = app.query_one('#message-input').region
        assert (area.x, area.right) == (box.x, box.right)
        # A sibling's margin must not narrow them: Textual resolves `1fr`
        # widths against the LARGEST sibling margin, and #skill-ac has 2.
        app.query_one('#skill-ac').display = True
        await pilot.pause(0.1)
        open_area = log.scrollable_content_region
        open_box = app.query_one('#message-input').region
        assert (open_area.x, open_area.right, open_box.x, open_box.right) == (area.x, area.right, box.x, box.right)
        app.query_one('#skill-ac').display = False

        # Wide is unchanged: the bubbles keep their chat indent.
        await pilot.resize_terminal(140, 40)
        await pilot.pause(0.2)
        area = log.scrollable_content_region
        assert cards[0].region.x > area.x + 4          # user indented from the left
        assert cards[2].region.right < area.right - 4  # assistant indented from the right

