#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: not inside a git repository"
  exit 1
fi

echo "==> Newy pre-push privacy check"
tracked_count=$(git ls-files | wc -l | tr -d ' ')
echo "Tracked files: $tracked_count"

failures=0
warnings=0

tracked_forbidden=(
  "data/config.local.json"
  "data/newy.sqlite3"
  "data/newy.sqlite3-shm"
  "data/newy.sqlite3-wal"
)
for path in "${tracked_forbidden[@]}"; do
  if git ls-files --error-unmatch "$path" >/dev/null 2>&1; then
    echo "FAIL  tracked local/runtime file: $path"
    failures=$((failures + 1))
  fi
done

scan_output=$(python3 - <<'PY'
import pathlib, re, subprocess
files = subprocess.check_output(['git','ls-files'], text=True).splitlines()
allow_phone_values = {'+14155238886', '+10000000000', '+10000000001', '+10000000002'}
patterns = {
    'twilio_sid': re.compile(r'AC[a-zA-Z0-9]{32}'),
    'openai_key': re.compile(r'sk-[A-Za-z0-9_-]{20,}'),
    'github_pat': re.compile(r'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'),
    'aws_access_key': re.compile(r'AKIA[0-9A-Z]{16}'),
    'generic_bearer': re.compile(r'Bearer\s+[A-Za-z0-9._-]{20,}'),
    'email': re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'phone_e164': re.compile(r'\+\d{8,15}'),
}
fail_names = {'twilio_sid','openai_key','github_pat','aws_access_key','generic_bearer'}
warn_names = {'email','phone_e164'}
fail_count = 0
warn_count = 0
for path in files:
    try:
        data = pathlib.Path(path).read_text(errors='ignore')
    except Exception:
        continue
    for name, pat in patterns.items():
        for m in pat.finditer(data):
            value = m.group(0)
            if name == 'phone_e164' and value in allow_phone_values:
                continue
            line_no = data.count('\n', 0, m.start()) + 1
            if name in fail_names:
                fail_count += 1
                print(f'FAIL  {path}:{line_no}  {name}  {value}')
            elif name in warn_names:
                warn_count += 1
                print(f'WARN  {path}:{line_no}  {name}  {value}')
print(f'SUMMARY\t{fail_count}\t{warn_count}')
PY
)

echo "$scan_output" | sed '/^SUMMARY\t/d'
summary_line=$(echo "$scan_output" | grep '^SUMMARY')
scan_failures=$(echo "$summary_line" | cut -f2)
scan_warnings=$(echo "$summary_line" | cut -f3)
failures=$((failures + scan_failures))
warnings=$((warnings + scan_warnings))

echo
echo "Ignored local/private files present locally (expected, not tracked):"
git status --short --ignored | grep '^!!' || true

echo
if [[ $failures -gt 0 ]]; then
  echo "RESULT: FAIL ($failures blocking findings, $warnings warnings)"
  exit 1
fi

echo "RESULT: PASS ($warnings warnings)"
echo "Blocking secrets were not found in tracked files. Review warnings manually if any appear."
