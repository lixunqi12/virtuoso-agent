;;; safe_patch_netlist.il — Plan Auto: bias-warm-start ic patcher.
;;; Compatible with Cadence IC23.1 / IC6.1.8 SKILL (no SKILL++ syntax).
;;;
;;; Requires: helpers.il loaded first (for safeHelpers_jsonEscape and
;;; safeOcean_validSigName; both come from safe_ocean.il's dependency
;;; graph). See virtuoso-agent config/LC_VCO_spec.md §9 for the
;;; rationale; briefly: skipdc=yes zeroes every non-ic node, including
;;; bias mirrors, which breaks startup. This helper reads spectre.fc
;;; (post-tran equilibrium snapshot) and rewrites input.scs's ic line
;;; so every non-perturb node is warm-started at its equilibrium value.
;;;
;;; Entry point:
;;;   safePatchNetlistIC(scsPath fcPath perturbListJson vCmHint)
;;;
;;;     scsPath         : POSIX path to Maestro's input.scs
;;;     fcPath          : POSIX path to spectre.fc (usually next to input.scs)
;;;     perturbListJson : SKILL list (from PC) of lists, each
;;;                       (name offset_mV) — offset_mV is a number.
;;;     vCmHint         : Numeric V_cm fallback when fc has no Vout pair.
;;;
;;;   Returns JSON:
;;;     {"ok":true,"numBiasNodes":N,"numPerturb":M,"vcmMeasured":v}
;;;     {"ok":false,"error":"..."}
;;;
;;; Security:
;;;   - Both paths validated: non-empty strings, no shell metachars.
;;;   - Perturb node names validated through safeOcean_validSigName.
;;;   - fc lines with "#unit A" (branch currents) or ":" in the name
;;;     (internal model nodes) are dropped — only top-level net
;;;     voltages reach the patched ic statement.
;;;   - The ic line replacement is line-granular: we match the
;;;     "^ic " prefix exactly, so any other line starting with 'i'
;;;     (include, ibis, ...) is untouched.

;;; ----------------------------------------------------------------
;;; Path guard: reject shell metacharacters, NUL, embedded quotes.
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_validPath(p)
    let((bad)
        when(stringp(p)
            bad = nil
            foreach(ch '("\"" "\\" "\n" "\r" "\t" ";" "`" "$" "(" ")" "|" "&")
                when(index(p ch) bad = t)
            )
            and(not(bad) (strlen(p) > 0) (strlen(p) <= 1024))
        )
    )
)

;;; ----------------------------------------------------------------
;;; Slurp a whole file into one string. Returns nil on open failure.
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_readAll(path)
    prog((port allText line)
        port = nil
        errset(port = infile(path))
        unless(port return(nil))
        allText = ""
        line = ""
        while(gets(line port)
            allText = strcat(allText line)
        )
        close(port)
        return(allText)
    )
)

;;; ----------------------------------------------------------------
;;; Parse spectre.fc into a list of (name value-string) pairs.
;;;
;;; Rules:
;;;   - Skip lines starting with "#" (comments / header).
;;;   - Skip lines containing "#unit A" (branch currents — ic can't
;;;     set these anyway).
;;;   - Skip lines whose first token contains ":" (internal model
;;;     terminals like "I0.M0:int_di" — setting ic on those often
;;;     triggers spectre warnings without helping startup).
;;;   - Accept anything else of the form "<name><ws><value>".
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_parseFc(fcPath)
    prog((allText lines results stripped tokens nameTok valTok
          rawFirst)
        allText = safePatchNetlist_readAll(fcPath)
        unless(allText return(nil))
        lines = parseString(allText "\n")
        results = nil
        foreach(ln lines
            when(stringp(ln)
                ;; Drop windows-style CR
                stripped = pcreReplace(pcreCompile("\r") ln "" 0)
                when(and((strlen(stripped) > 0)
                         not(pcreMatchp("^[ \t]*#" stripped))
                         not(pcreMatchp("#unit A" stripped)))
                    tokens = parseString(stripped " \t")
                    when((length(tokens) >= 2)
                        nameTok = car(tokens)
                        valTok  = cadr(tokens)
                        when(and(stringp(nameTok) stringp(valTok)
                                 not(index(nameTok ":"))
                                 (strlen(nameTok) > 0))
                            results = cons(list(nameTok valTok) results)
                        )
                    )
                )
            )
        )
        return(reverse(results))
    )
)

;;; ----------------------------------------------------------------
;;; Look up a fc entry by name. Returns value-string or nil.
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_fcLookup(fcPairs name)
    prog((found)
        found = nil
        foreach(pair fcPairs
            when(and(not(found) equal(car(pair) name))
                found = cadr(pair)
            )
        )
        return(found)
    )
)

;;; ----------------------------------------------------------------
;;; Parse a spectre number literal ("0.800000473" / "5.2e-05" /
;;; "0" / "100m"). Returns a SKILL float, or nil if unparseable.
;;;
;;; We accept bare scientific / decimal notation + a limited set of
;;; engineering suffixes that Maestro's fc actually emits. fc values
;;; are usually plain decimal; suffixes are a defensive extra.
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_num(s)
    prog((raw suffix mult body ok)
        unless(stringp(s) return(nil))
        raw = s
        mult = 1.0
        suffix = nil
        ;; Peek last char; if letter, slice it off as suffix.
        when((strlen(raw) > 0)
            body = raw
            ok = t
            when(pcreMatchp("[a-zA-Z]$" raw)
                suffix = substring(raw strlen(raw) 1)
                body   = substring(raw 1 (strlen(raw) - 1))
                cond(
                    (equal(suffix "f") mult = 1.0e-15)
                    (equal(suffix "p") mult = 1.0e-12)
                    (equal(suffix "n") mult = 1.0e-9)
                    (equal(suffix "u") mult = 1.0e-6)
                    (equal(suffix "m") mult = 1.0e-3)
                    (equal(suffix "k") mult = 1.0e+3)
                    (equal(suffix "K") mult = 1.0e+3)
                    (equal(suffix "M") mult = 1.0e+6)
                    (equal(suffix "G") mult = 1.0e+9)
                    (t ok = nil)
                )
            )
            when(ok
                ok = nil
                errset(progn(raw = atof(body) ok = numberp(raw)))
                when(ok return(raw * mult))
            )
        )
        return(nil)
    )
)

;;; ----------------------------------------------------------------
;;; Build the replacement ic statement. Returns the full line
;;; *without* trailing newline.
;;;
;;; Logic:
;;;   1. Walk fcPairs. Any name NOT in perturbNames, and with a
;;;      parseable numeric value, becomes a "name=value" token
;;;      using the ORIGINAL fc string (preserves precision).
;;;   2. For each perturbNode (name offset_mV), emit
;;;      "name=<V_cm + offset/1000>".
;;;   3. V_cm is the mean of the two first perturb_nodes' fc
;;;      values when both are present; otherwise vCmHint.
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_buildIcLine(fcPairs perturbNodes vCmHint)
    prog((perturbNames biasTokens perturbTokens vcm v1 v2 n1 n2
          valStr floatVal fmtStr out)
        perturbNames = nil
        foreach(pn perturbNodes
            perturbNames = cons(car(pn) perturbNames)
        )

        ;; Compute V_cm: mean of first two perturb nodes' fc values
        ;; when present. Otherwise fall back to the hint.
        vcm = vCmHint
        when((length(perturbNodes) >= 2)
            n1 = car(car(perturbNodes))
            n2 = car(cadr(perturbNodes))
            v1 = safePatchNetlist_num(safePatchNetlist_fcLookup(fcPairs n1))
            v2 = safePatchNetlist_num(safePatchNetlist_fcLookup(fcPairs n2))
            when(and(v1 v2)
                vcm = (v1 + v2) / 2.0
            )
        )

        biasTokens = nil
        foreach(pair fcPairs
            unless(member(car(pair) perturbNames)
                valStr = cadr(pair)
                floatVal = safePatchNetlist_num(valStr)
                when(floatVal
                    biasTokens = cons(
                        strcat(car(pair) "=" valStr) biasTokens
                    )
                )
            )
        )
        biasTokens = reverse(biasTokens)

        perturbTokens = nil
        foreach(pn perturbNodes
            floatVal = vcm + (cadr(pn) / 1000.0)
            ;; Use sprintf with a fixed-precision format so spectre
            ;; sees a plain decimal literal (no "e" notation) — that
            ;; keeps the ic line close to the hand-written style.
            fmtStr = sprintf(nil "%.6f" floatVal)
            perturbTokens = cons(
                strcat(car(pn) "=" fmtStr) perturbTokens
            )
        )
        perturbTokens = reverse(perturbTokens)

        out = strcat("ic " buildString(perturbTokens " "))
        when(biasTokens
            out = strcat(out " " buildString(biasTokens " "))
        )
        return(list(out vcm length(biasTokens)))
    )
)

;;; ----------------------------------------------------------------
;;; Rewrite the ic line in input.scs. Returns t/nil.
;;;
;;; Strategy:
;;;   - Read all of input.scs as a list of raw lines.
;;;   - Scan for the first line matching "^ic ". Replace its full
;;;     content with newIcLine.
;;;   - If no ic line exists, INSERT one right before the first
;;;     "tran " line so ic takes effect on the transient.
;;;   - Write back to input.scs (non-atomic; acceptable for a
;;;     local dev flow, and a failure mid-write would show up as
;;;     spectre parse error on the next run).
;;; ----------------------------------------------------------------
procedure(safePatchNetlist_rewriteScs(scsPath newIcLine)
    prog((allText lines outLines replaced inserted port)
        allText = safePatchNetlist_readAll(scsPath)
        unless(allText return(nil))
        lines = parseString(allText "\n")
        outLines = nil
        replaced = nil
        inserted = nil
        foreach(ln lines
            cond(
                (and(not(replaced) stringp(ln)
                     pcreMatchp("^ic[ \t]" ln))
                 replaced = t
                 outLines = cons(newIcLine outLines)
                )
                (and(not(replaced) not(inserted) stringp(ln)
                     pcreMatchp("^tran[ \t]" ln))
                 ;; No prior ic line — insert one before tran.
                 inserted = t
                 outLines = cons(ln (cons(newIcLine outLines)))
                )
                (t outLines = cons(ln outLines))
            )
        )
        outLines = reverse(outLines)

        port = nil
        errset(port = outfile(scsPath "w"))
        unless(port return(nil))
        foreach(ln outLines
            fprintf(port "%s\n" ln)
        )
        close(port)
        return(or(replaced inserted))
    )
)

;;; ----------------------------------------------------------------
;;; safePatchNetlistIC — top-level entry point (allow-listed).
;;;
;;; Validates inputs, runs the three-stage pipeline (parse fc →
;;; build ic line → rewrite scs), returns JSON status.
;;; ----------------------------------------------------------------
procedure(safePatchNetlistIC(scsPath fcPath perturbList vCmHint)
    prog((fcPairs icResult newLine vcm nbias rewrote pnClean
          perturbOne name off)
        unless(safePatchNetlist_validPath(scsPath)
            return("{\"ok\":false,\"error\":\"invalid scsPath\"}")
        )
        unless(safePatchNetlist_validPath(fcPath)
            return("{\"ok\":false,\"error\":\"invalid fcPath\"}")
        )
        unless(listp(perturbList)
            return("{\"ok\":false,\"error\":\"perturbList must be a list\"}")
        )
        unless(numberp(vCmHint)
            return("{\"ok\":false,\"error\":\"vCmHint must be numeric\"}")
        )

        ;; Validate every perturb entry before touching files.
        pnClean = nil
        foreach(pn perturbList
            unless(and(listp(pn) (length(pn) >= 2))
                return("{\"ok\":false,\"error\":\"perturb entry malformed\"}")
            )
            name = car(pn)
            off  = cadr(pn)
            unless(and(stringp(name) numberp(off)
                       safeOcean_validSigName(name))
                return("{\"ok\":false,\"error\":\"perturb name/offset invalid\"}")
            )
            pnClean = cons(list(name off) pnClean)
        )
        pnClean = reverse(pnClean)

        fcPairs = safePatchNetlist_parseFc(fcPath)
        unless(fcPairs
            return("{\"ok\":false,\"error\":\"spectre.fc unreadable or empty\"}")
        )

        icResult = safePatchNetlist_buildIcLine(fcPairs pnClean vCmHint)
        newLine = car(icResult)
        vcm     = cadr(icResult)
        nbias   = caddr(icResult)

        rewrote = safePatchNetlist_rewriteScs(scsPath newLine)
        unless(rewrote
            return("{\"ok\":false,\"error\":\"failed to rewrite input.scs\"}")
        )

        return(sprintf(nil
            "{\"ok\":true,\"numBiasNodes\":%d,\"numPerturb\":%d,\"vcmMeasured\":%g}"
            nbias length(pnClean) vcm))
    )
)

;;; ----------------------------------------------------------------
;;; Load-time canary: make sure pcre + the two external helpers we
;;; depend on are present. Emits a single CIW note; never raises.
;;; ----------------------------------------------------------------
let((canary)
    canary = nil
    when(and(fboundp('safeOcean_validSigName)
             fboundp('safeHelpers_jsonEscape))
        canary = t
    )
    printf("safe_patch_netlist.il loaded (canary=%s)\n"
           if(canary "ok" "WARN-missing-deps"))
)
