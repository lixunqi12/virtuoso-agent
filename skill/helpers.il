;;; helpers.il — Public utility functions for PDK-safe SKILL scripts
;;; Compatible with Cadence IC23.1 SKILL (no SKILL++ syntax)
;;;
;;; Provides:
;;;   safeHelpers_listToJson(lst)         - Convert a SKILL association list to JSON string
;;;   safeHelpers_cellAlias(cellName)     - Map foundry cell name to generic alias
;;;   safeHelpers_validateParamName(name) - Check if parameter name is in the whitelist
;;;   safeHelpers_validateParamValue(val) - Check if parameter value is safe (no injection)
;;;   safeHelpers_jsonEscape(str)         - Escape a string for JSON embedding
;;;   safeHelpers_numToJson(val)          - Convert numeric value to JSON-safe string

;;; ----------------------------------------------------------------
;;; Global configuration — override these after loading to customize
;;; ----------------------------------------------------------------

;; Cell name mapping: foundry name -> generic name
;; MUST be populated on the remote side only, via ~/.virtuoso/pdk_map_private.il
;; (see docs/remote_deployment/pdk_map_private.il.example for the format).
;; The PC-side repo must not contain real foundry cell names.
defvar(safeHelpers_cellMap  nil)

;; Fallback generic name when cell is not in the map
defvar(safeHelpers_genericCellName  "GENERIC_DEVICE")

;; Allowed design parameters (lowercase symbols)
defvar(safeHelpers_allowedParams  '(w l nf nfin fingers m multi wf r c idc vdc))

;; Safe operating-point keys
;;
;; Rev 7 (Stage B, 2026-04-19): extended for tranOp op-point feedback to
;; the LLM. Probe 7 v4 confirmed these attrs are exposed by the
;; BSIM-CMG FinFET compact model via handle->prop on the IC23.1
;; tranOp result container. Keys dropped vs rev 6:
;;   - cdb       (not exposed by tmibsimcmg in probe v4)
;; Keys added vs rev 6:
;;   - vgs vds ids region   (per-device bias + saturation region enum)
;; `region` returns an integer enum (0..4); Python side maps to a string
;; label ("cutoff"/"triode"/"saturation"/"subthreshold"/"breakdown").
;; `vov` is NOT in this list — it is a NIL attr on tmibsimcmg and must
;; be computed Python-side as (vgs - vth).
defvar(safeHelpers_safeOpKeys  '(vgs vds ids id gm gds vth vdsat region cgs cgd))

;; Schematic decoration cells to skip when reading topology.
;; These are not active devices (ground symbols, I/O pin markers,
;; supply rails); filtering them keeps the LLM-facing topology
;; focused on actual DUT components. Cell-name match is case-sensitive.
defvar(safeHelpers_skipCells  '("gnd" "vdd" "vss" "ipin" "opin" "iopin"))

;; Blocklist for Layer-2 fallback validation (case-insensitive substrings).
;; Names that match the safe-char pattern but contain any of these tokens
;; are rejected. Covers SKILL-injection function names and PDK-leakage
;; keywords. Keep in sync with _BLOCKED_PARAM_WORDS in src/safe_bridge.py.
defvar(safeHelpers_blockedWords  '(
    "load" "file" "path" "system" "eval" "exec" "shell" "include" "require"
    "getss" "errset" "evalstring" "infile" "outfile" "popen" "ipcbegin"
    "rexcompile" "rexexecute" "sprintf" "printf"
    "model" "subckt" "section"
))

;; Rev 5: BSIM model intrinsic names to reject with EXACT match (case-ins.).
;; Substring match would false-positive on legitimate names like "stk1" /
;; "link2" / "mu0level", so we keep this as a separate exact-match set.
;; Must stay aligned with model_info_keys in pdk_map.yaml and with
;; self.model_info_keys in src/safe_bridge.py.
defvar(safeHelpers_blockedModelParams  '(
    toxe u0 vth0 k1 k2 pclm
    dvt0 dvt1 nfactor cdsc eta0 dsub
))


;;; ----------------------------------------------------------------
;;; Load the remote-local private cell map (foundry name -> generic name).
;;; This file must live ONLY on the remote host and must never be copied
;;; into the PC-side repo. If it is missing, safeHelpers_cellMap stays
;;; nil, so safeHelpers_cellAlias() will return the generic fallback
;;; for every cell — the safe-fail mode.
;;; ----------------------------------------------------------------
let((privateMapPath home)
    home = getShellEnvVar("HOME")
    when(home
        privateMapPath = strcat(home "/.virtuoso/pdk_map_private.il")
        when(isFile(privateMapPath)
            load(privateMapPath)
        )
    )
)


;;; ----------------------------------------------------------------
;;; JSON string escaping
;;; ----------------------------------------------------------------
procedure(safeHelpers_jsonEscape(str)
    let((result ch i len dq)
        result = ""
        dq = getchar("\"" 1)
        len = strlen(str)
        for(i 1 len
            ch = getchar(str i)
            cond(
                (ch == '\\ result = strcat(result "\\\\"))
                (ch == dq  result = strcat(result "\\\""))
                (ch == '\n result = strcat(result "\\n"))
                (ch == '\r result = strcat(result "\\r"))
                (ch == '\t result = strcat(result "\\t"))
                (t         result = strcat(result substring(str i 1)))
            )
        )
        result
    )
)


;;; ----------------------------------------------------------------
;;; Numeric to JSON string (handles special float values)
;;; ----------------------------------------------------------------
procedure(safeHelpers_numToJson(val)
    cond(
        (fixp(val)   sprintf(nil "%d" val))
        (floatp(val)
            cond(
                (val != val         "null")   ; NaN
                (val == 1e308 * 2   "null")   ; +Inf
                (val == -1e308 * 2  "null")   ; -Inf
                (t                  sprintf(nil "%.12g" val))
            )
        )
        (t  "null")
    )
)


;;; ----------------------------------------------------------------
;;; Cell name alias lookup
;;; ----------------------------------------------------------------
procedure(safeHelpers_cellAlias(cellName)
    let((entry result)
        result = safeHelpers_genericCellName
        foreach(entry safeHelpers_cellMap
            when(equal(car(entry) cellName)
                result = cadr(entry)
            )
        )
        result
    )
)


;;; ----------------------------------------------------------------
;;; Check if a generic alias already (value side of map)
;;; If the name is already generic, return it as-is.
;;; ----------------------------------------------------------------
procedure(safeHelpers_isGenericName(cellName)
    let((entry found)
        found = nil
        foreach(entry safeHelpers_cellMap
            when(equal(cadr(entry) cellName)
                found = t
            )
        )
        found
    )
)


;;; ----------------------------------------------------------------
;;; Parameter name validation — two-layer check:
;;;   Layer 1: exact match against safeHelpers_allowedParams (case-ins.)
;;;   Layer 2: ^[a-zA-Z][a-zA-Z0-9_]{0,31}$ AND no blocklist substring
;;; Returns t if valid, nil otherwise.
;;; ----------------------------------------------------------------
procedure(safeHelpers_validateParamName(name)
    prog((nameLower)
        unless(stringp(name)  return(nil))
        nameLower = lowerCase(name)
        ;; Layer 1
        when(member(stringToSymbol(nameLower) safeHelpers_allowedParams)
            return(t)
        )
        ;; Layer 2 (rev 5: also reject exact BSIM model-intrinsic names;
        ;; mirrors Python self.model_info_keys check in _is_allowed_param_name)
        when(and(safeHelpers_matchesSafeVarPattern(name)
                 not(safeHelpers_containsBlockedWord(nameLower))
                 not(member(stringToSymbol(nameLower)
                            safeHelpers_blockedModelParams)))
            return(t)
        )
        return(nil)
    )
)


;;; ----------------------------------------------------------------
;;; Safe-char pattern check: ^[a-zA-Z][a-zA-Z0-9_]{0,31}\z
;;;
;;; Rev 6.2.2 (Route C, pcreMatchp + \z, dual-APPROVE'd 2026-04-17).
;;; Uses \z (absolute end-of-string) instead of $ because PCRE $ also
;;; matches just-before-trailing-\n — that would accept "Ibias\n" as
;;; valid. Probe 5a confirmed \z is supported on this IC23.1 install.
;;; Mirrors Python _NAME_RE shape (^[a-zA-Z][a-zA-Z0-9_]{0,31}\Z) for
;;; audit-single-source.
;;; ----------------------------------------------------------------
procedure(safeHelpers_matchesSafeVarPattern(name)
    if(stringp(name)
        pcreMatchp("^[a-zA-Z][a-zA-Z0-9_]{0,31}\\z" name)
        nil
    )
)


;;; ----------------------------------------------------------------
;;; Rev 6.4 F1 (dual-signed 2026-04-18): literal substring-contains
;;; predicate built from substring/equal/strlen, confirmed callable on
;;; IC23.1 by Probe 6 (§6.2.10). Replaces the rev-6.3 strsearch call,
;;; which Probe 6 demonstrated is undefined on this IC23.1 install.
;;; Semantics: returns t iff `needle` appears as a contiguous literal
;;; substring of `haystack`. An empty `needle` matches trivially (t);
;;; a `needle` longer than `haystack` cannot match (nil). SKILL
;;; substring is 1-indexed; the loop scans start positions 1..maxStart.
;;; ----------------------------------------------------------------
procedure(safeHelpers_literalContainsp(haystack needle)
    prog((hayLen needleLen maxStart i)
        hayLen = strlen(haystack)
        needleLen = strlen(needle)
        when(needleLen == 0  return(t))
        when(needleLen > hayLen  return(nil))
        maxStart = hayLen - needleLen + 1
        for(i 1 maxStart
            when(equal(substring(haystack i needleLen) needle)
                return(t)
            )
        )
        return(nil)
    )
)


;;; ----------------------------------------------------------------
;;; Blocklist check: does lowercased name contain any blocked substring?
;;; Uses safeHelpers_literalContainsp (substring+equal+strlen) — NOT
;;; rexMatchp. rexMatchp would treat metachars in future blocklist
;;; entries as regex syntax and could silently under-match (e.g. a
;;; word containing "." would match any char). literalContainsp is a
;;; pure literal scan.
;;; ----------------------------------------------------------------
procedure(safeHelpers_containsBlockedWord(nameLower)
    prog((word found)
        found = nil
        foreach(word safeHelpers_blockedWords
            when(safeHelpers_literalContainsp(nameLower word)
                found = t
            )
        )
        return(found)
    )
)


;;; ----------------------------------------------------------------
;;; Parameter value validation — only numeric literals or
;;; engineering-unit strings like "10u", "500n", "1.2m"
;;; Rejects anything that could be SKILL injection.
;;; Returns t if safe, nil otherwise.
;;; ----------------------------------------------------------------
procedure(safeHelpers_validateParamValue(val)
    ;; Rev 6.3 (dual-APPROVE'd 2026-04-17; rev 6.2.2 Route C + drop
    ;; lstrip/rstrip). The rev-5 lstrip(s)/rstrip(s) guards were
    ;; undefined on IC23.1 and aborted the load-time self-test for
    ;; "500u". They are provably redundant under Route C: whitespace,
    ;; tab, CR and newline are NOT in the regex character class
    ;; [+-0-9.eEa-zA-Z], and `^`/`\z` are absolute anchors, so
    ;; leading/trailing/embedded whitespace all reject at the
    ;; pcreMatchp gate (reviewer walk recorded in §6.2.10 rev 6.3).
    ;;
    ;; `strlen(s) == 0` is kept as an explicit early-out; the regex
    ;; also rejects empty (mandatory `[0-9]+`), but the explicit
    ;; check is cheaper, more readable, and costs nothing.
    ;;
    ;; Accept numbers; for strings, require numeric-literal shape with
    ;; optional engineering-unit letters. The numeric regex strictly
    ;; subsumes the rev 5 substring blocklist (dbOpen/load/shell/system
    ;; /procedure/()";), so no separate blocklist pass is needed — none
    ;; of those tokens can match ^[+-]?[0-9]*\.?[0-9]+([eE][+-]?[0-9]+)?
    ;; [a-zA-Z]*\z (see §6.2.6 blocklist subsumption row).
    ;;
    ;; \z (not $) closes the trailing-\n acceptance hazard that the
    ;; rev 5 rexMatchp blocklist had; Probe 5a confirmed \z support.
    prog((s)
        return(cond(
            (numberp(val)  t)
            (not(stringp(val))  nil)
            (t
                s = val
                when(strlen(s) == 0
                    return(nil)
                )
                pcreMatchp(
                    "^[+-]?[0-9]*\\.?[0-9]+([eE][+-]?[0-9]+)?[a-zA-Z]*\\z"
                    s)
            )
        ))
    )
)


;;; ----------------------------------------------------------------
;;; Convert a SKILL assoc-list to a JSON object string.
;;;
;;; Input: list of (key . value) or (key value) pairs where:
;;;   - key is a string
;;;   - value is a string, number, list (nested object), or nil
;;;
;;; For simple key-value JSON, pass:
;;;   list(list("name" "M0") list("w" 1e-6))
;;; Produces:  {"name":"M0","w":1e-06}
;;; ----------------------------------------------------------------
procedure(safeHelpers_listToJson(lst)
    let((parts entry k v jsonVal)
        if(lst == nil then
            "{}"
        else
            parts = nil
            foreach(entry lst
                k = safeHelpers_jsonEscape(car(entry))
                v = cadr(entry)
                cond(
                    (v == nil
                        jsonVal = "null"
                    )
                    (stringp(v)
                        jsonVal = strcat("\"" safeHelpers_jsonEscape(v) "\"")
                    )
                    (numberp(v)
                        jsonVal = safeHelpers_numToJson(v)
                    )
                    (listp(v)
                        ;; If first element is also a list, treat as nested object
                        if(and(v listp(car(v))) then
                            jsonVal = safeHelpers_listToJson(v)
                        else
                            ;; Treat as JSON array of strings/numbers
                            jsonVal = safeHelpers_listToJsonArray(v)
                        )
                    )
                    (t
                        jsonVal = strcat("\"" safeHelpers_jsonEscape(
                            sprintf(nil "%L" v)) "\"")
                    )
                )
                parts = cons(strcat("\"" k "\":" jsonVal) parts)
            )
            strcat("{" buildString(reverse(parts) ",") "}")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Convert a flat SKILL list to a JSON array string.
;;; ----------------------------------------------------------------
procedure(safeHelpers_listToJsonArray(lst)
    let((parts item jsonVal)
        if(lst == nil then
            "[]"
        else
            parts = nil
            foreach(item lst
                cond(
                    (item == nil    jsonVal = "null")
                    (stringp(item)  jsonVal = strcat("\"" safeHelpers_jsonEscape(item) "\""))
                    (numberp(item)  jsonVal = safeHelpers_numToJson(item))
                    (listp(item)
                        if(and(item listp(car(item))) then
                            jsonVal = safeHelpers_listToJson(item)
                        else
                            jsonVal = safeHelpers_listToJsonArray(item)
                        )
                    )
                    (t  jsonVal = strcat("\"" safeHelpers_jsonEscape(
                            sprintf(nil "%L" item)) "\""))
                )
                parts = cons(jsonVal parts)
            )
            strcat("[" buildString(reverse(parts) ",") "]")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Rev 6.1: load-time self-test. Catches the char-code bug class
;;; (integer from getchar vs 'a symbol) loud and early, instead of
;;; deferring to first-call abort at OCEAN/Maestro runtime. Placed
;;; BEFORE the "loaded successfully" printf so a failure suppresses
;;; the success message and aborts via error(), giving a clear
;;; visual signal in CIW.
;;; ----------------------------------------------------------------
unless(safeHelpers_matchesSafeVarPattern("Abc_123")
    error("SELF-TEST FAIL: matchesSafeVarPattern rejected 'Abc_123'")
)
when(safeHelpers_matchesSafeVarPattern("1abc")
    error("SELF-TEST FAIL: matchesSafeVarPattern accepted '1abc'")
)
unless(safeHelpers_validateParamValue("500u")
    error("SELF-TEST FAIL: validateParamValue rejected '500u'")
)
when(safeHelpers_validateParamValue("foo(bar)")
    error("SELF-TEST FAIL: validateParamValue accepted 'foo(bar)'")
)
;; Rev 6.4 F1: exercise the validateParamName path end-to-end so the
;; strsearch→literalContainsp swap is proven at load, closing the
;; coverage gap that let rev 6.3's strsearch bug ship past self-test.
unless(safeHelpers_validateParamName("Ibias")
    error("SELF-TEST FAIL: validateParamName rejected 'Ibias'")
)
when(safeHelpers_validateParamName("loadIt")
    error("SELF-TEST FAIL: validateParamName accepted 'loadIt'")
)

printf("helpers.il loaded successfully.\n")
