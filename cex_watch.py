"""CeX RTX 3090/4090/5090 stock watcher -> ntfy push notifications.

Graphics cards: alert when buyable online (shipped) or in a nearby store.
Pre-built PCs:  alert only when in stock at a store near AL10 (PCs aren't delivered).

Python stdlib only. Usage:
    NTFY_TOPIC=my-secret-topic python cex_watch.py            # normal run
    python cex_watch.py --dry-run                              # print, don't send
"""
import csv
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
MODELS = ["3090"]
MODEL_RE = re.compile(r"RTX\s?3090(?!\d)", re.I)  # also matches 3090 Ti

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
HISTORY_FILE = os.path.join(HERE, "history.csv")
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
# Silent hourly "still running" message (lowest priority, no sound).
# Updates one notification in place. ntfy.sh's free tier allows 250 messages/day
# per IP, and hitting it would block the real stock alerts too, so status
# messages are capped at HEARTBEAT_DAILY_CAP (every 8 min = 180/day).
HEARTBEAT_MINUTES = 8
HEARTBEAT_DAILY_CAP = 180


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
    """Return (found, national).

    found:    {boxId: {name, price, kind, where[]}} for items we alert on.
    national: {"boxId|place": {...}} every matching item in stock anywhere in
              the UK, per store (plus "online"), for the history log.
    """
    NEAR.clear()
    NEAR.update(load_near_stores())
    found = {}
    national = {}
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
            places = set(h.get("stores") or [])
            if kind == "GPU" and h.get("inStockOnline") == 1:  # PCs can't be delivered
                places.add("online")
            for place in places:
                national[f"{h['boxId']}|{place}"] = {
                    "box_id": h["boxId"], "place": place, "kind": kind,
                    "name": name, "price": h.get("sellPrice")}
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
    return found, national


def log_history(state, national):
    """Append UK-wide appear/disappear events to history.csv.

    Based on CeX's search data, which can lag real stock by up to ~an hour.
    The first run writes 'baseline' rows (already in stock, arrival unknown).
    """
    old = state.get("national")
    rows = []
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if old is None:
        rows = [("baseline", v) for v in national.values()]
    else:
        rows = [("in", v) for k, v in national.items() if k not in old]
        rows += [("out", v) for k, v in old.items() if k not in national]
    if rows:
        new_file = not os.path.exists(HISTORY_FILE)
        with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "event", "kind", "price", "place", "name", "box_id"])
            for event, v in rows:
                w.writerow([stamp, event, v["kind"], v["price"], v["place"], v["name"], v["box_id"]])
    state["national"] = national


# ---------------------------------------------------------------- ntfy
def notify(title, message, click=None, priority=4, tags=None, dry_run=False, topic=None,
           sequence_id=None):
    topic = topic or NTFY_TOPIC
    if dry_run or not topic:
        print(f"[notify] {title}\n    {message}\n    {click or ''}")
        return
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    if sequence_id:  # same id = replace the earlier notification instead of stacking
        payload["sequence_id"] = sequence_id
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
        found, national = matches()
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
    try:
        log_history(state, national)
    except Exception as e:  # never let logging stop the alerts
        print(f"history log failed: {e!r}", file=sys.stderr)

    # Older state files stored just the list of places.
    seen = {k: (v if isinstance(v, dict) else {"where": v})
            for k, v in state.get("seen", {}).items()}
    for box_id, item in found.items():
        new_where = [w for w in item["where"] if w not in seen.get(box_id, {}).get("where", [])]
        if not new_where:
            continue
        emoji = "desktop_computer" if item["kind"] == "PC" else "video_game"
        notify(f"£{item['price']:g} {item['kind']}: {item['name'][:60]}",
               f"{item['name']}\nIn stock: {', '.join(new_where)}",
               click=PRODUCT_URL.format(box_id), priority=5, tags=[emoji],
               dry_run=dry_run)
        time.sleep(1)

    # Quiet ping when something we alerted on sells, so the phone stays current.
    for box_id, old in seen.items():
        if box_id in found or "name" not in old:
            continue
        notify(f"Sold/gone: £{old['price']:g} {old['kind']}: {old['name'][:50]}",
               f"{old['name']}\nNo longer in stock (was: {', '.join(old['where'])})",
               click=PRODUCT_URL.format(box_id), priority=2, tags=["x"],
               dry_run=dry_run)
        time.sleep(1)

    # Items that vanish are forgotten, so they alert again if they come back.
    state["seen"] = found

    today = date.today().isoformat()
    if state.get("heartbeat_day") != today:
        state["heartbeat_day"], state["heartbeats_today"] = today, 0
    if (time.time() - state.get("last_heartbeat", 0) >= HEARTBEAT_MINUTES * 60 - 30
            and state["heartbeats_today"] < HEARTBEAT_DAILY_CAP):
        lines = [f"£{v['price']:g} {v['kind']}: {v['name'][:45]} - {', '.join(v['where'])}"
                 for v in found.values()] or ["Nothing matching in stock right now."]
        try:
            notify(f"Watcher running - checked {time.strftime('%H:%M')}",
                   f"{len(found)} item(s) in stock:\n" + "\n".join(lines),
                   priority=1, tags=["heartbeat"], dry_run=dry_run,
                   sequence_id="watcher-status",
                   )
            state["last_heartbeat"] = time.time()
            state["heartbeats_today"] += 1
        except Exception as e:
            print(f"heartbeat failed: {e!r}", file=sys.stderr)
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
