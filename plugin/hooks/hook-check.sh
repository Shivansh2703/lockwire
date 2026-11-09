#!/bin/sh
# Plugin edit hook. Claude Code blocks an edit only on exit 2, so a missing or broken lockwire
# (exit 1, 127) would let every edit through. Here it blocks a robot-model edit with one line.
p=$(cat)
case $(printf %s "$p" | tr '[:upper:]' '[:lower:]') in
  *'.urdf"'* | *'.sdf"'* | *'.xacro"'* | *'lockwire.yaml"'*) ;;
  *) exit 0 ;;  # not a file the hook checks
esac
for lw in "lockwire" "uvx --from lockwire lockwire"; do
  if $lw --version >/dev/null 2>&1 </dev/null; then
    printf '%s' "$p" | $lw hook-check && exit 0
    [ $? = 2 ] || echo "lockwire hook-check crashed (see above); fix lockwire, then retry" >&2
    exit 2
  fi
done
echo "lockwire not found or broken: install it with 'uv tool install lockwire', then retry the edit" >&2
exit 2
