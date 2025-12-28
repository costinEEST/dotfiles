# tree fallback if not installed
tree() {
  if command -v tree >/dev/null 2>&1; then
    command tree "$@"
  else
    find . -print | sed -e 's;[^/]*/;│   ;g;s;│   \([^│]\);├── \1;'
  fi
}