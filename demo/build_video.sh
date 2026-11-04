#!/usr/bin/env bash
# Build the demo video from the scenes recorded by demo/record_demo.py (demo/out/segments.json).
#   demo/out/demo_1080p.mp4  H.264 CRF 20, veryfast, yuv420p, +faststart
#   demo/out/demo_720p.mp4
#   demo/out/thumbnail.png   a frame from the booking scene
#   demo/out/booking.gif     the end of the booking scene, max 10 s, 720 px wide, under ~5 MB
# If demo/voiceover.mp3 exists it becomes the audio track (padded or cut to the video's length).
set -euo pipefail
cd "$(dirname "$0")"
OUT=out
SEGMENTS="$OUT/segments.json"
[ -f "$SEGMENTS" ] || { echo "No $SEGMENTS: run demo/record_demo.py first." >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg is not installed (brew install ffmpeg)." >&2; exit 1; }

TMP="$OUT/tmp"
rm -rf "$TMP" && mkdir -p "$TMP"

# 1. Trim each scene to its start/end marks and normalise to 30 fps.
python3 - "$SEGMENTS" "$TMP" > "$TMP/plan.sh" <<'PY'
import json, shlex, sys
segments, tmp = json.load(open(sys.argv[1])), sys.argv[2]
for i, s in enumerate(segments):
    out = f"{tmp}/{i:02d}.mp4"
    print(f"ffmpeg -loglevel error -y -ss {s['start']:.2f} -to {s['end']:.2f} -i {shlex.quote(s['file'])} "
          f"-vf fps=30,format=yuv420p -c:v libx264 -crf 16 -preset veryfast -an {out}")
    print(f"echo \"file '{i:02d}.mp4'\" >> {tmp}/list.txt")
PY
bash "$TMP/plan.sh"

# 2. Join, then encode the deliverables.
ffmpeg -loglevel error -y -f concat -safe 0 -i "$TMP/list.txt" -c copy "$TMP/joined.mp4"
AUDIO=()
if [ -f voiceover.mp3 ]; then
  echo "Mixing in voiceover.mp3"
  AUDIO=(-i voiceover.mp3 -map 0:v -map 1:a -af apad -shortest -c:a aac -b:a 160k)
fi
ffmpeg -loglevel error -y -i "$TMP/joined.mp4" ${AUDIO[@]+"${AUDIO[@]}"} -c:v libx264 -crf 20 -preset veryfast \
  -pix_fmt yuv420p -movflags +faststart "$OUT/demo_1080p.mp4"
ffmpeg -loglevel error -y -i "$OUT/demo_1080p.mp4" -vf scale=-2:720 -c:v libx264 -crf 22 \
  -preset veryfast -pix_fmt yuv420p -c:a copy -movflags +faststart "$OUT/demo_720p.mp4"

# 3. Thumbnail and GIF from the booking scene (where it sits in the joined video).
read -r BOOK_START BOOK_END THUMB_AT < <(python3 - "$SEGMENTS" <<'PY'
import json, sys
t = 0.0
segments = json.load(open(sys.argv[1]))
for s in segments:
    length = s["end"] - s["start"]
    if s.get("thumbnail"):
        print(f"{t:.2f} {t + length:.2f} {t + length - 1.5:.2f}")
        break
    t += length
else:
    print("0 10 5")
PY
)
ffmpeg -loglevel error -y -ss "$THUMB_AT" -i "$OUT/demo_1080p.mp4" -frames:v 1 "$OUT/thumbnail.png"

GIF_START=$(python3 -c "print(max($BOOK_START, $BOOK_END - 10))")
for FPS in 12 10 8; do
  ffmpeg -loglevel error -y -ss "$GIF_START" -t 10 -i "$OUT/demo_1080p.mp4" \
    -vf "fps=$FPS,scale=720:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
    "$OUT/booking.gif"
  SIZE=$(stat -f %z "$OUT/booking.gif" 2>/dev/null || stat -c %s "$OUT/booking.gif")
  [ "$SIZE" -le 5000000 ] && break
  echo "booking.gif is $((SIZE / 1024)) KB at ${FPS} fps; trying fewer frames"
done

rm -rf "$TMP"
DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/demo_1080p.mp4")
printf "Built (video length %.1f s):\n" "$DURATION"
ls -lh "$OUT"/demo_1080p.mp4 "$OUT"/demo_720p.mp4 "$OUT"/thumbnail.png "$OUT"/booking.gif | awk '{print "  " $5 "  " $9}'
