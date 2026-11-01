#!/bin/bash

# rbenv picks the Ruby version from live/.ruby-version
cd /home/mastodon/live || exit 1
export RAILS_ENV=production

status=0

run_tootctl() {
    echo "Running tootctl $*"
    if ! bin/tootctl "$@"; then
        echo "FAILED: tootctl $*"
        status=1
    fi
}

# Prune remote accounts that never interacted with a local user
run_tootctl accounts prune

# Remove remote statuses that local users never interacted with older than 4 days
run_tootctl statuses remove --days 4

# Remove media attachments older than 4 days
run_tootctl media remove --days 4

# Remove avatars and headers of accounts nobody follows, not seen for 4 days
run_tootctl media remove --prune-profiles --days 4

# Remove all headers (including people I follow)
run_tootctl media remove --remove-headers --include-follows --days 0

# Remove link previews older than 4 days
run_tootctl preview_cards remove --days 4

# Remove files not linked to any post
run_tootctl media remove-orphans

exit "$status"
