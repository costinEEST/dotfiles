# LLVM / C++
export LLVM_HOME="/usr/local/opt/llvm"
export PATH="$LLVM_HOME/bin:$PATH"

export CC="$LLVM_HOME/bin/clang"
export CXX="$LLVM_HOME/bin/clang++"

# Java
export JAVA_HOME=$(/usr/libexec/java_home -v 25)

# labctl
export PATH="$PATH:$HOME/.iximiuz/labctl/bin"

# User-level tools: uv, uv-managed Python (python, python3), Claude Code.
# Must come after `brew shellenv` so it wins over Homebrew's python3.
export PATH="$HOME/.local/bin:$PATH"
