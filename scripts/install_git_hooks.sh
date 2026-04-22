#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p .git/hooks
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
"$(git rev-parse --show-toplevel)/scripts/prepush_privacy_check.sh"
HOOK
chmod +x .git/hooks/pre-push

echo "Installed .git/hooks/pre-push -> scripts/prepush_privacy_check.sh"
