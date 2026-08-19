import time
from bridge import Chrome

with Chrome() as c:
    print(c.click("#description-inline-expander #expand"))
    time.sleep(1.5)
    t = c.text("#description-inner #expanded")
    print("LEN:", len(t))
    print(t[:4000])
