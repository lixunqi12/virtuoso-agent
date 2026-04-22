import sys
p = 0
in_str = False
esc = False
with open(sys.argv[1], "r", encoding="utf-8") as f:
    for lineno, line in enumerate(f, 1):
        i = 0
        # strip line comment (; that is not inside a string)
        pos = []
        j = 0
        s_in_str = False
        s_esc = False
        cut = len(line)
        for j, c in enumerate(line):
            if s_esc:
                s_esc = False
                continue
            if c == "\\":
                s_esc = True
                continue
            if c == '"':
                s_in_str = not s_in_str
                continue
            if c == ";" and not s_in_str:
                cut = j
                break
        line = line[:cut]
        # count parens outside strings
        esc2 = False
        in_s2 = False
        for c in line:
            if esc2:
                esc2 = False
                continue
            if c == "\\":
                esc2 = True
                continue
            if c == '"':
                in_s2 = not in_s2
                continue
            if in_s2:
                continue
            if c == "(":
                p += 1
            elif c == ")":
                p -= 1
        if p < 0:
            print(f"line {lineno}: extra )  (running balance {p})")
print(f"final paren balance: {p}  (0 = balanced)")
