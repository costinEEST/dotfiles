# Rust environment (required for all shells)
. "$HOME/.cargo/env"
# User-level tools: uv, uv-managed Python (python, python3), Claude Code.
# Login shells reorder PATH after this file (path_helper, brew shellenv), so
# ~/.zsh/env.zsh prepends it again. typeset -U drops the duplicate entries.
typeset -U path PATH
export PATH="$HOME/.local/bin:$PATH"
