#!/bin/bash
# Double-click this file in Finder to run: scripts/daemon.sh start
cd "$(dirname "$0")" || exit 1
./daemon.sh start
code=$?
echo
read -n 1 -s -r -p "Press any key to close this window..."
echo
exit $code
