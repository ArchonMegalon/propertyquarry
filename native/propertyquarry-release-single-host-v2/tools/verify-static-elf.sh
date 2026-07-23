#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C LC_ALL=C
unset BASH_ENV ENV CDPATH GLOBIGNORE LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GCONV_PATH

[[ "$#" -eq 1 && "$1" = /* && -f "$1" && ! -L "$1" ]] || exit 2
binary="$1"
metadata="$(stat -Lc '%a:%h:%F' -- "$binary")"
[[ "$metadata" == "755:1:regular file" || "$metadata" == "555:1:regular file" ]] || exit 2
header="$(readelf -W -h -- "$binary")"
programs="$(readelf -W -l -- "$binary")"
dynamic="$(readelf -W -d -- "$binary")"
file_kind="$(file -b -- "$binary")"
[[ "$header" == *"Class:"*"ELF64"* ]]
[[ "$header" == *"Data:"*"little endian"* ]]
[[ "$header" == *"Type:"*"EXEC"* ]]
[[ "$header" == *"Machine:"*"Advanced Micro Devices X86-64"* ]]
[[ "$programs" != *" INTERP "* ]]
[[ "$dynamic" == *"There is no dynamic section in this file."* ]]
[[ "$dynamic" != *"(NEEDED)"* ]]
[[ "$file_kind" == *"statically linked"* ]]
[[ "$file_kind" == *"not stripped"* || "$file_kind" == *"stripped"* ]]
python3 - "$programs" <<'PY'
import sys
lines=sys.argv[1].splitlines()
for line in lines:
    fields=line.split()
    if fields and fields[0] == "GNU_STACK" and any("E" in field for field in fields[1:]):
        raise SystemExit(1)
    if fields and fields[0] == "LOAD" and fields[-1].isdigit():
        flags="".join(fields[-2:-1])
        if "W" in flags and "E" in flags:
            raise SystemExit(1)
PY
