"""The native names of an exe: its NUL-terminated strings of the native groups.

    python native_strings.py X3AP.exe > names.txt

A native name is a string GROUP_Name (groups SA SE B3D INS X2 STA P SFX J3D MOV TI GS) standing alone (no printable
byte before it, a NUL after it). One letter after the prefix (P_T, P_Y, ...) is code bytes, not a name. This is how
x3kc/natives_ap.txt and natives_fl.txt were made; the dispatchers' name tables hold the same names.
"""
import re
import sys

PAT = re.compile(rb'(?<![\x21-\x7e])((?:SA|SE|B3D|INS|X2|STA|P|SFX|J3D|MOV|TI|GS)_[A-Za-z0-9_]{2,})\x00')


def names(data):
    return sorted({m.group(1).decode() for m in PAT.finditer(data)})


if __name__ == '__main__':
    with open(sys.argv[1], 'rb') as f:
        sys.stdout.reconfigure(newline='\n')
        sys.stdout.write(''.join(n + '\n' for n in names(f.read())))
