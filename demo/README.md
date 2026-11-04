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
python3 demo/site/serve.py &                         # the restaurant site on http://localhost:8090
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
