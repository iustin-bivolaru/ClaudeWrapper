#!/usr/bin/env bash
# Symlink the remi launcher into ~/.local/bin (or $1).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-$HOME/.local/bin}"
mkdir -p "$target"
ln -sf "$root/bin/remi" "$target/remi"
echo "Installed: $target/remi -> $root/bin/remi"
case ":$PATH:" in *":$target:"*) ;; *) echo "Add $target to your PATH." ;; esac
# pyright-lsp: remi's settings enable it; the plugin itself must be installed once.
if ! grep -q '"pyright-lsp@claude-plugins-official"' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null; then
  claude plugin install pyright-lsp@claude-plugins-official || echo "Install the plugin: claude plugin install pyright-lsp@claude-plugins-official"
fi
if ! command -v pyright-langserver >/dev/null; then
  if command -v uv >/dev/null; then uv tool install pyright; else echo "Install pyright for type errors and code navigation: uv tool install pyright (or npm i -g pyright)."; fi
fi
command -v gitleaks >/dev/null || echo "Optional: 'brew install gitleaks' for a stronger pre-commit secret scan."
