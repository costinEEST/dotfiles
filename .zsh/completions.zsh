# Lazy-load completion system
if ! typeset -f _complete >/dev/null 2>&1; then
  autoload -Uz compinit
  compinit
fi

# labctl completion (lazy)
if command -v labctl >/dev/null 2>&1; then
  source <(labctl completion zsh)
fi