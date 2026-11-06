# Demo knowledge

Fictional businesses used by `python -m app.cli seed-demo`. Any resemblance to real businesses is
coincidental.

- `restaurant/` → tenant `demo-restaurant`: Nodi Kitchen (নদী কিচেন), a restaurant in Dhaka.
- `shop/` → tenant `demo-shop`: Jamdani Lane, a small online shop for handloom goods.

## Demo video (Saffron & Sage Bistro)

`tenant/` holds a third fictional business, Saffron & Sage Bistro in Austin, TX (tenant
`saffron-sage`), used for the product video. The video is recorded by a script from
`scenes.yaml`; edit that file to change the captions, questions, expected keywords or hold times.

To re-record it (needs Docker, [uv](https://docs.astral.sh/uv/) and ffmpeg):

```bash
docker compose up -d --build                         # API, worker, dashboard
docker compose run --rm -v "$PWD/demo/site:/demo-site" migrate \
  python /demo/seed.py --site-config /demo-site/config.local.js   # tenant, documents, widget key
python3 demo/site/serve.py &                         # the restaurant site on http://localhost:8095
RECORD="uv run --no-project --with playwright==1.63.0 --with pyyaml python demo/record_demo.py"
$RECORD --dry-run                                    # play every scene and check the replies
$RECORD                                              # record (each scene retried up to 3 times)
./demo/build_video.sh                                # demo/out: MP4s, thumbnail.png, booking.gif
```

- **Voiceover:** if `demo/voiceover.mp3` exists, `build_video.sh` uses it as the audio track.
- **What's committed:** only `out/thumbnail.png` and `out/booking.gif`. The videos and the
  site's widget key (`site/config.local.js`) are git-ignored.
- **Browser:** Playwright 1.63 uses the Chromium build the dashboard's tests already
  downloaded. Otherwise, run `uvx --from playwright==1.63.0 playwright install chromium` once.

## Demo video (Bangladesh: Nodi Kitchen and Jamdani Lane)

A second video, scripted in `scenes-bd.yaml`, uses the two demo tenants seeded by
`./scripts/demo-setup.sh` (`demo-restaurant` and `demo-shop`, pages on http://localhost:8080).
It shows replies in English, Bangla and Banglish, a Banglish booking, a party request passed to
staff, an order lookup (order JL-10232) and a catalogue question, then the dashboard.

```bash
docker compose up -d --build && ./scripts/demo-setup.sh   # stack, widget, demo tenants and pages
REC="uv run --project backend --with playwright==1.63.0 python demo/record_demo.py --scenes demo/scenes-bd.yaml --out demo/out/bd"
$REC --dry-run                                  # every reply checked, with its detected language
$REC                                            # record (each scene retried up to 3 times)
./demo/build_video.sh --out demo/out/bd --name bd   # demo_bd_1080p.mp4, demo_bd_720p.mp4, thumbnail_bd.png, booking_bd.gif
```

What `scenes-bd.yaml` adds to the scene format:
- **`tenant` and `site_url` per scene.** An admin key is created and revoked for each tenant whose
  dashboard is shown.
- **`language` per message** (`english`, `bengali` or `banglish`). The reply must be in that
  language, judged by the app's own `reply_language` rule, which is why this script runs with the
  backend's environment (`--project backend`).
- **`translation`:** a smaller second caption line for viewers who don't read Bangla.
- **`video.widget_zoom`:** enlarges only the chat widget, so its text is about 28 px in the
  1080p video.

A full recording first deletes the old conversations, bookings and leads of every tenant in its
script, so the dashboard shows only what it recorded. Documents, orders, settings and keys are
kept.
