#!/bin/bash
# <xbar.title>Usage Bar</xbar.title>
# <xbar.version>v0.1</xbar.version>
# <xbar.author>Giuseppe Salvi</xbar.author>
# <xbar.desc>Claude Code + Codex usage at a glance: spend + rate-limit windows.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <swiftbar.hideAbout>false</swiftbar.hideAbout>
# <swiftbar.refreshOnOpen>true</swiftbar.refreshOnOpen>
#
# Reads local logs only (no network) unless you opt into Claude windows.
# Refreshes every 30s (filename: *.30s.sh). Uses system python3 so it works in
# SwiftBar's minimal PATH; usage.py is stdlib-only.

# Resolve this script's real directory even when symlinked into SwiftBar's
# plugin folder (portable; no GNU `readlink -f` needed).
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

exec /usr/bin/python3 "$DIR/usage.py" --swiftbar
