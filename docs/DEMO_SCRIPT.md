# Demo walkthrough

A step-by-step tour of both apps on one laptop: two field devices (Officer Rina on :8101, Officer Arjun on :8102) and the NGO office (Impact on :8000).

## The photos

The demo uses real, openly licensed photos from Wikimedia Commons ([PHOTOS.md](PHOTOS.md)): storm damage and clean-up in Salt Lake, Kolkata (2017–2018) and a mangrove plantation in Hingalganj, Sundarbans (2025), with GPS and capture time from the camera. The organisation, its two projects (`saakshi_core/data/projects.json`) and the notes are fictional.

`python scripts/fetch_sample_photos.py` puts them in `demo_photos/commons/`. What each one shows:

| Photo | Shows |
|---|---|
| `saltlake_storm-tarpaulin_1.jpg` | Urgent ("storm damage"), two people: faces blurred on the device; the "before" at Sector V courtyard |
| `saltlake_storm-tarpaulin_2.jpg` | Taken 3 s after the first: linked as a repeat once the first has synced |
| `saltlake_courtyard-washed_1/2.jpg` | The "after" (three months later, same premises, another part of it) |
| `saltlake_shelter_1.jpg` / `_2.jpg` | "Labour shelter collapsed" from device A, "being rebuilt" from device B: urgent, and a conflict between the two reports |
| `hingalganj_north-bank.jpg` | "Breach risk": the search "breach near the river" finds it |
| `hingalganj_*` (7 more) | The mangrove project: the plantation strip, the village ghat |
| `IMG-20250519-WA0007.jpg` | WhatsApp copy: no location, no time; filed under "No location" and marked for review |
| `schoolyard_reposted.jpg` | The Salt Lake courtyard photo submitted for the mangrove project: Impact flags the reuse |

## Setup

1. `python scripts/download_models.py` once: the device's memory card then says "CLIP ViT-B/32 on device".
2. Optional: Cloudinary with the AI Vision add-on in `.env` for tags, captions and change descriptions in Impact.
3. A cluster-mode Qdrant (`docker compose up -d qdrant`, or Qdrant Cloud), then `python scripts/run_demo.py`.
4. `python scripts/seed_demo.py` loads the photos into both devices. With `--for-video` it leaves out `saltlake_storm-tarpaulin_1/2.jpg` and `saltlake_shelter_1.jpg`, so you can drop them in yourself in step 1 below.

To start again from nothing: stop the apps (Ctrl+C), delete the data folder (`data/`, or `SAAKSHI_DATA_DIR` if set) and the Qdrant collection.

## Walkthrough

1. **Capture offline.** Device A: switch to **Offline**. Drop the tarpaulin photo with the note "Storm damage: rooftop tarpaulin torn off" and status *Damaged*, then the collapsed shelter. Each card appears in under a second with its site and its sync decision.
2. **Search.** **Search** "storm damage", try *Recent first* and *Near site*. The query plan shows what Qdrant Edge ran and how long it took.
3. **Ask.** "What is the latest at the plantation strip?" Every line cites the photos it used; click a citation.
4. **Decide what leaves.** **Sync** tab: each item has its reason (blurred, repeat linked, urgent first). Switch to **2G** and press **Sync now**: urgent photos go, routine photos wait, vectors go for all.
5. **Regional sync.** Switch to **Online**. On device B, **How it works** shows its region mirrors; the Sync card shows the size of the last pull. Device B finds A's photos with the network off.
6. **Conflict.** **Sites** tab: A says the shelter needs attention, B says it is being rebuilt, 43 s apart. Choose one; the decision syncs to every device.
7. **Impact.** Overview: the school-yard photo is flagged as reused from another project; the WhatsApp copy needs review. Open *Storm Clean-up, Salt Lake* → **Before / after**: the composite, the AI Vision description of the change, **Make campaign images** (faces blurred), **Provenance** and the **Donor report**.
8. **Verdict back.** On device A the cards carry "HQ: verified" (or flagged), and *HQ-verified only* filters search by it, offline.
9. **Benchmarks** tab: the measured numbers, and a live device-vs-server test.

## Offline venue

The field side needs no network. Impact needs Cloudinary and Qdrant Cloud, so bring a phone hotspot for step 7.
