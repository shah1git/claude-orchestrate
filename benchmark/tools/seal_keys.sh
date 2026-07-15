#!/usr/bin/env bash
# Seal / unseal the polygon's hidden answer keys (DESIGN §4 vector 3, §8).
#
# seal   — hash every hidden file into hidden.sha256, pack+encrypt them into
#          hidden.tar.enc, then DELETE the plaintext so no candidate can read
#          a key that is not on disk. Passphrase is read from $POLYGON_PASS.
# unseal — restore plaintext from the encrypted archive (used at grading),
#          verifying every file against hidden.sha256 first.
#
# The list of sealed paths is exactly the answer keys — never the
# candidate-visible task material (tickets, visible_tests, repro.py, the
# buggy fixtures, the critic deliverable).
set -euo pipefail
BM="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BM"

SEALED=(
  tasks/scout/t1/hidden/key.json
  tasks/scout/t2/hidden/key.json
  tasks/scout/t3/hidden
  tasks/builder/t1/hidden
  tasks/builder/t2/hidden
  tasks/critic/t1/hidden
  tasks/architect/t1/hidden/cause.md
  tasks/architect/t2/hidden/cause.md
)

case "${1:-}" in
  seal)
    : "${POLYGON_PASS:?set POLYGON_PASS}"
    # Drop caches so they are neither hashed nor packed.
    find "${SEALED[@]}" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    # Provenance: hash every sealed file (sorted, reproducible).
    find "${SEALED[@]}" -type f | sort | xargs sha256sum > hidden.sha256
    tar -cf - "${SEALED[@]}" \
      | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:POLYGON_PASS \
      > hidden.tar.enc
    # Structural anti-cheat: remove the plaintext keys entirely.
    rm -rf "${SEALED[@]}"
    echo "sealed $(wc -l < hidden.sha256) files -> hidden.tar.enc ; plaintext removed"
    ;;
  unseal)
    : "${POLYGON_PASS:?set POLYGON_PASS}"
    openssl enc -d -aes-256-cbc -pbkdf2 -pass env:POLYGON_PASS -in hidden.tar.enc \
      | tar -xf -
    sha256sum -c hidden.sha256
    echo "unsealed and verified against hidden.sha256"
    ;;
  *) echo "usage: $0 {seal|unseal}   (POLYGON_PASS must be set)"; exit 2 ;;
esac
