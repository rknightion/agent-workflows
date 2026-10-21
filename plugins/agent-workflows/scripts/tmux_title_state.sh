#!/bin/sh
# Claude hook: keep the session state in the tmux pane option @cc_state, so a tmux that
# builds its outer title from it (set-titles-string in ~/.tmux.conf) shows the state on
# the terminal tab. That title survives mosh, which drops Claude's own status sequences.
# Outside tmux it does nothing, so local sessions are untouched.
#
#   tmux_title_state.sh working|waiting|done|failed|clear
[ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ] || exit 0

case "${1:-}" in
  working) mark='⏳ ' ;;
  waiting) mark='❓ ' ;;
  done)    mark='✅ ' ;;
  failed)  mark='❌ ' ;;
  clear)   mark='' ;;
  *)       exit 0 ;;
esac

tmux=$(command -v tmux 2>/dev/null) || tmux=/opt/homebrew/bin/tmux
if [ -n "$mark" ]; then
  "$tmux" set-option -p -t "$TMUX_PANE" @cc_state "$mark" >/dev/null 2>&1
else
  "$tmux" set-option -p -t "$TMUX_PANE" -u @cc_state >/dev/null 2>&1
fi
exit 0
