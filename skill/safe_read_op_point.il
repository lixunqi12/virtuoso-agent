;;; safe_read_op_point.il — Read DC operating-point data, return only safe parameters
;;; Compatible with Cadence IC23.1 SKILL (no SKILL++ syntax)
;;;
;;; Requires: helpers.il loaded first
;;;
;;; Usage:
;;;   safeReadOpPoint("myLib" "myCell")
;;;   => Returns JSON string with only safe operating-point data
;;;
;;; Security guarantees:
;;;   - Only 8 safe parameters returned: gm, gds, id, vth, vdsat, cgs, cgd, cdb
;;;   - BSIM4 model parameters (toxe, u0, vth0, k1, k2, pclm, etc.) NEVER returned
;;;   - Instance names preserved for identification, library names sanitized

procedure(safeReadOpPoint(libName cellName @optional (viewName "schematic"))
    prog((cv results resultsDir opData instDirs instDir
         instName paramNames paramName paramVal
         safeInstData allInstData jsonStr)

        ;; Input validation
        unless(and(stringp(libName) stringp(cellName))
            return("{\"error\":\"Invalid arguments: lib and cell must be strings\"}")
        )

        ;; Open the cellView to get simulation results
        cv = dbOpenCellViewByType(libName cellName viewName nil "r")
        unless(cv
            return(sprintf(nil "{\"error\":\"Cannot open cellView %s/%s/%s\"}"
                libName cellName viewName))
        )

        ;; Try to get the DC operating-point results
        ;; Cadence stores simulation results in the results database
        resultsDir = asiGetResultDir()
        unless(resultsDir
            dbClose(cv)
            return("{\"error\":\"No simulation results directory found. Run DC simulation first.\"}")
        )

        ;; Get the operating-point data from the most recent DC analysis
        results = asiGetDcOpResults()
        unless(results
            ;; Fallback: try to open results via ADE
            results = asiGetResults("dcOp")
            unless(results
                dbClose(cv)
                return("{\"error\":\"No DC operating-point results found. Run DC simulation first.\"}")
            )
        )

        ;; ---- Extract safe OP data per instance ----
        allInstData = nil

        ;; Iterate over instance operating points
        instDirs = asiListInstOpPointNames()
        when(instDirs
            foreach(instName instDirs
                safeInstData = nil

                ;; For each safe key, try to read the value
                foreach(safeKey safeHelpers_safeOpKeys
                    paramName = symbolToString(safeKey)
                    paramVal = asiGetInstOpPointValue(instName paramName)
                    when(and(paramVal numberp(paramVal))
                        safeInstData = cons(list(paramName paramVal) safeInstData)
                    )
                )

                ;; Only include instances that have at least one safe parameter
                when(safeInstData
                    safeInstData = reverse(safeInstData)
                    allInstData = cons(
                        list(instName safeInstData)
                        allInstData
                    )
                )
            )
        )
        allInstData = reverse(allInstData)

        ;; ---- Build JSON output ----
        jsonStr = strcat(
            "{"
            "\"cell\":\"" safeHelpers_jsonEscape(cellName) "\","
            "\"lib\":\"GENERIC_PDK\","
            "\"analysis\":\"dcOp\","
            "\"instances\":" safeReadOpPoint_instToJson(allInstData)
            "}"
        )

        dbClose(cv)
        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; Alternative entry point: read OP from already-loaded results
;;; Useful when the caller has already set up the results context.
;;; ----------------------------------------------------------------
procedure(safeReadOpPointFromResults(resultsHandle)
    prog((instNames instName safeInstData paramName paramVal allInstData jsonStr)

        unless(resultsHandle
            return("{\"error\":\"Invalid results handle\"}")
        )

        allInstData = nil

        instNames = asiListInstOpPointNames()
        when(instNames
            foreach(instName instNames
                safeInstData = nil
                foreach(safeKey safeHelpers_safeOpKeys
                    paramName = symbolToString(safeKey)
                    paramVal = asiGetInstOpPointValue(instName paramName)
                    when(and(paramVal numberp(paramVal))
                        safeInstData = cons(list(paramName paramVal) safeInstData)
                    )
                )
                when(safeInstData
                    safeInstData = reverse(safeInstData)
                    allInstData = cons(list(instName safeInstData) allInstData)
                )
            )
        )
        allInstData = reverse(allInstData)

        jsonStr = strcat(
            "{\"analysis\":\"dcOp\","
            "\"instances\":" safeReadOpPoint_instToJson(allInstData)
            "}"
        )
        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; Internal: convert instance OP data to JSON
;;; Input: list of (instName  ((param1 val1) (param2 val2) ...))
;;; Output: {"instName": {"gm": 0.001, ...}, ...}
;;; ----------------------------------------------------------------
procedure(safeReadOpPoint_instToJson(allInstData)
    let((parts entry instName instParams paramParts paramEntry)
        if(allInstData == nil then
            "{}"
        else
            parts = nil
            foreach(entry allInstData
                instName = car(entry)
                instParams = cadr(entry)

                ;; Build the parameter object for this instance
                paramParts = nil
                foreach(paramEntry instParams
                    paramParts = cons(
                        strcat("\"" safeHelpers_jsonEscape(car(paramEntry))
                               "\":" safeHelpers_numToJson(cadr(paramEntry)))
                        paramParts
                    )
                )

                parts = cons(
                    strcat("\"" safeHelpers_jsonEscape(instName)
                           "\":{" buildString(reverse(paramParts) ",") "}")
                    parts
                )
            )
            strcat("{" buildString(reverse(parts) ",") "}")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Rev 7 (Stage B, 2026-04-19): safeReadOpPointAfterTran
;;;
;;; Read per-instance DC op-point data from the `tranOp` sub-result that
;;; Spectre/APS auto-computes at t=0 of a transient run. No dedicated DC
;;; analysis needed. Dual-signed path confirmed by probe v3 + v4:
;;;
;;;   probe v3 → selectResult('tranOp) succeeds; outputs() returns the
;;;              full instance list (14 entries under /I0/); getData(
;;;              "/I0/M1" ?result "tranOp") returns a dr: handle.
;;;   probe v4 → handle->vgs / handle->vds / handle->gm / handle->region
;;;              etc return scalar values; drGetPropList & drGetPropValue
;;;              are UNDEFINED on this IC23.1 install — dispatcher must
;;;              be hard-coded.
;;;
;;; Security guarantees (unchanged from safeReadOpPoint):
;;;   - Only the keys in safeHelpers_safeOpKeys are readable. The
;;;     dispatcher is a hard-coded case-of-symbol, so even if the
;;;     whitelist is tampered with in memory, no other handle->prop can
;;;     fire. Whitelist defense + dispatcher defense = two independent
;;;     gates.
;;;   - No BSIM model intrinsics (toxe/u0/vth0/...) can ever leak.
;;;   - Library name stays replaced with "GENERIC_PDK" in the JSON.
;;;   - Instance paths are preserved (they are just topology, not PDK IP).
;;;   - The OCEAN result selection is restored to 'tran on exit so the
;;;     enclosing Maestro waveform view isn't disturbed.
;;;
;;; Usage (called via safe_bridge.py read_op_point_after_tran):
;;;   safeReadOpPointAfterTran("<psfDir>")
;;;   => JSON string with per-instance op values, or {"error":...}
;;;
;;; Rev 13b (2026-04-21, B1 fix): the function now opens its own PSF
;;; results database before selectResult('tranOp). Previously it relied
;;; on a prior selectResult('tran) binding in the SKILL session; Task A2
;;; added an iter-start unselectResult() that cleared that binding, so
;;; every iter's selectResult('tranOp) was failing. Taking psfDir as an
;;; explicit argument makes this function self-contained and immune to
;;; caller-side session state. The caller (Python) passes
;;; bridge.last_results_dir.
;;; ----------------------------------------------------------------

;;; Hard-coded handle-prop dispatcher. Any `safeKey` outside this case()
;;; returns nil — a silent second-layer defence if the whitelist is
;;; somehow bypassed. Add new keys ONLY after probe v4 has confirmed
;;; their availability on the target PDK.
procedure(safeReadOpPoint_handleGet(h safeKey)
    case(safeKey
        ;; M-device primary bias
        (vgs     h->vgs)
        (vds     h->vds)
        (vbs     h->vbs)
        ;; M-device current
        (id      h->id)
        (ids     h->ids)
        ;; Small-signal
        (gm      h->gm)
        (gds     h->gds)
        (gmb     h->gmb)
        ;; Threshold / overdrive
        (vth     h->vth)
        (vdsat   h->vdsat)
        (vov     h->vov)
        ;; Region flag (integer enum)
        (region  h->region)
        ;; Terminal capacitances
        (cgs     h->cgs)
        (cgd     h->cgd)
        (cdb     h->cdb)
        (cdg     h->cdg)
        (cgg     h->cgg)
        ;; Non-MOS device attrs (resistor/inductor/cap)
        (i       h->i)
        (v       h->v)
        (pwr     h->pwr)
        (t       nil)
    )
)


procedure(safeReadOpPointAfterTran(psfDir)
    prog((origSelect instNames safeInstData allInstData
          jsonStr h paramName paramVal openRv)

        ;; Input validation: psfDir must be a non-empty string. Python
        ;; side scrubs it with _SAFE_PSF_DIR_RE before we see it.
        unless(and(stringp(psfDir) (length(psfDir) > 0))
            return("{\"error\":\"safeReadOpPointAfterTran requires a non-empty psfDir string\"}")
        )

        ;; Open the PSF results DB. Self-contained — does not depend on
        ;; the caller's current selectResult state (see rev 13b note).
        openRv = nil
        openRv = errset(openResults(psfDir) t)
        unless(and(openRv car(openRv))
            return("{\"error\":\"openResults failed; psfDir may not exist or was overwritten\"}")
        )

        ;; Save the currently selected result so we can restore on exit.
        origSelect = nil
        errset(origSelect = results() t)

        ;; Switch into tranOp. If selectResult returns nil or errors out,
        ;; bail with an explicit error string.
        unless(errset(selectResult('tranOp) t)
            return(sprintf(nil "{\"error\":\"selectResult('tranOp) failed; tran may not have run\"}"))
        )

        ;; Enumerate instances. outputs() in tranOp context returns a
        ;; list of instance paths like ("/I0/M0" "/I0/M1" ...).
        instNames = nil
        let((rv)
            rv = errset(outputs() t)
            when(and(rv listp(car(rv)))
                instNames = car(rv)
            )
        )

        unless(instNames
            ;; Clean up result binding and bail. Rev 13b: unselectResult
            ;; matches the exit path of the success branch below.
            errset(unselectResult() t)
            return("{\"error\":\"No instances found in tranOp outputs()\"}")
        )

        allInstData = nil
        foreach(instName instNames
            when(stringp(instName)
                ;; Fetch the dr: handle for this instance
                h = nil
                let((rv)
                    rv = errset(getData(instName ?result "tranOp") t)
                    when(and(rv car(rv))
                        h = car(rv)
                    )
                )

                when(h
                    safeInstData = nil
                    foreach(safeKey safeHelpers_safeOpKeys
                        paramName = symbolToString(safeKey)
                        paramVal = nil
                        let((rv)
                            rv = errset(safeReadOpPoint_handleGet(h safeKey) t)
                            when(and(rv car(rv))
                                paramVal = car(rv)
                            )
                        )
                        when(and(paramVal numberp(paramVal))
                            safeInstData = cons(list(paramName paramVal) safeInstData)
                        )
                    )
                    when(safeInstData
                        safeInstData = reverse(safeInstData)
                        allInstData = cons(
                            list(instName safeInstData)
                            allInstData
                        )
                    )
                )
            )
        )
        allInstData = reverse(allInstData)

        ;; Drop the selection handle that selectResult('tranOp) installed.
        ;; Rev 13b: we own the results binding for this call (we opened
        ;; it), so leave the session clean on exit — no stale handle for
        ;; the next iter's unselectResult() to guard against.
        errset(unselectResult() t)

        jsonStr = strcat(
            "{"
            "\"lib\":\"GENERIC_PDK\","
            "\"analysis\":\"tranOp\","
            "\"instances\":" safeReadOpPoint_instToJson(allInstData)
            "}"
        )
        return(jsonStr)
    )
)


printf("safe_read_op_point.il loaded successfully.\n")
