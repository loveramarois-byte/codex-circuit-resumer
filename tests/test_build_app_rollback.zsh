#!/bin/zsh
set -eu

PROJECT_DIR="${0:A:h:h}"
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/codex-build-rollback.XXXXXX")
trap 'rm -rf "$TEST_ROOT"' EXIT

DESTINATION="$TEST_ROOT/Test.app"
BACKUP_DIR="$TEST_ROOT/.codex-circuit-resumer-backups"
FAKE_BIN="$TEST_ROOT/bin"
mkdir -p "$DESTINATION/Contents/MacOS" "$BACKUP_DIR" "$FAKE_BIN"
print -r -- "original-app" > "$DESTINATION/Contents/sentinel.txt"
print -r -- '#!/bin/sh' 'exit 0' > "$DESTINATION/Contents/MacOS/CodexCircuitResumer"
chmod 755 "$DESTINATION/Contents/MacOS/CodexCircuitResumer"

for stamp in 202501010101 202501010102 202501010103; do
  archive="$BACKUP_DIR/Test-$stamp.zip"
  : > "$archive"
  touch -t "$stamp" "$archive"
done

print -r -- '#!/bin/sh' 'exit 42' > "$FAKE_BIN/xcrun"
chmod 755 "$FAKE_BIN/xcrun"

set +e
PATH="$FAKE_BIN:$PATH" "$PROJECT_DIR/scripts/build_app.sh" "$DESTINATION" >/dev/null 2>&1
exit_code=$?
set -e

(( exit_code != 0 )) || { print -u2 "expected the injected build failure"; exit 1; }
[[ "$(<"$DESTINATION/Contents/sentinel.txt")" == "original-app" ]] || {
  print -u2 "the previous app was not restored"
  exit 1
}
archives=("$BACKUP_DIR"/*.zip(N))
(( ${#archives[@]} == 3 )) || { print -u2 "expected exactly three retained backups"; exit 1; }
partials=("$BACKUP_DIR"/*.partial.*(N))
(( ${#partials[@]} == 0 )) || { print -u2 "partial backup was not cleaned"; exit 1; }

print "build rollback test passed"
