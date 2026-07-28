#!/bin/sh
set -eu
PATH=/usr/bin:/bin
LANG=C
LC_ALL=C
TZ=UTC
export PATH LANG LC_ALL TZ

fail() {
  printf '%s\n' "error: $1" >&2
  exit 1
}

[ "$#" -eq 1 ] || fail "usage: verify-static-elf.sh binary"
BINARY=$1
[ -f "$BINARY" ] && [ ! -L "$BINARY" ] && [ -x "$BINARY" ] ||
  fail "binary must be an executable regular non-symlink file"
[ -x /usr/bin/file ] || fail "file(1) is unavailable"
[ -x /usr/bin/readelf ] || fail "readelf(1) is unavailable"
[ -x /usr/bin/awk ] || fail "awk(1) is unavailable"

FILE_OUTPUT=$(/usr/bin/file -b -- "$BINARY") || fail "file inspection failed"
case "$FILE_OUTPUT" in
  "ELF 64-bit LSB executable, x86-64, version 1 (SYSV), statically linked,"*) ;;
  *) fail "binary is not the closed static ELF64 AMD64 executable contract" ;;
esac
case "$FILE_OUTPUT" in
  *"dynamically linked"*|*"interpreter "*|*"pie executable"*)
    fail "binary retains a runtime loader dependency"
    ;;
esac

ELF_HEADER=$(/usr/bin/readelf --wide --file-header "$BINARY") ||
  fail "ELF header inspection failed"
case "$ELF_HEADER" in *"Class:"*"ELF64"*) ;; *) fail "ELF class is invalid" ;; esac
case "$ELF_HEADER" in *"Data:"*"2's complement, little endian"*) ;; *) fail "ELF byte order is invalid" ;; esac
case "$ELF_HEADER" in *"Type:"*"EXEC (Executable file)"*) ;; *) fail "ELF type is not ET_EXEC" ;; esac
case "$ELF_HEADER" in *"Machine:"*"Advanced Micro Devices X86-64"*) ;; *) fail "ELF machine is invalid" ;; esac

PROGRAM_HEADERS=$(/usr/bin/readelf --wide --program-headers "$BINARY") ||
  fail "ELF program-header inspection failed"
case "$PROGRAM_HEADERS" in *INTERP*) fail "PT_INTERP is forbidden" ;; esac
printf '%s\n' "$PROGRAM_HEADERS" | /usr/bin/awk '
  $1 == "LOAD" {
    loads += 1
    flags = ""
    for (field = 7; field < NF; field += 1) flags = flags $field
    if (index(flags, "W") && index(flags, "E")) bad_load = 1
  }
  $1 == "GNU_STACK" {
    stacks += 1
    flags = ""
    for (field = 7; field < NF; field += 1) flags = flags $field
    if (index(flags, "E")) bad_stack = 1
  }
  END {
    if (loads < 1 || bad_load || stacks != 1 || bad_stack) exit 1
  }
' || fail "ELF W^X or non-executable-stack contract failed"

DYNAMIC=$(/usr/bin/readelf --wide --dynamic "$BINARY") ||
  fail "ELF dynamic-section inspection failed"
case "$DYNAMIC" in *NEEDED*) fail "DT_NEEDED is forbidden" ;; esac
case "$DYNAMIC" in
  *"There is no dynamic section in this file."*) ;;
  *) fail "dynamic section is forbidden" ;;
esac

SECTIONS=$(/usr/bin/readelf --wide --sections "$BINARY") ||
  fail "ELF section inspection failed"
case "$SECTIONS" in *" .interp "*|*" .dynamic "*) fail "loader section is forbidden" ;; esac

printf '%s\n' static-elf-ok
