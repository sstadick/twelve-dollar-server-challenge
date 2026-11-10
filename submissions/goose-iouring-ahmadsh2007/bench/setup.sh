#!/usr/bin/env bash
# Local load generator only; not part of the server build. Needs make, gcc and Perl.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$here/.build/wrk"
curl -fsSL --retry 3 https://codeload.github.com/wg/wrk/tar.gz/refs/tags/4.2.0 -o "$here/.build/wrk.tar.gz"
printf '%s  %s\n' e255f696bff6e329f5d19091da6b06164b8d59d62cb9e673625bdcd27fe7bdad "$here/.build/wrk.tar.gz" | sha256sum -c -
tar -xzf "$here/.build/wrk.tar.gz" -C "$here/.build/wrk" --strip-components=1
make -C "$here/.build/wrk" -j2
