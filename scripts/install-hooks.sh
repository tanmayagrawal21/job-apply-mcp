#!/usr/bin/env bash
# Optional: install a local pre-commit hook that runs the privacy scan on staged changes.
# If ~/.config/job-apply-mcp/privacy_terms.txt exists it is passed as --terms (never printed or committed).
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
hook="$root/.git/hooks/pre-commit"
[ -e "$hook" ] && ! grep -q privacy_scan "$hook" && { echo "pre-commit exists; add the scan manually"; exit 1; }
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
root="$(git rev-parse --show-toplevel)"
terms="$HOME/.config/job-apply-mcp/privacy_terms.txt"
args=(--staged)
[ -f "$terms" ] && args+=(--terms "$terms")
exec python3 "$root/scripts/privacy_scan.py" "${args[@]}"
HOOK
chmod +x "$hook"
echo "installed $hook"
