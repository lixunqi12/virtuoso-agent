;;; safe_ocean.il — Safely drive an OCEAN simulation from allow-listed inputs.
;;; Compatible with Cadence IC23.1 / IC6.1.8 SKILL (no SKILL++ syntax).
;;;
;;; Requires: helpers.il loaded first.
;;;
;;; Entry point (only one — keeps SafeBridge allow-list small):
;;;   safeOceanRun(libName cellName tbCellName varList analysisList
;;;                @optional (viewName "schematic") (tbViewName "schematic"))
;;;
;;;   varList      : list of (varName varValue) pairs, all validated through
;;;                  safeHelpers_validateParamValue. Both strings (e.g. "3K")
;;;                  and numbers accepted.
;;;   analysisList : list of analysis-name strings, each must be one of the
;;;                  safeOcean_allowedAnalyses below.
;;;
;;; Returns JSON:
;;;   {"ok":true, "resultsDir":"...", "varsApplied":N, "analyses":[...]}
;;;   {"ok":false, "error":"..."}
;;;
;;; Security guarantees:
;;;   - lib/cell/tbCell names go through safeSetParam_validateName.
;;;   - Every var name is rejected unless it passes
;;;     safeHelpers_validateParamName (whitelist in helpers.il).
;;;   - Every var value goes through safeHelpers_validateParamValue, so the
;;;     LLM cannot smuggle `load("/evil.il")` through a desVar value.
;;;   - Analysis type is checked against a local allow-list; arbitrary
;;;     OCEAN function names (run, save, hiOpenLib, ...) cannot be smuggled
;;;     through analysisList.
;;;   - This function is the ONLY place in the PC->remote host path that calls
;;;     `run()`; PC never concatenates raw OCEAN strings.

defvar(safeOcean_allowedAnalyses  '("tran" "ac" "dc" "noise" "xf" "stb"))


;;; ----------------------------------------------------------------
;;; Helper: check analysis name is in the allow-list.
;;; ----------------------------------------------------------------
procedure(safeOcean_validAnalysis(name)
    let((nameLower)
        when(stringp(name)
            nameLower = lowerCase(name)
            member(nameLower safeOcean_allowedAnalyses) != nil
        )
    )
)


;;; ----------------------------------------------------------------
;;; Run one analysis. Must be called inside an active OCEAN session.
;;; ----------------------------------------------------------------
procedure(safeOcean_invokeAnalysis(analysisName)
    let((nm)
        nm = lowerCase(analysisName)
        ;; Only call the pre-configured analysis; the actual tran/ac/dc
        ;; sweep setup must already live in the Maestro/testbench state.
        ;; We refuse to add new analyses from the LLM, because that would
        ;; require accepting arbitrary sweep ranges / output lists as
        ;; SKILL expressions.
        analysis(stringToSymbol(nm))
        t
    )
)


;;; ----------------------------------------------------------------
;;; Rev 6 B2 (2026-04-18): invoke one analysis WITH numeric kwargs
;;; parsed from Maestro's input.scs. Root cause fix for OCN-6038:
;;; under `design()` + `analysis('tran)` alone, OCEAN inherits no
;;; stop-time, so spectre completes with 0 errors but writes no tran
;;; psf — safeOceanDumpAll sees a ghost history and every metric is
;;; null. Forwarding ?stop "200n" (and any other numeric kwarg) via
;;; safeOceanListAnalyses makes the tran actually run for the window
;;; Maestro configured.
;;;
;;; Rev 11 (2026-04-19): the "numeric-only" kwarg gate silently
;;; dropped `skipdc=yes`, which is MANDATORY for circuits with a
;;; symmetric DC equilibrium (LC-VCO / SRAM / Schmitt). Without
;;; skipdc, spectre runs DC init and LC-VCO locks at the symmetric
;;; stable point — tran from there has no perturbation, VCO never
;;; oscillates. Added a small enum-kwarg whitelist below so
;;; `skipdc=yes|no` survives the ListAnalyses parser and reaches
;;; analysis('tran ?skipdc "yes").
;;;
;;; Safety: every kwargName must pass safeHelpers_validateParamName
;;; and every kwargValue must pass safeHelpers_validateParamValue OR
;;; the enum-kwarg allow-list below. We build an s-expression and
;;; pass it to eval() rather than evalstring()-ing a built-up SKILL
;;; source string, so a validator regression (if one ever slipped in)
;;; could at worst pass a bad literal as data — never as executable
;;; SKILL code. The list elements are:
;;;   ('analysis  (quote <typeSym>)  ?key1 "val1"  ?key2 "val2"  ...)
;;; ----------------------------------------------------------------

;;; ----------------------------------------------------------------
;;; safeOcean_isEnumKwarg(name val) — Rev 11 (2026-04-19)
;;;
;;; Explicit (name, value) whitelist of non-numeric kwargs that
;;; analysis() in OCEAN is allowed to receive from us. Keep this
;;; list MINIMAL — anything added here bypasses the numeric gate
;;; on BOTH validateParamValue checks downstream. Today only
;;; `skipdc=yes|no` is needed for LC-VCO startup; extend only when
;;; a concrete simulation issue demands it.
;;; ----------------------------------------------------------------
procedure(safeOcean_isEnumKwarg(name val)
    when(and(stringp(name) stringp(val))
        cond(
            (and(equal(lowerCase(name) "skipdc")
                 or(equal(val "yes") equal(val "no")))
                t)
            (t nil)
        )
    )
)

procedure(safeOcean_invokeAnalysisSpec(analysisName kwargs)
    let((exprList kwSym nm k v)
        unless(safeOcean_validAnalysis(analysisName)
            error("safeOcean_invokeAnalysisSpec: analysis type not in allow-list"))
        nm = lowerCase(analysisName)
        exprList = list('analysis list('quote stringToSymbol(nm)))
        foreach(kw kwargs
            k = car(kw)
            v = cadr(kw)
            unless(and(stringp(k) safeHelpers_validateParamName(k))
                error("safeOcean_invokeAnalysisSpec: invalid kwarg name"))
            unless(or(safeHelpers_validateParamValue(v)
                     safeOcean_isEnumKwarg(k v))
                error("safeOcean_invokeAnalysisSpec: invalid kwarg value"))
            kwSym = stringToSymbol(strcat("?" k))
            exprList = append(exprList list(kwSym v))
        )
        eval(exprList)
        t
    )
)


;;; ----------------------------------------------------------------
;;; Main entry point.
;;; ----------------------------------------------------------------
procedure(safeOceanRun(libName cellName tbCellName varList analysisList
                      @optional (viewName "schematic") (tbViewName "schematic"))
    prog((errors appliedCount analysesJson varsJson resultsDir a aLower
          varName varVal varNameLower sessionName)

        ;; ---- Validate lib/cell/tbCell names ----
        unless(safeSetParam_validateName(libName "lib")
            return("{\"ok\":false,\"error\":\"Invalid library name\"}")
        )
        unless(safeSetParam_validateName(cellName "cell")
            return("{\"ok\":false,\"error\":\"Invalid cell name\"}")
        )
        unless(safeSetParam_validateName(tbCellName "tbCell")
            return("{\"ok\":false,\"error\":\"Invalid testbench cell name\"}")
        )

        ;; ---- Validate every analysis entry ----
        ;; Accepts either a bare string "tran" or a (name kwargs) list such as
        ;; ("tran" (("stop" "200n") ("maxiters" "5"))). The execution loop
        ;; below dispatches on type via cond(), so the validation loop must
        ;; agree — otherwise list-typed entries get rejected as
        ;; "not allowed" even when the name itself is legal.
        errors = nil
        foreach(a analysisList
            cond(
                (stringp(a)
                    unless(safeOcean_validAnalysis(a)
                        errors = cons(sprintf(nil "Analysis '%L' is not allowed" a) errors)
                    )
                )
                (and(listp(a) (length(a) >= 1) stringp(car(a)))
                    unless(safeOcean_validAnalysis(car(a))
                        errors = cons(sprintf(nil "Analysis type '%L' not allowed" car(a)) errors)
                    )
                )
                (t
                    errors = cons(sprintf(nil "Malformed analysis entry: %L" a) errors)
                )
            )
        )

        ;; ---- Validate every (varName varValue) pair ----
        foreach(pair varList
            varName = car(pair)
            varVal  = cadr(pair)
            unless(stringp(varName)
                errors = cons(sprintf(nil "Var name must be a string, got %L" varName) errors)
            )
            when(stringp(varName)
                varNameLower = lowerCase(varName)
                unless(safeHelpers_validateParamName(varNameLower)
                    errors = cons(sprintf(nil "Var '%s' not in whitelist" varName) errors)
                )
                unless(safeHelpers_validateParamValue(varVal)
                    errors = cons(sprintf(nil "Unsafe value for var '%s': %L" varName varVal) errors)
                )
            )
        )

        when(errors
            errors = reverse(errors)
            return(strcat(
                "{\"ok\":false,\"error\":\""
                safeHelpers_jsonEscape(buildString(errors "; "))
                "\"}"
            ))
        )

        ;; ---- Ensure the schematic under optimization is saved. ----
        ;; If the caller previously mutated instance params via
        ;; safeSetParam, dbSave was already invoked — this pass reopens
        ;; and re-saves defensively so OCEAN sees the latest netlist.
        ;; (Root cause of prior "run() returns nil" bug.)
        let((cv)
            cv = dbOpenCellViewByType(libName cellName viewName nil "a")
            when(cv
                schCheck(cv)
                dbSave(cv)
                dbClose(cv)
            )
        )

        ;; ---- Build OCEAN session on the testbench cell ----
        ;; simulator/design must be set BEFORE desVar.
        simulator('spectre)
        design(libName tbCellName tbViewName)

        ;; ---- Apply each design variable ----
        appliedCount = 0
        foreach(pair varList
            varName = car(pair)
            varVal  = cadr(pair)
            when(numberp(varVal)
                varVal = sprintf(nil "%.12g" varVal)
            )
            desVar(varName varVal)
            appliedCount = appliedCount + 1
        )

        ;; ---- Invoke every allow-listed analysis ----
        ;; Rev 6 B2 (2026-04-18): each element of analysisList may now
        ;; be EITHER a plain string (legacy fallback, no kwargs —
        ;; equivalent to `analysis('tran)`), OR a (typeName kwargsList)
        ;; tuple produced from safeOceanListAnalyses, where kwargsList
        ;; is a list of (key value) string pairs. This is how the
        ;; stop-time / maxiters / strobeperiod that Maestro wrote into
        ;; input.scs actually reach OCEAN.
        foreach(a analysisList
            cond(
                (stringp(a)
                    safeOcean_invokeAnalysis(a))
                (and(listp(a) (length(a) >= 1) stringp(car(a)))
                    safeOcean_invokeAnalysisSpec(car(a) cadr(a)))
                (t
                    error("safeOceanRun: analysisList entry is neither a string nor (name kwargs) list"))
            )
        )

        ;; ---- NOTE (rev 6 2026-04-18): prior rev 5 tried
        ;; `errset(save('allpub))` here; that OCEAN call is illegal —
        ;; `save` only accepts v/i/allv/alli/all. `allpub` is a
        ;; spectre netlist option, not an OCEAN arg, and was silently
        ;; swallowed by errset. It was also unnecessary: Maestro's
        ;; testbench state already writes `saveOptions options save=allpub`
        ;; into input.scs, and `design()` inherits it. The real cause
        ;; of prior all-null dumps was a missing desVar set (see
        ;; safeOceanListDesignVars below); this block intentionally
        ;; does nothing save-related now.
        ;;
        ;; Rev 12 (2026-04-20): but save=allpub in the netlist only
        ;; covers TOP-LEVEL signals; subcircuit internal MOS terminal
        ;; currents (e.g. /I0/M2/D where M2 lives inside the LC_VCO
        ;; cell instantiated as I0) are NOT in the PSF, so
        ;; IT("/I0/M2/D") returns nil inside safeOceanDumpAll and every
        ;; current-kind metric comes back UNMEASURABLE. Augment via
        ;; OCEAN's save('alli) which IS in the allow-list above. This
        ;; is additive to the netlist's saveOptions and only touches
        ;; the current session — no Maestro state mutation. Errset-
        ;; wrapped because a future OCEAN version that rejects 'alli
        ;; must not abort the run.
        errset(save('alli))

        ;; ---- Run. run() returns the history name on success, nil on
        ;; failure (e.g. if the schematic did not save cleanly). The
        ;; prior VCO debug session traced a silent "flat waveform" to
        ;; run() returning nil; surface that instead of ok:true. ----
        let((runResult)
            runResult = nil
            unless(errset(runResult = run())
                return("{\"ok\":false,\"error\":\"OCEAN run() raised\"}")
            )
            unless(runResult
                return("{\"ok\":false,\"error\":\"OCEAN run() returned nil (no history). Verify analyses are configured and schematic saved.\"}")
            )
        )

        ;; ---- Stage 1 rev 5/6 (2026-04-18): pop up waveform viewer on
        ;; remote host after every successful run (user request). Iterates
        ;; the saved outputs() list, builds VT() waveforms, and calls
        ;; plot() with the full list so each iteration visually opens
        ;; on the Virtuoso X11 display. Every step errset-wrapped so
        ;; a missing display or plot failure never taints the JSON
        ;; return (measurement extraction still runs downstream).
        ;;
        ;; Rev 6 fix (2026-04-18 smoke-test): must call openResults +
        ;; selectResult BEFORE outputs() — otherwise OCN-6034 fires and
        ;; outputs() returns nil, and this whole block silently does
        ;; nothing. safe_ocean's own rev 5 version skipped this step
        ;; and the waveform never appeared under agent control.
        let((outs w wlist resDir)
            resDir = nil
            errset(resDir = asiGetResultsDir(asiGetCurrentSession()))
            when(stringp(resDir)
                errset(openResults(resDir))
            )
            errset(selectResult('tran))
            outs = nil
            errset(outs = outputs())
            wlist = nil
            when(outs
                foreach(oname outs
                    w = nil
                    errset(w = VT(oname))
                    when(w
                        wlist = cons(w wlist)
                    )
                )
            )
            when(wlist
                errset(apply('plot reverse(wlist)))
            )
        )

        ;; Best-effort capture of results directory. Uses the canonical
        ;; asiGetResultsDir(asiGetCurrentSession()) — same call the
        ;; virtuoso_bridge open_maestro_gui_with_history() uses. Drops
        ;; the earlier getResultsDir() guess (not a documented API).
        ;; PC-side _scrub() still runs on this field as second defense.
        resultsDir = "<results>"
        errset(resultsDir = asiGetResultsDir(asiGetCurrentSession()))
        unless(stringp(resultsDir)
            resultsDir = "<results>"
        )

        ;; ---- Build JSON response ----
        analysesJson = safeHelpers_listToJsonArray(analysisList)
        varsJson     = sprintf(nil "%d" appliedCount)
        return(strcat(
            "{\"ok\":true,"
            "\"resultsDir\":\"" safeHelpers_jsonEscape(resultsDir) "\","
            "\"varsApplied\":" varsJson ","
            "\"analyses\":" analysesJson
            "}"
        ))
    )
)


;;; ----------------------------------------------------------------
;;; safeOceanMeasure — Stage 1 rev 3 (2026-04-18)
;;;
;;; Extract the 7 LC_VCO metrics (spec §3) directly from the OCEAN
;;; transient history that safeOceanRun() just populated. Must be
;;; called AFTER safeOceanRun() in the same OCEAN session; it uses
;;; the current session's waveforms via VT()/IT().
;;;
;;; Prior stage 1 rev 1/2 had NO measurement extraction — the agent
;;; was asking the LLM to guess the 7 numbers from circuit params,
;;; which produced all-zero measurements and SAFEGUARD aborts on
;;; every run. This procedure closes that gap.
;;;
;;; Security:
;;;   - dutPath is validated against the instance-path pattern below
;;;     (safeOcean_validInstPath). Anything weird is rejected.
;;;   - Every metric computation is wrapped in errset so a failed
;;;     OCEAN call on one metric cannot abort the rest.
;;;   - NaN / Inf / nil collapse to JSON null via safeHelpers_numToJson.
;;;
;;; Returns JSON (keys match LC_VCO_spec.md §5 example verbatim so the
;;; LLM sees one name space across spec + sim data):
;;;   {"ok":true,"metrics":{
;;;       "f_osc_GHz":..., "V_diff_pp_V":..., "V_cm_V":...,
;;;       "duty_cycle_pct":..., "amp_hold_ratio":...,
;;;       "t_startup_ns":..., "I_core_uA":...
;;;   }}
;;;   {"ok":false,"error":"..."}
;;;
;;; Windows match LC_VCO_spec.md §3:
;;;   - f_osc       : freq of vdiff over 100e-9 .. 200e-9
;;;   - V_diff_pp   : ymax-ymin over 150e-9 .. 200e-9
;;;   - V_cm        : avg((vp+vn)/2) over 150e-9 .. 200e-9
;;;   - duty_cycle  : dutyCycle(vdiff) over 150e-9 .. 200e-9, ×100
;;;   - amp_hold    : rms(150..200ns) / rms(75..125ns)
;;;   - t_startup   : first rising crossing of |vdiff| at 0.9*V_diff_pp
;;;                   over 0..200ns (ns)
;;;   - I_core      : avg |I(dutPath/M2/D)| over 150..200ns (µA)
;;; ----------------------------------------------------------------

procedure(safeOcean_validInstPath(p)
    ;; Accept "/I0", "/I0/M2", etc. Leading slash, segments of
    ;; [A-Za-z_][A-Za-z0-9_]*, at most 6 segments, <=64 chars total.
    ;; Rejects spaces, parens, quotes, backslashes, and pipe chars.
    ;; M4 (2026-04-18 review): explicit strlen cap before regex so
    ;; the pcreMatchp engine never sees a pathological long input;
    ;; parity with the PC-side `len(dut_path) > 64` pre-check.
    ;; M4 rev2 (2026-04-18 round-2 review): use `if` instead of
    ;; `when(... return(nil))`. `return()` is only defined inside
    ;; `prog`/`for`/`foreach` on IC23.1; inside a bare `procedure`
    ;; it either silently evaluates to nil (letting pcreMatchp run
    ;; anyway — M4 defeated) or throws. `if` short-circuits
    ;; cleanly without adding `prog` overhead to a one-liner.
    when(stringp(p)
        if(strlen(p) > 64
            nil
            pcreMatchp("^(/[A-Za-z_][A-Za-z0-9_]*){1,6}\\z" p)
        )
    )
)

procedure(safeOcean_safeNum(w)
    ;; Coerce an OCEAN scalar (waveform or number) to a plain float
    ;; or nil. Wrap everything in errset; value() on nil or an
    ;; unresolved waveform throws on some IC releases.
    let((v)
        v = nil
        errset(v = value(w))
        cond(
            (numberp(v)   v)
            (t            nil)
        )
    )
)

procedure(safeOceanMeasure(@optional (dutPath "/I0"))
    prog((vp vn vdiff vabs vpp vcm fOsc duty ampHold ibias tStartup
          wEarly wLate wFull jsonMetrics)
        unless(safeOcean_validInstPath(dutPath)
            return("{\"ok\":false,\"error\":\"Invalid dutPath\"}")
        )

        ;; ---- Full differential waveform ----
        ;; 2026-04-18 empirical fix: pll/LC_VCO_tb labels the two
        ;; output nets as `/Vout_p` and `/Vout_n` (leading "V"),
        ;; not `/out_p` / `/out_n` as spec §2 originally assumed.
        ;; Verified by Maestro Results waveform legend on the first
        ;; real remote host run — rename here rather than re-labelling the
        ;; schematic to avoid touching the user's DUT wiring.
        errset(vp    = VT("/Vout_p"))
        errset(vn    = VT("/Vout_n"))
        errset(vdiff = vp - vn)
        errset(vabs  = abs(vdiff))

        ;; Bail early if the transient output is missing. PC-side
        ;; will see ok:false and surface it — better than returning
        ;; a JSON of nulls that looks like a legitimate failed run.
        unless(vdiff
            return("{\"ok\":false,\"error\":\"VT(/Vout_p)-VT(/Vout_n) unavailable; check analysis saved outputs\"}")
        )

        ;; ---- Windows ----
        errset(wFull  = clip(vdiff 100e-9 200e-9))
        errset(wLate  = clip(vdiff 150e-9 200e-9))
        errset(wEarly = clip(vdiff  75e-9 125e-9))

        ;; ---- f_osc (Hz -> GHz) ----
        fOsc = nil
        errset(fOsc = safeOcean_safeNum(frequency(wFull)))
        when(numberp(fOsc)
            fOsc = fOsc / 1.0e9
        )

        ;; ---- vpp (V) over late window ----
        vpp = nil
        let((ymx ymn)
            ymx = nil
            ymn = nil
            errset(ymx = safeOcean_safeNum(ymax(wLate)))
            errset(ymn = safeOcean_safeNum(ymin(wLate)))
            when(and(numberp(ymx) numberp(ymn))
                vpp = ymx - ymn
            )
        )

        ;; ---- vcm (V) = average((vp+vn)/2) over late window ----
        vcm = nil
        let((wsum wcm)
            errset(wsum = (vp + vn) / 2.0)
            when(wsum
                errset(wcm = clip(wsum 150e-9 200e-9))
                errset(vcm = safeOcean_safeNum(average(wcm)))
            )
        )

        ;; ---- duty (%) ----
        duty = nil
        errset(duty = safeOcean_safeNum(dutyCycle(wLate)))
        when(numberp(duty)
            duty = duty * 100.0
        )

        ;; ---- amp_hold_ratio = rms(late)/rms(early) ----
        ampHold = nil
        let((rmsL rmsE)
            rmsL = nil
            rmsE = nil
            errset(rmsL = safeOcean_safeNum(rms(wLate)))
            errset(rmsE = safeOcean_safeNum(rms(wEarly)))
            when(and(numberp(rmsL) numberp(rmsE) (rmsE > 1.0e-12))
                ampHold = rmsL / rmsE
            )
        )

        ;; ---- t_startup (ns) — first rising crossing of |vdiff| at
        ;; 0.9*V_diff_pp over 0..200ns. Spec §3 metric 6. ----
        ;; Rename of the earlier "edge_count" probe: §3 never asked for
        ;; a cycle count; it asked for the time the envelope first
        ;; reaches 0.9*V_diff_pp(final). OCEAN cross(..., 1, 'rising)
        ;; returns the first crossing time in seconds — convert to ns.
        ;; M3 (2026-04-18 review): this let() now scopes ONLY the
        ;; tStartup temporaries (thr/waFull/n); tStartup itself is a
        ;; prog slot so the JSON assembly below can read it. I_core
        ;; and JSON assembly moved back to prog top-level.
        tStartup = nil
        let((thr waFull n)
            when(and(numberp(vpp) (vpp > 0.0))
                thr = 0.9 * vpp
                errset(waFull = clip(vabs 0.0 200e-9))
                when(waFull
                    errset(n = safeOcean_safeNum(cross(waFull thr 1 'rising)))
                    when(numberp(n)
                        tStartup = n * 1.0e9
                    )
                )
            )
        )

        ;; ---- I_core (µA) = mean |I(dutPath/M2/D)| over last 50 ns ----
        ;; M2 = tail device per LC_VCO_spec.md §1 topology; window
        ;; matches §3 metric 7 (last 50 ns of 200 ns tran).
        ibias = nil
        let((itail iabs iwin iavg probePath)
            probePath = strcat(dutPath "/M2/D")
            errset(itail = IT(probePath))
            when(itail
                errset(iabs = abs(itail))
                errset(iwin = clip(iabs 150e-9 200e-9))
                when(iwin
                    errset(iavg = safeOcean_safeNum(average(iwin)))
                    when(numberp(iavg)
                        ibias = iavg * 1.0e6
                    )
                )
            )
        )

        ;; ---- Assemble JSON. Key names mirror LC_VCO_spec.md §5
        ;; example so the LLM sees one namespace across spec +
        ;; measurement payload. ----
        jsonMetrics = strcat(
            "{"
            "\"f_osc_GHz\":"      safeHelpers_numToJson(fOsc)     ","
            "\"V_diff_pp_V\":"    safeHelpers_numToJson(vpp)      ","
            "\"V_cm_V\":"         safeHelpers_numToJson(vcm)      ","
            "\"duty_cycle_pct\":" safeHelpers_numToJson(duty)     ","
            "\"amp_hold_ratio\":" safeHelpers_numToJson(ampHold)  ","
            "\"t_startup_ns\":"   safeHelpers_numToJson(tStartup) ","
            "\"I_core_uA\":"      safeHelpers_numToJson(ibias)
            "}"
        )
        return(strcat("{\"ok\":true,\"metrics\":" jsonMetrics "}"))
    )
)


;;; ================================================================
;;; Stage 1 rev 4 (2026-04-18) — generic dump + primitive-op API
;;;
;;; Replaces the LC_VCO-specific 7-metric `safeOceanMeasure` with two
;;; circuit-agnostic primitives. `safeOceanMeasure` is kept above for
;;; now (backward compat, legacy callers); new flow is:
;;;
;;;   safeOceanDumpAll(signalList windowList)
;;;     → per-signal / per-window JSON of mean/min/max/ptp/rms/mean_abs/
;;;       freq_Hz/duty_pct. No knowledge of oscillator semantics.
;;;
;;;   safeOceanTCross(signalKind signalPaths threshold
;;;                   crossStart crossEnd direction useAbs)
;;;     → first threshold-crossing time (s). Used for compound metrics
;;;       like t_startup where threshold = frac * ptp(other window),
;;;       computed on PC and passed back in verbatim.
;;;
;;; Architecture: per memory note "generic circuit design flow, NOT a
;;; VCO-specific flow". Spec YAML (signals/windows/metrics) declares
;;; what to probe and how to judge; PC evaluator (src/spec_evaluator.py)
;;; applies the declaration. SKILL now only exposes primitives.
;;; ================================================================

defvar(safeOcean_allowedSignalKinds
    '("V" "I" "Vdiff" "Vsum_half"))
defvar(safeOcean_allowedCrossDirections
    '("rising" "falling" "either"))

procedure(safeOcean_validSigName(n)
    ;; name for a signal or window: [A-Za-z_][A-Za-z0-9_]*, <=32 chars.
    when(stringp(n)
        if(strlen(n) > 32
            nil
            pcreMatchp("^[A-Za-z_][A-Za-z0-9_]*\\z" n)
        )
    )
)

procedure(safeOcean_validProbePath(p)
    ;; Probe path for VT/IT: leading slash, 1..8 segments of [A-Za-z_][A-Za-z0-9_]*,
    ;; total len <= 128. Mirrors Python-side _DUT_PATH_RE but more permissive
    ;; on segment count because device-terminal paths like /I0/M2/D need
    ;; 3 segments and may go deeper on hierarchical DUTs.
    when(stringp(p)
        if(strlen(p) > 128
            nil
            pcreMatchp("^(/[A-Za-z_][A-Za-z0-9_]*){1,8}\\z" p)
        )
    )
)

procedure(safeOcean_validKind(k)
    when(stringp(k)
        member(k safeOcean_allowedSignalKinds) != nil
    )
)

procedure(safeOcean_buildWaveform(kind paths)
    ;; Build an OCEAN waveform from a (kind, pathList) pair.
    ;;   V          : one path          → VT(path)
    ;;   I          : one path          → IT(path)
    ;;   Vdiff      : two paths         → VT(p1) - VT(p2)
    ;;   Vsum_half  : two paths         → (VT(p1) + VT(p2)) / 2
    ;; Every VT/IT/arithmetic call is errset-wrapped so a missing save
    ;; or a malformed path returns nil instead of aborting the session.
    let((w p1 p2 w1 w2)
        w = nil
        cond(
            (equal(kind "V")
                when(listp(paths) && length(paths) == 1
                    p1 = car(paths)
                    when(safeOcean_validProbePath(p1)
                        errset(w = VT(p1))
                    )
                )
            )
            (equal(kind "I")
                when(listp(paths) && length(paths) == 1
                    p1 = car(paths)
                    when(safeOcean_validProbePath(p1)
                        errset(w = IT(p1))
                        ;; Rev 13 (2026-04-20): spectre PSF stores
                        ;; subcircuit internal terminal currents as
                        ;; "I0.M2:d" (dot for hierarchy, colon before
                        ;; lowercased terminal), not "/I0/M2/D". When
                        ;; IT() returns nil on the slash form, retry
                        ;; with the translated form. Top-level paths
                        ;; like /I0/Ctrl still succeed on the first
                        ;; IT() and skip this branch, so behavior is
                        ;; unchanged for already-working probes. This
                        ;; is a generic spectre convention, not an
                        ;; LC_VCO-specific patch.
                        unless(w
                            let((segs cleanSegs nSeg tailTok bodyStr altPath)
                                segs = parseString(p1 "/")
                                cleanSegs = nil
                                foreach(s segs
                                    when(and(stringp(s) strlen(s) > 0)
                                        cleanSegs = cons(s cleanSegs)
                                    )
                                )
                                cleanSegs = reverse(cleanSegs)
                                nSeg = length(cleanSegs)
                                when(nSeg >= 2
                                    tailTok = lowerCase(nth(nSeg - 1 cleanSegs))
                                    bodyStr = buildString(
                                        reverse(cdr(reverse(cleanSegs)))
                                        ".")
                                    altPath = strcat(bodyStr ":" tailTok)
                                    errset(w = IT(altPath))
                                )
                            )
                        )
                    )
                )
            )
            (equal(kind "Vdiff")
                when(listp(paths) && length(paths) == 2
                    p1 = car(paths)
                    p2 = cadr(paths)
                    when(safeOcean_validProbePath(p1) && safeOcean_validProbePath(p2)
                        w1 = nil
                        w2 = nil
                        errset(w1 = VT(p1))
                        errset(w2 = VT(p2))
                        when(w1 && w2
                            errset(w = w1 - w2)
                        )
                    )
                )
            )
            (equal(kind "Vsum_half")
                when(listp(paths) && length(paths) == 2
                    p1 = car(paths)
                    p2 = cadr(paths)
                    when(safeOcean_validProbePath(p1) && safeOcean_validProbePath(p2)
                        w1 = nil
                        w2 = nil
                        errset(w1 = VT(p1))
                        errset(w2 = VT(p2))
                        when(w1 && w2
                            errset(w = (w1 + w2) / 2.0)
                        )
                    )
                )
            )
        )
        w
    )
)

procedure(safeOcean_nullStatsJson()
    ;; JSON fragment for a window where the waveform is missing.
    "{\"mean\":null,\"min\":null,\"max\":null,\"ptp\":null,\"rms\":null,\"mean_abs\":null,\"freq_Hz\":null,\"duty_pct\":null}"
)


;;; ----------------------------------------------------------------
;;; safeOcean_firstAfter(t0 timeList) — Rev 12 (2026-04-20)
;;;
;;; Returns the first element of `timeList` that is strictly greater
;;; than t0, or nil if none. timeList is assumed sorted ascending
;;; (which OCEAN cross() already guarantees). Used by duty-cycle pair
;;; matching below.
;;; ----------------------------------------------------------------
procedure(safeOcean_firstAfter(t0 timeList)
    prog((tF)
        foreach(tF timeList
            when(and(numberp(tF) (tF > t0))
                return(tF)
            )
        )
        return(nil)
    )
)


;;; ----------------------------------------------------------------
;;; safeOcean_collectCross(wc thresh dir maxN) — Rev 15 (2026-04-20)
;;;
;;; Return a Lisp list of crossing times (seconds), ascending.
;;;
;;; Rev 14 looped cross(wc thresh i dir) for i=1..maxN. Each call
;;; rescans the whole waveform to find the i-th crossing, making
;;; the helper O(maxN * N_samples). On noisy VCO outputs with
;;; near-mean wiggle (many real + noise crossings) maxN=2000 was
;;; saturated and a single dutyFromCross() call blew the 60 s
;;; OCEAN wall budget (see run_20260420_211211 iter 2-5).
;;;
;;; Rev 15 calls cross(... t) once. OCEAN does a single O(N_samples)
;;; scan and returns a drWaveform whose X = crossing index (1..N)
;;; and Y = crossing time. We then use xmax() to learn N and
;;; value(crossWave i) (O(log N) bisection inside the small
;;; crossing waveform) to read each crossing. Net cost drops from
;;; O(maxN * N_samples) to O(N_samples + N_cross * log N_cross).
;;; maxN still caps the returned list for safety.
;;; ----------------------------------------------------------------
procedure(safeOcean_collectCross(wc thresh dir maxN)
    prog((crossWave xm n_cross result i t_i)
        crossWave = nil
        errset(crossWave = cross(wc thresh t dir))
        unless(crossWave return(nil))
        ;; X-axis = crossing index (1..N); Y-axis = crossing time.
        xm = nil
        errset(xm = xmax(crossWave))
        unless(numberp(xm) return(nil))
        n_cross = fix(xm)
        when(n_cross > maxN  n_cross = maxN)
        when(n_cross < 1 return(nil))
        result = nil
        for(i 1 n_cross
            t_i = nil
            errset(t_i = value(crossWave float(i)))
            when(numberp(t_i)
                result = cons(t_i result)
            )
        )
        return(reverse(result))
    )
)


;;; ----------------------------------------------------------------
;;; safeOcean_dutyFromCross(w tStart tEnd) — Rev 16 (2026-04-20)
;;;
;;; Duty cycle in percent over [tStart, tEnd], computed from mean-
;;; threshold crossings rather than OCEAN's dutyCycle() (which
;;; returns nil for DC-offset signals).
;;;
;;; History of failures:
;;;   Rev 14: looped cross(wc thresh N dir) for N=1..2000 — O(N^2)
;;;           pairing via firstAfter took 7+ s/call at 20 GHz,
;;;           blowing the 60 s OceanWorker budget.
;;;   Rev 15: switched to cross(wc thresh t dir) drWaveform.
;;;           Returned nil on EVERY signal in run 213721 — the
;;;           cross(... t ...) form either errors under errset or
;;;           returns a drWaveform whose axis semantics differ from
;;;           the assumed X=index/Y=time (xmax→fix→0 → bail).
;;;
;;; Rev 16: uses ONLY the proven cross(wc thresh N dir) form with
;;; N = 1..4 (3-4 individual calls).  For a periodic signal, the
;;; duty cycle is fully determined by the average period from the
;;; first two rising crossings and the positive-half duration from
;;; the first rise→fall pair.  Total cost: 3-4 OCEAN cross() calls
;;; ≈ O(4 * N_samples), ~50 ms at 22 GHz / 200 ns.
;;; Returns nil if < 2 rising crossings (DC or sub-Nyquist).
;;; ----------------------------------------------------------------
procedure(safeOcean_dutyFromCross(w tStart tEnd)
    prog((wc meanVal firstRise secondRise firstFall secondFall
          firstFallAfterRise period posTime)
        wc = nil
        errset(wc = clip(w tStart tEnd))
        unless(wc return(nil))
        meanVal = nil
        errset(meanVal = safeOcean_safeNum(average(wc)))
        unless(numberp(meanVal) return(nil))

        ;; 1st and 2nd rising crossings → one-period span
        firstRise = nil
        errset(firstRise = cross(wc meanVal 1 'rising))
        unless(numberp(firstRise) return(nil))
        secondRise = nil
        errset(secondRise = cross(wc meanVal 2 'rising))
        unless(numberp(secondRise) return(nil))
        period = secondRise - firstRise
        when(period <= 0.0 return(nil))

        ;; First falling crossing after firstRise.
        ;; If waveform starts rising, cross(..1 'falling) > firstRise.
        ;; If it starts falling, the 1st fall precedes firstRise;
        ;; use the 2nd fall instead.
        firstFall = nil
        errset(firstFall = cross(wc meanVal 1 'falling))
        unless(numberp(firstFall) return(nil))
        if(firstFall > firstRise then
            firstFallAfterRise = firstFall
        else
            secondFall = nil
            errset(secondFall = cross(wc meanVal 2 'falling))
            unless(and(numberp(secondFall) secondFall > firstRise)
                return(nil))
            firstFallAfterRise = secondFall
        )

        ;; Positive-half duration; sanity: must fit within one period
        posTime = firstFallAfterRise - firstRise
        when(posTime <= 0.0 return(nil))
        when(posTime > period return(nil))
        return(100.0 * posTime / period)
    )
)

procedure(safeOcean_statsJson(w tStart tEnd)
    ;; Compute all stats on the waveform clipped to [tStart, tEnd].
    ;; Every call errset-wrapped; any failed stat becomes JSON null.
    prog((wc mn mx mean rmsVal ptp freq duty meanAbs wabs wabsClipped)
        mean    = nil
        mn      = nil
        mx      = nil
        rmsVal  = nil
        ptp     = nil
        freq    = nil
        duty    = nil
        meanAbs = nil
        wc      = nil
        fprintf(stderr "[statsJson] T=%s op=clip begin tStart=%L tEnd=%L\n" getCurrentTime() tStart tEnd)
        errset(wc = clip(w tStart tEnd))
        fprintf(stderr "[statsJson] T=%s op=clip end wc=%L\n" getCurrentTime() if(wc "ok" "nil"))
        when(wc
            fprintf(stderr "[statsJson] T=%s op=average begin\n" getCurrentTime())
            errset(mean   = safeOcean_safeNum(average(wc)))
            fprintf(stderr "[statsJson] T=%s op=average end\n" getCurrentTime())
            fprintf(stderr "[statsJson] T=%s op=ymin begin\n" getCurrentTime())
            errset(mn     = safeOcean_safeNum(ymin(wc)))
            fprintf(stderr "[statsJson] T=%s op=ymin end\n" getCurrentTime())
            fprintf(stderr "[statsJson] T=%s op=ymax begin\n" getCurrentTime())
            errset(mx     = safeOcean_safeNum(ymax(wc)))
            fprintf(stderr "[statsJson] T=%s op=ymax end\n" getCurrentTime())
            fprintf(stderr "[statsJson] T=%s op=rms begin\n" getCurrentTime())
            errset(rmsVal = safeOcean_safeNum(rms(wc)))
            fprintf(stderr "[statsJson] T=%s op=rms end\n" getCurrentTime())
            when(numberp(mx) && numberp(mn)
                ptp = mx - mn
            )
            fprintf(stderr "[statsJson] T=%s op=frequency begin\n" getCurrentTime())
            errset(freq = safeOcean_safeNum(frequency(wc)))
            fprintf(stderr "[statsJson] T=%s op=frequency end freq=%L\n" getCurrentTime() freq)
            ;; Rev 12 (2026-04-20): OCEAN's dutyCycle(wc) without an
            ;; explicit threshold returned nil for every signal (see
            ;; safeOcean_dutyFromCross docstring). Use our own helper
            ;; which centers on the window mean so signals with DC
            ;; offset (Vcm, Vout_p/n around 0.8 V) also yield a duty.
            fprintf(stderr "[statsJson] T=%s op=dutyFromCross begin\n" getCurrentTime())
            errset(duty = safeOcean_dutyFromCross(w tStart tEnd))
            fprintf(stderr "[statsJson] T=%s op=dutyFromCross end duty=%L\n" getCurrentTime() duty)
            wabs = nil
            fprintf(stderr "[statsJson] T=%s op=abs begin\n" getCurrentTime())
            errset(wabs = abs(w))
            fprintf(stderr "[statsJson] T=%s op=abs end wabs=%L\n" getCurrentTime() if(wabs "ok" "nil"))
            when(wabs
                wabsClipped = nil
                fprintf(stderr "[statsJson] T=%s op=clip_abs begin\n" getCurrentTime())
                errset(wabsClipped = clip(wabs tStart tEnd))
                fprintf(stderr "[statsJson] T=%s op=clip_abs end\n" getCurrentTime())
                when(wabsClipped
                    fprintf(stderr "[statsJson] T=%s op=average_abs begin\n" getCurrentTime())
                    errset(meanAbs = safeOcean_safeNum(average(wabsClipped)))
                    fprintf(stderr "[statsJson] T=%s op=average_abs end\n" getCurrentTime())
                )
            )
        )
        return(strcat(
            "{"
            "\"mean\":"     safeHelpers_numToJson(mean)    ","
            "\"min\":"      safeHelpers_numToJson(mn)      ","
            "\"max\":"      safeHelpers_numToJson(mx)      ","
            "\"ptp\":"      safeHelpers_numToJson(ptp)     ","
            "\"rms\":"      safeHelpers_numToJson(rmsVal)  ","
            "\"mean_abs\":" safeHelpers_numToJson(meanAbs) ","
            "\"freq_Hz\":"  safeHelpers_numToJson(freq)    ","
            "\"duty_pct\":" safeHelpers_numToJson(duty)
            "}"
        ))
    )
)

;;; ----------------------------------------------------------------
;;; Stage 1 rev 11 (2026-04-20, Bug 1) — selectResult handle hygiene.
;;;
;;; Best-effort unselectResult() call wrapped in errset so it never
;;; raises. Called at the entry of safeOceanDumpAll and
;;; safeOceanProbePtp to clear any stale selection left by a prior
;;; iter's hung call, and at exit to release the handle so the next
;;; RAMIC connection can progress without a 30 s connect timeout
;;; (see run_20260420_033152 log iters 2/4/6/9/10).
;;; ----------------------------------------------------------------
procedure(safeOcean_unselectIfHeld()
    errset(unselectResult())
    t
)


;;; ----------------------------------------------------------------
;;; Stage 1 rev 11 (2026-04-20, Bug 3) — oscillation gate probe.
;;;
;;; Reads ptp/mean of (VT(signalP) - VT(signalN)) over [tStart, tEnd]
;;; and returns a compact JSON payload. Intended as a <1 s precheck
;;; the agent runs BEFORE the heavy safeOceanDumpAll, so degenerate
;;; non-oscillating waveforms (Ibias too low, symmetric equilibrium
;;; trap) are identified without hanging 30 s inside the full dump
;;; path.
;;;
;;; Returns:
;;;   {"ok":true,"ptp_V":<float>,"mean_V":<float>}
;;;   {"ok":false,"error":"..."}
;;; ----------------------------------------------------------------
procedure(safeOceanProbePtp(signalP signalN tStart tEnd)
    prog((w1 w2 wdiff wc mnV mxV meanV ptpV)
        unless(stringp(signalP) && safeOcean_validProbePath(signalP)
            return("{\"ok\":false,\"error\":\"Bad signalP path\"}")
        )
        unless(stringp(signalN) && safeOcean_validProbePath(signalN)
            return("{\"ok\":false,\"error\":\"Bad signalN path\"}")
        )
        unless(numberp(tStart) && numberp(tEnd) && tEnd > tStart
            return("{\"ok\":false,\"error\":\"Bad time window\"}")
        )

        ;; Release any stale selectResult from a prior hung call.
        safeOcean_unselectIfHeld()
        errset(selectResult('tran))

        w1 = nil
        w2 = nil
        errset(w1 = VT(signalP))
        errset(w2 = VT(signalN))
        unless(w1 && w2
            safeOcean_unselectIfHeld()
            return("{\"ok\":false,\"error\":\"VT() returned nil; no tran history\"}")
        )
        wdiff = nil
        errset(wdiff = w1 - w2)
        unless(wdiff
            safeOcean_unselectIfHeld()
            return("{\"ok\":false,\"error\":\"VT(p1)-VT(p2) unavailable\"}")
        )
        wc = nil
        errset(wc = clip(wdiff tStart tEnd))
        unless(wc
            safeOcean_unselectIfHeld()
            return("{\"ok\":false,\"error\":\"clip() returned nil\"}")
        )
        mnV = nil
        mxV = nil
        meanV = nil
        errset(mnV   = safeOcean_safeNum(ymin(wc)))
        errset(mxV   = safeOcean_safeNum(ymax(wc)))
        errset(meanV = safeOcean_safeNum(average(wc)))
        ptpV = nil
        when(numberp(mnV) && numberp(mxV)
            ptpV = mxV - mnV
        )
        safeOcean_unselectIfHeld()
        return(strcat(
            "{"
            "\"ok\":true,"
            "\"ptp_V\":"  safeHelpers_numToJson(ptpV)  ","
            "\"mean_V\":" safeHelpers_numToJson(meanV)
            "}"
        ))
    )
)


procedure(safeOceanDumpAll(signalList windowList)
    ;; Generic dump primitive. No knowledge of oscillator semantics.
    ;;
    ;;   signalList : list of (sigName sigKind pathList)
    ;;                  sigKind ∈ safeOcean_allowedSignalKinds
    ;;   windowList : list of (winName tStart tEnd)
    ;;
    ;; Returns:
    ;;   {"ok":true,"dumps":{"<sig>":{"<win>":{stats}, ...}, ...}}
    ;;   {"ok":false,"error":"..."}
    ;;
    ;; Rev 11 (2026-04-20, Bug 1): call safeOcean_unselectIfHeld at
    ;; entry to clear stale selectResult handles from prior hung
    ;; iterations, and at every return path so the daemon's next
    ;; request is not serialized behind an orphaned selection. This
    ;; is the hygiene fix — the complementary defense is the
    ;; safeOceanProbePtp gate on the agent side.
    prog((errors sigEntry winEntry sigName sigKind sigPaths
          winName tStart tEnd w winFrag winParts sigFrag sigParts
          allJson)
        errors = nil

        safeOcean_unselectIfHeld()

        unless(listp(signalList)
            safeOcean_unselectIfHeld()
            return("{\"ok\":false,\"error\":\"signalList must be a list\"}")
        )
        unless(listp(windowList)
            safeOcean_unselectIfHeld()
            return("{\"ok\":false,\"error\":\"windowList must be a list\"}")
        )

        ;; Validate every signal entry shape before we run any sims.
        foreach(sigEntry signalList
            if(listp(sigEntry) && length(sigEntry) >= 3
                then
                    sigName  = nth(0 sigEntry)
                    sigKind  = nth(1 sigEntry)
                    sigPaths = nth(2 sigEntry)
                    unless(safeOcean_validSigName(sigName)
                        errors = cons(sprintf(nil "Bad signal name %L" sigName) errors)
                    )
                    unless(safeOcean_validKind(sigKind)
                        errors = cons(sprintf(nil "Bad signal kind %L" sigKind) errors)
                    )
                    unless(listp(sigPaths)
                        errors = cons("signal paths must be a list" errors)
                    )
                else
                    errors = cons("signal entry must be (name kind pathList)" errors)
            )
        )

        foreach(winEntry windowList
            if(listp(winEntry) && length(winEntry) == 3
                then
                    winName = nth(0 winEntry)
                    tStart  = nth(1 winEntry)
                    tEnd    = nth(2 winEntry)
                    unless(safeOcean_validSigName(winName)
                        errors = cons(sprintf(nil "Bad window name %L" winName) errors)
                    )
                    unless(numberp(tStart) && numberp(tEnd) && tEnd > tStart
                        errors = cons(sprintf(nil "Bad window times for %L" winName) errors)
                    )
                else
                    errors = cons("window entry must be (name tStart tEnd)" errors)
            )
        )

        when(errors
            errors = reverse(errors)
            safeOcean_unselectIfHeld()
            return(strcat(
                "{\"ok\":false,\"error\":\""
                safeHelpers_jsonEscape(buildString(errors "; "))
                "\"}"
            ))
        )

        ;; Build (sig, win) dump.
        sigParts = nil
        foreach(sigEntry signalList
            sigName  = nth(0 sigEntry)
            sigKind  = nth(1 sigEntry)
            sigPaths = nth(2 sigEntry)
            fprintf(stderr "[dumpAll] T=%s BUILD sig=%s kind=%s begin\n" getCurrentTime() sigName sigKind)
            w = safeOcean_buildWaveform(sigKind sigPaths)
            fprintf(stderr "[dumpAll] T=%s BUILD sig=%s end w=%L\n" getCurrentTime() sigName if(w "ok" "nil"))
            winParts = nil
            foreach(winEntry windowList
                winName = nth(0 winEntry)
                tStart  = nth(1 winEntry)
                tEnd    = nth(2 winEntry)
                fprintf(stderr "[dumpAll] T=%s STATS sig=%s win=%s begin\n" getCurrentTime() sigName winName)
                winFrag = if(w
                    safeOcean_statsJson(w tStart tEnd)
                    safeOcean_nullStatsJson())
                fprintf(stderr "[dumpAll] T=%s STATS sig=%s win=%s end\n" getCurrentTime() sigName winName)
                winParts = cons(
                    strcat("\"" safeHelpers_jsonEscape(winName) "\":" winFrag)
                    winParts)
            )
            winParts = reverse(winParts)
            sigFrag = strcat("{" buildString(winParts ",") "}")
            sigParts = cons(
                strcat("\"" safeHelpers_jsonEscape(sigName) "\":" sigFrag)
                sigParts)
        )
        sigParts = reverse(sigParts)
        allJson = strcat("{" buildString(sigParts ",") "}")
        safeOcean_unselectIfHeld()
        return(strcat("{\"ok\":true,\"dumps\":" allJson "}"))
    )
)

procedure(safeOceanTCross(signalKind signalPaths threshold
                         crossStart crossEnd
                         @optional (direction "rising") (useAbs nil))
    ;; First-crossing-time primitive. Threshold is supplied by the PC
    ;; evaluator (it already knows the dump's ptp/rms for any window),
    ;; so SKILL stays oblivious to compound-metric semantics.
    ;;
    ;; Returns {"ok":true,"value":<seconds>} or {"ok":false,"error":"..."}.
    prog((w wUse wClipped crossTime wabs dirSym)
        unless(safeOcean_validKind(signalKind)
            return("{\"ok\":false,\"error\":\"Bad signal kind\"}")
        )
        unless(listp(signalPaths)
            return("{\"ok\":false,\"error\":\"signalPaths must be a list\"}")
        )
        unless(numberp(threshold)
            return("{\"ok\":false,\"error\":\"threshold must be numeric\"}")
        )
        unless(numberp(crossStart) && numberp(crossEnd) && crossEnd > crossStart
            return("{\"ok\":false,\"error\":\"Bad cross window\"}")
        )
        unless(stringp(direction) &&
               member(direction safeOcean_allowedCrossDirections) != nil
            return("{\"ok\":false,\"error\":\"Bad direction\"}")
        )

        w = safeOcean_buildWaveform(signalKind signalPaths)
        unless(w
            return("{\"ok\":false,\"error\":\"Could not build waveform\"}")
        )

        wUse = w
        when(useAbs
            wabs = nil
            errset(wabs = abs(w))
            when(wabs
                wUse = wabs
            )
        )

        wClipped = nil
        errset(wClipped = clip(wUse crossStart crossEnd))
        unless(wClipped
            return("{\"ok\":false,\"error\":\"Could not clip cross window\"}")
        )

        dirSym = stringToSymbol(direction)
        crossTime = nil
        errset(crossTime = safeOcean_safeNum(cross(wClipped threshold 1 dirSym)))
        unless(numberp(crossTime)
            return("{\"ok\":false,\"error\":\"No crossing found\"}")
        )

        return(strcat(
            "{\"ok\":true,\"value\":"
            safeHelpers_numToJson(crossTime)
            "}"
        ))
    )
)

;;; ================================================================
;;; Stage 1 rev 6 (2026-04-18) — auto-discover testbench desVar set
;;;
;;; safeOceanListDesignVars(scsPath)
;;;   Parse the `parameters name=val name=val ...` line out of the
;;;   spectre input.scs that Maestro's ExplorerRun generated; return
;;;   the list of (name, default) pairs as JSON.
;;;
;;;   Why this instead of an OCEAN API: IC23.1 has no documented
;;;   public call that enumerates every desVar after `design()`.
;;;   `listDesignVariables` / `asiGetDesignVariables` are not
;;;   available on this install. Maestro's input.scs is the single
;;;   authoritative source — it is the exact parameter set spectre
;;;   actually saw, so parsing it guarantees 1:1 alignment with the
;;;   testbench topology. Works for any cell, any desVar set, no
;;;   hardcoding.
;;;
;;;   `temperature` is filtered out — spectre built-in, not a
;;;   schematic parameter. All other names are returned verbatim
;;;   (case preserved; SKILL-side desVar() is case-sensitive).
;;;
;;; Returns JSON:
;;;   {"ok":true,"vars":[{"name":"C","default":"1f"}, ...]}
;;;   {"ok":false,"error":"..."}
;;;
;;; Security / robustness:
;;;   - scsPath opened via infile with errset; any I/O failure
;;;     degrades to ok:false (never raises).
;;;   - Name is validated against safeOcean_validSigName so a
;;;     corrupted scs cannot smuggle an arbitrary SKILL token into
;;;     later desVar() calls.
;;;   - Default value is JSON-escaped (safeHelpers_jsonEscape) so
;;;     embedded quotes / backslashes in e.g. "1u" or "1k*scale"
;;;     cannot break the JSON wrapper.
;;;   - PC side must still sandbox scsPath to stay inside the
;;;     user's simulation tree.
;;; ----------------------------------------------------------------

procedure(safeOceanListDesignVars(scsPath)
    prog((port line allText joined lines paramLine tokens
          parts name val varPairs pairParts entryJson allJson
          re1 re2 compiled1 compiled2)
        unless(stringp(scsPath)
            return("{\"ok\":false,\"error\":\"scsPath must be a string\"}")
        )

        port = nil
        errset(port = infile(scsPath))
        unless(port
            return("{\"ok\":false,\"error\":\"Could not open input.scs\"}")
        )

        ;; Read entire file. input.scs is typically <50 KB, so we can
        ;; slurp rather than stream.
        allText = ""
        line = ""
        while(gets(line port)
            allText = strcat(allText line)
        )
        close(port)

        ;; Collapse line continuations. Pattern = backslash + any
        ;; trailing whitespace + CR/LF + any leading whitespace on
        ;; the next line, replaced with a single space. Use
        ;; pcreCompile so pcreReplace gets a compiled obj (IC23.1 API).
        compiled1 = nil
        errset(compiled1 = pcreCompile("\\\\[ \t]*[\r\n]+[ \t]*"))
        unless(compiled1
            return("{\"ok\":false,\"error\":\"pcreCompile(continuation) failed\"}")
        )
        joined = allText
        errset(joined = pcreReplace(compiled1 allText " " 0))

        ;; Split into logical lines.
        lines = parseString(joined "\n")

        ;; Find the first `^[ \t]*parameters[ \t]+...` line, strip
        ;; that prefix, keep the remainder as paramLine.
        compiled2 = nil
        errset(compiled2 = pcreCompile("^[ \t]*parameters[ \t]+"))
        unless(compiled2
            return("{\"ok\":false,\"error\":\"pcreCompile(prefix) failed\"}")
        )
        paramLine = nil
        foreach(l lines
            when(and(not(paramLine)
                     pcreMatchp("^[ \t]*parameters[ \t]+" l))
                errset(paramLine = pcreReplace(compiled2 l "" 1))
            )
        )
        unless(paramLine
            return("{\"ok\":false,\"error\":\"No parameters line in scs\"}")
        )

        ;; Each token in paramLine is `name=value`. Split on whitespace
        ;; first, then on `=` to split name/value. Values cannot
        ;; contain whitespace in a Maestro-emitted scs parameters
        ;; line, so this is safe.
        tokens = parseString(paramLine " \t\r")
        varPairs = nil
        foreach(tok tokens
            when(and(stringp(tok) (strlen(tok) > 0))
                parts = parseString(tok "=")
                when(and((length(parts) >= 2)
                         safeOcean_validSigName(car(parts)))
                    name = car(parts)
                    val  = cadr(parts)
                    unless(equal(name "temperature")
                        varPairs = cons(list(name val) varPairs)
                    )
                )
            )
        )
        varPairs = reverse(varPairs)

        pairParts = nil
        foreach(pair varPairs
            entryJson = strcat(
                "{\"name\":\"" safeHelpers_jsonEscape(car(pair))
                "\",\"default\":\"" safeHelpers_jsonEscape(cadr(pair))
                "\"}")
            pairParts = cons(entryJson pairParts)
        )
        pairParts = reverse(pairParts)
        allJson = strcat("[" buildString(pairParts ",") "]")
        return(strcat("{\"ok\":true,\"vars\":" allJson "}"))
    )
)


;;; ----------------------------------------------------------------
;;; safeOceanListAnalyses(scsPath)  — Rev 6 B2 (2026-04-18)
;;;
;;;   Parse `<instanceName> <analysisType> key=value ...` analysis
;;;   blocks out of Maestro's input.scs and return them as a JSON list
;;;   of {name,kwargs} entries. analysisType must be in the global
;;;   allow-list (safeOcean_allowedAnalyses); anything else is
;;;   ignored (comment, subckt, parameters, etc.).
;;;
;;;   Root cause this solves: `design()` + a bare `analysis('tran)`
;;;   inherits NO stop-time from Maestro — spectre runs to t=0 with
;;;   no psf, producing OCN-6038 "Results tran are not available".
;;;   Forwarding `?stop "200n"` (and any other numeric kwarg) keeps
;;;   the agent's OCEAN session aligned with what Maestro intended,
;;;   without re-deriving the numbers in Python or hardcoding them.
;;;
;;;   Only kwargs whose VALUE passes safeHelpers_validateParamValue
;;;   (i.e. numeric literals with optional unit suffix like "200n",
;;;   "10u", "5") are forwarded. Two classes are deliberately dropped:
;;;
;;;     (a) spectre bookkeeping (write="spectre.ic",
;;;         writefinal="spectre.fc", annotate=status) — OCEAN's
;;;         analysis() doesn't need them to produce tran data.
;;;     (b) enum-valued tuning kwargs (errpreset=conservative,
;;;         method=trap, sweeptype=lin) — OCEAN expects a symbol
;;;         here (`?errpreset 'conservative`), NOT a string, and this
;;;         wrapper only inserts kwargs as strings. If a future
;;;         circuit needs a specific errpreset/method, it must be
;;;         set in the Maestro testbench state directly, or a
;;;         later rev must add enum-kwarg support with its own
;;;         allow-list and stringToSymbol coercion.
;;;         (claude_reviewer advisory A2, 2026-04-18)
;;;
;;; Returns JSON:
;;;   {"ok":true,"analyses":[{"name":"tran","kwargs":[
;;;       {"key":"stop","value":"200n"},
;;;       {"key":"maxiters","value":"5"}]}]}
;;;   {"ok":false,"error":"..."}
;;;
;;; Security / robustness:
;;;   - Same file-I/O guards as safeOceanListDesignVars (infile wrapped
;;;     in errset; returns ok:false on open failure).
;;;   - Uses the same pcreCompile/pcreReplace for line-continuation
;;;     stitching; the load-time canary in this file already proved
;;;     both primitives work on IC23.1.
;;;   - No OCEAN call happens here — pure parse-to-JSON. Downstream
;;;     safeOcean_invokeAnalysisSpec re-validates every key/value
;;;     before eval()-ing the analysis call.
;;; ----------------------------------------------------------------
procedure(safeOceanListAnalyses(scsPath)
    prog((port line allText joined lines compContinuation
          tokens instName analysisType kwargTokens kwargList
          n v parts analysesOut kwargsJson kwargsParts
          entry allJson)
        unless(stringp(scsPath)
            return("{\"ok\":false,\"error\":\"scsPath must be a string\"}")
        )

        port = nil
        errset(port = infile(scsPath))
        unless(port
            return("{\"ok\":false,\"error\":\"Could not open input.scs\"}")
        )

        allText = ""
        line = ""
        while(gets(line port)
            allText = strcat(allText line)
        )
        close(port)

        ;; Collapse line continuations (same regex as ListDesignVars).
        compContinuation = nil
        errset(compContinuation = pcreCompile("\\\\[ \t]*[\r\n]+[ \t]*"))
        unless(compContinuation
            return("{\"ok\":false,\"error\":\"pcreCompile(continuation) failed\"}")
        )
        joined = allText
        errset(joined = pcreReplace(compContinuation allText " " 0))

        lines = parseString(joined "\n")

        analysesOut = nil
        foreach(l lines
            tokens = parseString(l " \t\r")
            when(and((length(tokens) >= 2)
                     stringp(car(tokens))
                     stringp(cadr(tokens))
                     ;; Instance name must look like a SKILL identifier
                     ;; (first token). If not, this isn't an analysis
                     ;; declaration — skip.
                     safeHelpers_validateParamName(car(tokens))
                     ;; Analysis type must be in the global allow-list.
                     safeOcean_validAnalysis(cadr(tokens)))
                instName = car(tokens)
                analysisType = lowerCase(cadr(tokens))
                kwargTokens = cddr(tokens)
                kwargList = nil
                foreach(tok kwargTokens
                    when(and(stringp(tok) (strlen(tok) > 0))
                        parts = parseString(tok "=")
                        when((length(parts) >= 2)
                            n = car(parts)
                            v = cadr(parts)
                            ;; Strip surrounding double quotes if any.
                            when(and(stringp(v)
                                     (strlen(v) >= 2)
                                     equal(substring(v 1 1) "\"")
                                     equal(substring(v strlen(v) 1) "\""))
                                v = substring(v 2 (strlen(v) - 2))
                            )
                            ;; Keep kwargs whose value is either a numeric
                            ;; literal OR a whitelisted enum (e.g.
                            ;; skipdc=yes|no). String-valued kwargs outside
                            ;; that whitelist (write=..., annotate=...)
                            ;; are dropped.
                            when(and(safeHelpers_validateParamName(n)
                                     or(safeHelpers_validateParamValue(v)
                                        safeOcean_isEnumKwarg(n v)))
                                kwargList = cons(list(n v) kwargList)
                            )
                        )
                    )
                )
                kwargList = reverse(kwargList)
                analysesOut = cons(list(analysisType kwargList) analysesOut)
            )
        )
        analysesOut = reverse(analysesOut)

        ;; Build JSON.
        allJson = nil
        foreach(a analysesOut
            kwargsParts = nil
            foreach(kw cadr(a)
                kwargsParts = cons(
                    strcat("{\"key\":\"" safeHelpers_jsonEscape(car(kw))
                           "\",\"value\":\"" safeHelpers_jsonEscape(cadr(kw))
                           "\"}")
                    kwargsParts)
            )
            kwargsParts = reverse(kwargsParts)
            kwargsJson = strcat("[" buildString(kwargsParts ",") "]")
            entry = strcat(
                "{\"name\":\"" safeHelpers_jsonEscape(car(a))
                "\",\"kwargs\":" kwargsJson "}")
            allJson = cons(entry allJson)
        )
        allJson = reverse(allJson)
        return(strcat("{\"ok\":true,\"analyses\":["
                      buildString(allJson ",")
                      "]}"))
    )
)


;;; ----------------------------------------------------------------
;;; Rev 6 M2 (claude_reviewer 2026-04-18): §6.2.10 primitive-probe
;;; canary for pcreCompile / pcreReplace. The stage0 probe matrix
;;; confirmed pcreMatchp on IC23.1, but safeOceanListDesignVars
;;; introduces pcreCompile + pcreReplace for the first time in this
;;; code path. Fail the load loudly if either primitive is broken or
;;; missing, instead of deferring the failure to first invocation.
;;; Placed BEFORE the "loaded successfully" printf so a canary failure
;;; suppresses the success message and aborts load-time.
;;; ----------------------------------------------------------------
let((safeOcean_canaryCompiled safeOcean_canaryReplaced)
    safeOcean_canaryCompiled = nil
    errset(safeOcean_canaryCompiled = pcreCompile("^test$"))
    unless(safeOcean_canaryCompiled
        error("SELF-TEST FAIL: pcreCompile unavailable on this IC version")
    )
    safeOcean_canaryReplaced = nil
    errset(safeOcean_canaryReplaced =
        pcreReplace(safeOcean_canaryCompiled "test" "ok" 1))
    unless(equal(safeOcean_canaryReplaced "ok")
        error("SELF-TEST FAIL: pcreReplace broken or missing")
    )
)

printf("safe_ocean.il loaded successfully.\n")
