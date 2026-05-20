#!/usr/bin/env bash
# Run the unittest suite from the project root.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 -m unittest discover -s tests -v
