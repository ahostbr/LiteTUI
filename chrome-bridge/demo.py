"""Smoke test against a REAL browser. Load the extension first (see README)."""

from bridge import Chrome

with Chrome() as c:
    print("connected:", c.ping())

    print("\nopen tabs:")
    for t in c.tabs()[:10]:
        print("  {:>6}  {}".format(t["id"], (t["title"] or "")[:70]))

    info = c.navigate("https://example.com", new_tab=True)
    print("\nnavigated ->", info)

    page = c.content()
    print("title:", page["title"])
    print("text:", page["text"][:200].replace("\n", " "))

    print("\nclicking the first link...")
    print(" ", c.click("a"))
    print("now at:", c.content()["url"])
