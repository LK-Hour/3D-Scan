#!/usr/bin/env bash
# Compile the pure-Kotlin core and run its checks on a plain JVM (no Android SDK needed).
# Needs kotlinc on PATH (or KOTLINC=/path/to/kotlinc). Optional online part:
#   core-tests/run.sh http://127.0.0.1:8765 <token> /path/to/synthetic-session
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
KOTLINC="${KOTLINC:-kotlinc}"
out="${TMPDIR:-/tmp}/scan3d-core-smoke.jar"
"$KOTLINC" -nowarn "$here"/../app/src/main/java/com/scan3d/capture/core/*.kt "$here"/CoreSmoke.kt -include-runtime -d "$out"
java -jar "$out" "$@"
