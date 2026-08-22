"""Generate the styled HTML renders FROM the markdown sub-plans (v9 rule:
markdown is the source of truth; HTML is the presentation and cannot drift
because it is always regenerated from the .md). Run: python render_html.py"""
from pathlib import Path

from markdown_it import MarkdownIt

HERE = Path(__file__).parent

STYLE = """
:root{--bg:#09090b;--card:#111113;--secondary:#18181b;--border:#27272a;--text:#fafafa;
--textSec:#a1a1aa;--muted:#71717a;--primary:#facc15;--mono:ui-monospace,'Cascadia Code',monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:ui-sans-serif,system-ui,'Segoe UI',sans-serif;line-height:1.55;font-size:14px}
main{max-width:960px;margin:0 auto;padding:28px 24px 60px}
h1{font-size:23px;margin:0 0 6px}h2{font-size:15px;margin:26px 0 8px;color:var(--primary);
text-transform:uppercase;letter-spacing:.07em}h3{font-size:14px;margin:14px 0 6px}
p,li,td{color:var(--textSec)}code{font-family:var(--mono);font-size:12px;background:var(--secondary);
padding:1px 4px;border-radius:4px}pre{background:var(--secondary);border:1px solid var(--border);
border-radius:6px;padding:10px;overflow-x:auto}pre code{background:none;padding:0}
a{color:var(--primary)}blockquote{border-left:3px solid var(--primary);margin:12px 0;
padding:4px 14px;background:var(--card);border-radius:0 8px 8px 0}
table{width:100%;border-collapse:collapse;font-size:12px;margin:6px 0}
th,td{border:1px solid var(--border);padding:5px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;background:var(--secondary)}
ul{padding-left:22px}hr{border:none;border-top:1px solid var(--border)}
"""

md = MarkdownIt("commonmark", {"html": False}).enable("table")

for src in sorted(HERE.glob("*.md")):
    body = md.render(src.read_text(encoding="utf-8"))
    # keep intra-plan links working in the browser: .md hrefs -> .html
    body = body.replace('.md"', '.html"')
    title = src.stem.replace("-", " ").title()
    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>Plan: {title}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    out = src.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    print(f"rendered {out.name} ({len(html):,} bytes)")
