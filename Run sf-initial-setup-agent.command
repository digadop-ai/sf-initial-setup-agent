#!/usr/bin/env bash
# Double-click launcher for the sf-initial-setup-agent.
# Opens a Terminal window in the agent dir (when launched from Finder),
# runs ./run.sh, then auto-closes the Terminal window when run.sh exits
# (which happens after the retrieve reaches a terminal state — the in-app
# watchdog in web_ui.py flips uvicorn's should_exit so the Python process
# returns and run.sh unwinds).
cd "$(dirname "$0")"
printf '\033]0;sf-initial-setup-agent (running)\007'
./run.sh
status=$?

# Close the Terminal window this script is running in. Match by tty so we
# only close OUR window — a user may have other Terminal sessions open.
TTY=$(tty)
osascript <<EOF >/dev/null 2>&1
tell application "Terminal"
    repeat with w in windows
        repeat with t in tabs of w
            try
                if tty of t is "$TTY" then
                    close w saving no
                    return
                end if
            end try
        end repeat
    end repeat
end tell
EOF

exit $status
