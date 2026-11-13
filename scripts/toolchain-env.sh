#!/bin/sh
# Sourced by checks and Git hooks, including GUI/noninteractive Git callers.
export PATH="$HOME/.local/bin:$PATH"
if [ -d "$HOME/.nvm/versions/node" ]; then
  octopus_node_version=$(ls "$HOME/.nvm/versions/node" | sort -V | tail -1)
  export PATH="$HOME/.nvm/versions/node/$octopus_node_version/bin:$PATH"
  unset octopus_node_version
fi
