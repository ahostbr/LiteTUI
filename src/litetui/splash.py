"""A wordmark that fits the chat viewport, including after terminal resizing."""

from rich.text import Text
from textual.widgets import Static


class Splash(Static):
    DEFAULT_CSS = """
    Splash {
        height: auto;
        margin: 0 2;
        color: $text-muted;
        text-align: center;
    }
    """

    def __init__(self, artwork: str) -> None:
        super().__init__()
        lines = artwork.strip("\n").splitlines()
        self.large = [line[2:].rstrip() for line in lines[:-1]]
        self.tagline = lines[-1].strip()

    def render(self) -> Text:
        width = self.content_size.width
        compact = [
            "█   ███ ███ ███ ███ █ █ ███",
            "█    █   █  █    █  █ █  █ ",
            "█    █   █  ██   █  █ █  █ ",
            "█    █   █  █    █  █ █  █ ",
            "███ ███  █  ███  █  ███ ███",
        ]
        rows = self.large if width >= max(map(len, self.large)) else compact
        if width < max(map(len, rows)):
            rows = ["LiteTUI"]
        # Equal row widths keep every letter aligned when Textual centers lines.
        art_width = max(map(len, rows))
        art = "\n".join(row.ljust(art_width) for row in rows)
        return Text("\n" + art + "\n\n" + self.tagline + "\n")

    def on_resize(self) -> None:
        self.refresh(layout=True)
