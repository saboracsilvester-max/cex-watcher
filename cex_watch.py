"""CeX RTX 3090/4090/5090 stock watcher -> ntfy push notifications.

Graphics cards: alert when buyable online (shipped) or in a nearby store.
Pre-built PCs:  alert only when in stock at a store near AL10 (PCs aren't delivered).

Python stdlib only. Usage:
    NTFY_TOPIC=my-secret-topic python cex_watch.py            # normal run
    python cex_watch.py --dry-run                              # print, don't send
"""
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

# ---------------------------------------------------------------- config
MODELS = ["3090", "4090", "5090"]
MODEL_RE = re.compile(r"RTX\s?(3090|4090|5090)(?!\d)", re.I)

GPU_CATEGORY = "PCI-Express Graphics Cards"
PC_CATEGORY = "Desktops - Windows"

# Skip laptop-GPU eGPU boxes that sit in the graphics card category.
EXCLUDE_RE = re.compile(r"XG Mobile|eGPU", re.I)

# Stores count as "near" within MAX_MILES (straight line) of AL10.
HOME = (51.763, -0.225)  # AL10
MAX_MILES = 40
# Always near, even though CeX's store list has no coordinates for them.
# Matched by prefix, so "Hatfield" also matches "Hatfield - Temporarily Closed".
ALWAYS_NEAR = ["Hatfield", "Welwyn Garden City"]

SEARCH_URL = "https://search.webuy.io/1/indexes/*/queries"
PRODUCT_URL = "https://uk.webuy.com/product-detail/?id={}"
DETAIL_URL = "https://wss2.cex.uk.webuy.io/v3/boxes/{}/detail"
NEAREST_URL = ("https://wss2.cex.uk.webuy.io/v3/boxes/{}/neareststores"
               f"?latitude={HOME[0]}&longitude={HOME[1]}")
STORES_URL = "https://wss2.cex.uk.webuy.io/v3/stores"
USER_AGENT = "personal-cex-stock-watcher/1.0 (one request per model, every few minutes)"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "seen.json")
LOG_FILE = os.path.join(HERE, "watch.log")
TOPIC_FILE = os.path.join(HERE, "ntfy_topic.txt")  # local runs; GitHub uses the secret


def _topic():
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"].strip()
    try:
        with open(TOPIC_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


NTFY_TOPIC = _topic()


# ---------------------------------------------------------------- cex
def search(model):
    params = urllib.parse.urlencode({
        "query": model,
        "hitsPerPage": 1000,
        "typoTolerance": "false",
        "facetFilters": json.dumps([
            [f"categoryFriendlyName:{GPU_CATEGORY}", f"categoryFriendlyName:{PC_CATEGORY}"],
            ["availability:In Stock In Store", "availability:In Stock Online"],
        ]),
    })
    body = json.dumps({"requests": [{"indexName": "prod_cex_uk", "params": params}]}).encode()
    req = urllib.request.Request(SEARCH_URL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return data["results"][0]["hits"]  # KeyError = unexpected shape -> treated as broken


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)["response"]["data"]


def miles(lat, lon):
    la1, lo1, la2, lo2 = map(math.radians, (HOME[0], HOME[1], lat, lon))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def load_near_stores():
    names = set()
    for s in get_json(STORES_URL)["stores"]:
        if s.get("latitude") and s.get("longitude"):
            if miles(float(s["latitude"]), float(s["longitude"])) <= MAX_MILES:
                names.add(s["storeName"].lower())
    return names


NEAR = set()  # filled at the start of each run


def is_near(store, distance=None):
    s = store.lower()
    if any(s.startswith(n.lower()) for n in ALWAYS_NEAR):
        return True
    if distance is not None:
        return float(distance) <= MAX_MILES
    return s in NEAR


def live_where(box_id, kind):
    """Re-check stock against CeX's live product record.

    The search index lags real stock by up to ~an hour, so a card can show
    as in stock there after it has sold. Returns None if the check fails.
    """
    try:
        where = []
        if kind == "GPU":
            d = get_json(DETAIL_URL.format(box_id))["boxDetails"][0]
            if (d.get("ecomQuantityOnHand") or 0) > 0 and d.get("boxWebSaleAllowed"):
                where.append("online (delivery)")
        stores = get_json(NEAREST_URL.format(box_id))["nearestStores"] or []
        where += [f"{s['storeName']} ({float(s['distance']):.0f} mi)" for s in stores
                  if int(s.get("quantityOnHand") or 0) > 0
                  and is_near(s["storeName"], s.get("distance"))]
        return where
    except Exception as e:
        print(f"live check failed for {box_id}: {e!r}", file=sys.stderr)
        return None


def matches():
    """Return {boxId: {name, price, kind, where[]}} for items we care about."""
    NEAR.clear()
    NEAR.update(load_near_stores())
    found = {}
    for i, model in enumerate(MODELS):
        if i:
            time.sleep(1.5)
        for h in search(model):
            name = h.get("boxName", "")
            if not MODEL_RE.search(name) or EXCLUDE_RE.search(name):
                continue
            cat = h.get("categoryFriendlyName")
            stores = set(h.get("stores") or []) | set(h.get("collectionStores") or [])
            near = sorted(s for s in stores if is_near(s))
            where = []
            if cat == GPU_CATEGORY:
                kind = "GPU"
                if h.get("inStockOnline") == 1:
                    where.append("online (delivery)")
                where += near
            elif cat == PC_CATEGORY:
                kind = "PC"
                where += near
            else:
                continue
            if where:
                found[h["boxId"]] = {"name": name, "price": h.get("sellPrice"),
                                     "kind": kind, "where": where}

    # Confirm every candidate against live stock; drop ones that have sold.
    for box_id, item in list(found.items()):
        time.sleep(0.5)
        live = live_where(box_id, item["kind"])
        if live is None:
            continue  # live check failed: fall back to the search data
        if live:
            item["where"] = live
        else:
            del found[box_id]
    return found


# ---------------------------------------------------------------- ntfy
def notify(title, message, click=None, priority=4, tags=None, dry_run=False):
    if dry_run or not NTFY_TOPIC:
        print(f"[notify] {title}\n    {message}\n    {click or ''}")
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "broken": False, "fails": 0, "last_day": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- main
def main():
    dry_run = "--dry-run" in sys.argv
    state = load_state()

    try:
        found = matches()
    except Exception as e:  # network error, 403, schema change...
        print(f"CeX check failed: {e!r}", file=sys.stderr)
        state["fails"] = state.get("fails", 0) + 1
        # Only alert after 3 failures in a row, and only once per outage.
        if state["fails"] >= 3 and not state.get("broken"):
            notify("CeX watcher broken", f"Check failed: {e!r}"[:300],
                   priority=3, tags=["warning"], dry_run=dry_run)
            state["broken"] = True
        save_state(state)
        return 0  # exit 0 so GitHub doesn't email a failure every 5 minutes

    if state.get("broken"):
        notify("CeX watcher working again", "Checks are succeeding again.",
               priority=2, tags=["white_check_mark"], dry_run=dry_run)
        state["broken"] = False
    state["fails"] = 0

    seen = state.get("seen", {})
    for box_id, item in found.items():
        new_where = [w for w in item["where"] if w not in seen.get(box_id, [])]
        if not new_where:
            continue
        emoji = "desktop_computer" if item["kind"] == "PC" else "video_game"
        notify(f"£{item['price']:g} {item['kind']}: {item['name'][:60]}",
               f"{item['name']}\nIn stock: {', '.join(new_where)}",
               click=PRODUCT_URL.format(box_id), priority=5, tags=[emoji],
               dry_run=dry_run)
        time.sleep(1)

    # Items that vanish are forgotten, so they alert again if they come back.
    state["seen"] = {k: v["where"] for k, v in found.items()}
    # Changes once a day -> one commit/day keeps GitHub's 60-day cron timer alive.
    state["last_day"] = date.today().isoformat()
    save_state(state)
    print(f"OK: {len(found)} matching item(s) in stock")
    return 0


if __name__ == "__main__":
    if "--log" in sys.argv:  # windowless runs (Task Scheduler): append output to watch.log
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        sys.stdout = sys.stderr = open(LOG_FILE, "a", encoding="utf-8")
        print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}")
    sys.exit(main())
