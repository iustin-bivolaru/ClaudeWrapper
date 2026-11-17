#!/usr/bin/env bash
# Symlink the remi launcher into ~/.local/bin (or $1).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-$HOME/.local/bin}"
mkdir -p "$target"
ln -sf "$root/bin/remi" "$target/remi"
echo "Installed: $target/remi -> $root/bin/remi"
case ":$PATH:" in *":$target:"*) ;; *) echo "Add $target to your PATH." ;; esac
command -v gitleaks >/dev/null || echo "Optional: 'brew install gitleaks' for a stronger pre-commit secret scan."
