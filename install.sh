#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ln -sf "$DIR/.vimrc" "$HOME/.vimrc"
ln -sf "$DIR/.tmux.conf" "$HOME/.tmux.conf"
grep -q "aliases.sh" "$HOME/.bashrc" || echo "source $DIR/aliases.sh" >> "$HOME/.bashrc"
echo "dotfiles linked."
