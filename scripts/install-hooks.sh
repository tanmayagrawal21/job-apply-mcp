#!/usr/bin/env bash
# Optional: install a local pre-commit hook that runs the privacy scan on staged changes.
# If <config dir>/privacy_terms.txt (XDG-aware) exists it is passed as --terms (never printed or committed).
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
hook="$root/.git/hooks/pre-commit"
[ -e "$hook" ] && ! grep -q privacy_scan "$hook" && { echo "pre-commit exists; add the scan manually"; exit 1; }
cat > "$hook" <<'HOOK'
#!/usr/bin/env bash
root="$(git rev-parse --show-toplevel)"
if ! terms="$(PYTHONPATH="$root/src" python3 -c 'from job_apply_mcp import paths; print(paths.privacy_terms_file())' 2>/dev/null)" || [ -z "$terms" ]; then
  guess="${XDG_CONFIG_HOME:-$HOME/.config}/job-apply-mcp/privacy_terms.txt"
  echo "privacy hook: WARNING could not resolve the privacy terms path with python3" >&2
  if [ -e "$guess" ]; then
    echo "privacy hook: a terms file exists at the default location but cannot be loaded; commit blocked" >&2
    exit 1
  fi
  echo "privacy hook: scanning WITHOUT private terms" >&2
  terms=""
fi
args=(--staged)
if [ -n "$terms" ] && [ -e "$terms" ]; then
  if [ ! -f "$terms" ] || [ ! -r "$terms" ]; then
    echo "privacy hook: terms file exists but is not a readable file; commit blocked" >&2
    exit 1
  fi
  args+=(--terms "$terms")
else
  echo "privacy hook: WARNING no privacy terms file found; scanning without private terms" >&2
fi
exec python3 "$root/scripts/privacy_scan.py" "${args[@]}"
HOOK
chmod +x "$hook"
echo "installed $hook"
