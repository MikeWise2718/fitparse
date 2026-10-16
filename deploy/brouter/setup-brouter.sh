#!/bin/bash
# Install, or bring up to date, the BRouter routing engine behind fitmon's route planner.
# Everything lives in ~/brouter and runs as mike: no sudo, no Homebrew, nothing system-wide.
# Safe to re-run; the monthly com.fitmon.brouter-tiles job runs it to refresh the map tiles.
#   ~/brouter/jre/          a private Java runtime (Temurin 21 JRE)
#   ~/brouter/current/      the BRouter release (jar + shipped profiles)
#   ~/brouter/segments4/    OpenStreetMap routing tiles, 5x5 degrees each
#   ~/brouter/customprofiles/  tuned copies of profiles, if any
set -euo pipefail
VERSION=${BROUTER_VERSION:-1.7.10}
# Langen (50.0 N, 8.7 E) sits on a tile boundary: E5_N45 covers 45-50 N, E5_N50 covers 50-55 N.
TILES=${BROUTER_TILES:-"E5_N45 E5_N50"}
DEST=~/brouter
mkdir -p "$DEST/segments4" "$DEST/customprofiles"
cd "$DEST"

if [ ! -d "brouter-$VERSION" ]; then
    echo "fetching BRouter $VERSION"
    curl -fsSL -o brouter.zip "https://github.com/abrensch/brouter/releases/download/v$VERSION/brouter-$VERSION.zip"
    unzip -q -o brouter.zip && rm brouter.zip
fi
ln -sfn "brouter-$VERSION" current

if [ ! -x jre/bin/java ]; then
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64) os=mac; arch=aarch64 ;;
        Darwin-x86_64) os=mac; arch=x64 ;;
        Linux-aarch64) os=linux; arch=aarch64 ;;
        Linux-x86_64) os=linux; arch=x64 ;;
        *) echo "no Java download known for $(uname -sm)" >&2; exit 1 ;;
    esac
    echo "fetching a Java 21 runtime ($os/$arch)"
    rm -rf jre-tmp && mkdir jre-tmp
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/21/ga/$os/$arch/jre/hotspot/normal/eclipse" | tar xz -C jre-tmp
    home=$(dirname "$(dirname "$(find jre-tmp -type f -path '*/bin/java' | head -1)")")
    rm -rf jre && mv "$home" jre && rm -rf jre-tmp
fi

changed=0
for t in $TILES; do
    f="segments4/$t.rd5"
    # -z: only download when the server's copy is newer than ours.
    if [ -f "$f" ]; then
        curl -fsSL -z "$f" -o "$f.part" "https://brouter.de/brouter/segments4/$t.rd5" || true
    else
        curl -fsSL -o "$f.part" "https://brouter.de/brouter/segments4/$t.rd5"
    fi
    if [ -s "$f.part" ]; then mv "$f.part" "$f"; changed=1; echo "updated $f"; fi
    rm -f "$f.part"
done

# New tiles are only read by a fresh process. launchd (KeepAlive) starts it again at once.
if [ "$changed" = 1 ] && pgrep -f btools.server.RouteServer >/dev/null; then
    echo "restarting BRouter for the new tiles"
    pkill -f btools.server.RouteServer || true
fi
echo "BRouter ready in $DEST ($(ls segments4 | tr '\n' ' '))"
