#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo 'Usage: tts.sh TEXT OUTPUT.wav' >&2
    exit 2
fi

dictionary=${OPEN_JTALK_DIC:-/var/lib/mecab/dic/open-jtalk/naist-jdic}
voice=${OPEN_JTALK_VOICE:-/usr/share/hts-voice/nitech-jp-atr503-m001/nitech_jp_atr503_m001.htsvoice}
work=$(mktemp -d "$(dirname -- "$2")/.open-jtalk.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

printf '%s\n' "$1" | tr '\r\n' '  ' > "$work/input.txt"
open_jtalk -x "$dictionary" -m "$voice" -ow "$work/speech.wav" "$work/input.txt"
ffmpeg -nostdin -hide_banner -loglevel error -y -i "$work/speech.wav" \
    -ar 8000 -ac 1 -c:a pcm_s16le -f wav "$2"
