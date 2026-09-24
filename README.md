# CeX GPU / PC watcher

Pings your Android phone (via ntfy) when CeX UK lists an **RTX 3090** (incl. 3090 Ti):

- **Graphics cards:** in stock online (delivered), or in a CeX store within 40 miles of AL10.
- **Pre-built PCs** with one of those cards: only when in stock at a CeX store within 40 miles of AL10 (CeX doesn't deliver PCs).

It runs free on GitHub Actions every ~5 minutes (GitHub can delay runs 5–30 min at busy times).
Each item pings once. If it sells and comes back, or turns up at another nearby store, you get pinged again.
If CeX blocks or changes its API, you get one "CeX watcher broken" ping.

## Setup (~10 minutes)

### 1. Phone
1. Install **ntfy** from Google Play.
2. Tap **+** and subscribe to a topic name nobody could guess, e.g. `cex-gpu-` plus random letters (`cex-gpu-k7q2xv9m4p`).
   Anyone who knows the name can read your alerts, so treat it like a password.
3. Optional but recommended: in ntfy settings turn on **Instant delivery**, and in Android set ntfy's battery usage to **Unrestricted**.

### 2. GitHub
1. Make a free account at github.com if you don't have one.
2. **New repository**, any name (e.g. `cex-watcher`), **Public** (public = unlimited free Actions minutes).
3. Upload this folder's contents: `cex_watch.py`, `README.md` and the `.github/workflows/watch.yml` file.
   The easiest way is **Add file → Upload files** and drag the whole folder contents in, including the `.github` folder.
   Check that `.github/workflows/watch.yml` exists in the repo afterwards.
4. **Settings → Secrets and variables → Actions → New repository secret**
   Name: `NTFY_TOPIC`, Value: your topic name from step 1.
5. **Actions** tab → enable workflows if asked → **CeX stock watch** → **Run workflow**.
   Within a minute your phone should ping for everything currently in stock (today: a 3090 and a 4090 card online).

After that it runs by itself. The `seen.json` file it commits is its memory of what it has already sent you.

## Running on this PC instead (currently set up)

A Windows Task Scheduler job, **CeX GPU Watcher**, runs `pythonw cex_watch.py --log` every 3 minutes while the PC is on.
The topic is read from `ntfy_topic.txt` and output goes to `watch.log`. Both are git-ignored, so don't upload them.

- Pause: `schtasks /change /tn "CeX GPU Watcher" /disable`
- Resume: `schtasks /change /tn "CeX GPU Watcher" /enable`
- Remove: `schtasks /delete /tn "CeX GPU Watcher" /f`

To check it's alive: every hour your topic gets a silent "Watcher running" message listing what's in stock.

If you move to GitHub Actions, remove the local task so you don't get double pings.

## Tweaking (edit `cex_watch.py` on GitHub)

- **Radius:** change `MAX_MILES` (straight-line miles from AL10). `ALWAYS_NEAR` adds stores by name.
- **Models:** `MODELS` / `MODEL_RE`, e.g. add `4090` back with `MODELS = ["3090", "4090"]` and `RTX\s?(3090|4090)(?!\d)`.
- **eGPUs:** laptop external-GPU boxes (Asus XG Mobile) are skipped by `EXCLUDE_RE`.
- To test locally: `python cex_watch.py --dry-run` prints alerts instead of sending.

## Caveats

- This uses CeX's website's own search service, not an official API. It could change or be blocked; if so you'll get the "broken" ping.
- CeX's search index lags real stock by up to ~an hour. Every match is re-checked against CeX's live product record before pinging, so you won't get pinged for items that already sold, but a brand-new listing can still reach you up to an hour late. Fast sellers can go in that window.
- CeX Hatfield currently shows as "Temporarily Closed".
