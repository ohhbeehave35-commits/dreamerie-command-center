#!/usr/bin/env bash
# Install the pre-push guard.
#
# WHY THIS EXISTS: on 29 Jul a red test suite was pushed twice in one session,
# both times because `pytest` and `git push` were chained in a single command
# so the push ran regardless of the failure. The rule against that was already
# written down and already loaded. Writing it down again does not work.
#
# This makes it mechanical: the push is refused.
#
#   bash tools/install_hooks.sh
#
# Emergency bypass is `git push --no-verify`, which is deliberately something
# you have to type on purpose rather than something that happens by accident.

set -euo pipefail
HOOK_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOK_DIR"

cat > "$HOOK_DIR/pre-push" <<'HOOK'
#!/usr/bin/env bash
# Refuse a push when the deterministic suite or the Bug Doctor is red.
# Learning suites (round2/round3/chaos/progressive/tier_) are excluded: they
# fail by design and blocking on them would train everyone to --no-verify.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "pre-push: running the deterministic suite..."
REG=$(ls tests/test_*.py 2>/dev/null \
      | grep -vE "round3|round2|chaos|progressive|tier_|full_suite" \
      | tr '\n' ' ')
if [ -z "$REG" ]; then
  echo "pre-push: no tests found -- allowing."
  exit 0
fi

if ! python -m pytest $REG -q -p no:randomly >/tmp/prepush.log 2>&1; then
  echo ""
  echo "  ================================================================"
  echo "  PUSH REFUSED -- tests are failing."
  echo "  ================================================================"
  tail -25 /tmp/prepush.log
  echo ""
  echo "  Fix them, or push with --no-verify if you genuinely mean to."
  exit 1
fi
echo "pre-push: tests pass."

if [ -f tools/bug_doctor.py ]; then
  if ! python tools/bug_doctor.py >/tmp/prepush_doctor.log 2>&1; then
    echo ""
    echo "  ================================================================"
    echo "  PUSH REFUSED -- Bug Doctor found a NEW issue."
    echo "  ================================================================"
    tail -30 /tmp/prepush_doctor.log
    echo ""
    echo "  Fix it, or add it to tools/bug_doctor_baseline.txt WITH a reason."
    exit 1
  fi
  echo "pre-push: bug doctor clean."
fi
exit 0
HOOK

chmod +x "$HOOK_DIR/pre-push"
echo "installed: $HOOK_DIR/pre-push"
echo "verify with: git push --dry-run"
