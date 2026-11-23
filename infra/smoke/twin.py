"""A stub realtime consumer (the twin's place): read the collector's realtime point."""

import json
import os
import time
import urllib.request

url = os.environ["AXIOM_REALTIME_URL"].rstrip("/") + "/latest"
seen = 0
while True:
    try:
        v = json.loads(urllib.request.urlopen(url, timeout=3).read())
        if v.get("value") is not None:
            seen += 1
            if seen in (1, 5):
                print(f"twin received value={v['value']} ({seen} reads)", flush=True)
    except OSError as e:
        print(f"twin waiting: {e}", flush=True)
    time.sleep(1)
