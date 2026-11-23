#!/bin/sh
# Baut PhotoFetch.app (lädt Originale über PhotoKit aus iCloud) aus fetch-src/. Braucht die Xcode Command Line Tools.
# Aufruf: ./build.sh [zielordner]   (Standard: neben diesem Skript)
# Die App wird nur lokal signiert ("ad hoc"). Beim ersten Start fragt macOS nach dem Zugriff auf Fotos; die
# Freigabe hängt an der Signatur – nach jedem Neubau fragt macOS deshalb erneut.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
app="${1:-$here}/PhotoFetch.app"
mkdir -p "$app/Contents/MacOS"
cp "$here/fetch-src/Info.plist" "$app/Contents/Info.plist"
swiftc -O "$here/fetch-src/main.swift" -o "$app/Contents/MacOS/PhotoFetch"
codesign -s - --force "$app"
echo "gebaut: $app"
