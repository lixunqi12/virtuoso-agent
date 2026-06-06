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

        ;; Try to get the DC operating-point results.  Newer IC23.1
        ;; builds expose asiGetResultsDir(); older notes used
        ;; asiGetResultDir().  Guard both names because calling an
        ;; undefined SKILL function, even inside errset, pollutes CIW.
        resultsDir = nil
        when(isCallable('asiGetResultsDir)
            errset(resultsDir = asiGetResultsDir(asiGetCurrentSession()) t)
        )
        when(and(null(resultsDir) isCallable('asiGetResultDir))
            errset(resultsDir = asiGetResultDir() t)
        )
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
;;; Internal helpers for Maestro DC operating-point readback.
;;; The readback is intentionally request-scoped: the caller must pass
;;; explicit net and instance paths. We never enumerate model/primitives
;;; result containers because those names can expose PDK/model metadata.
;;; ----------------------------------------------------------------
procedure(safeReadOpPoint_validHistoryName(historyName)
    if(and(stringp(historyName)
           pcreMatchp("^[A-Za-z0-9_.-]{1,128}\\z" historyName))
        t
        nil
    )
)

procedure(safeReadOpPoint_validResultPath(resultPath)
    if(and(stringp(resultPath)
           pcreMatchp("^/[A-Za-z0-9_./!]{1,255}\\z" resultPath))
        t
        nil
    )
)

procedure(safeReadOpPoint_validatePathList(pathList label maxItems)
    prog((idx item)
        unless(listp(pathList)
            return(strcat(label " must be a list"))
        )
        when(greaterp(length(pathList) maxItems)
            return(sprintf(nil "%s exceeds max items" label))
        )
        idx = 0
        foreach(item pathList
            idx = idx + 1
            unless(safeReadOpPoint_validResultPath(item)
                return(sprintf(nil "%s item %d has invalid path shape"
                               label idx))
            )
        )
        return(nil)
    )
)

procedure(safeReadOpPoint_stringListToJson(items)
    let((parts item)
        parts = nil
        foreach(item items
            when(stringp(item)
                parts = cons(strcat("\"" safeHelpers_jsonEscape(item) "\"")
                             parts)
            )
        )
        strcat("[" buildString(reverse(parts) ",") "]")
    )
)

procedure(safeReadOpPoint_nodeToJson(nodeData)
    let((parts entry)
        if(nodeData == nil then
            "{}"
        else
            parts = nil
            foreach(entry nodeData
                parts = cons(
                    strcat("\"" safeHelpers_jsonEscape(car(entry)) "\":"
                           safeHelpers_numToJson(cadr(entry)))
                    parts
                )
            )
            strcat("{" buildString(reverse(parts) ",") "}")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Internal: bounded vdsat alias probe support.
;;;
;;; Some FinFET compact-model result containers expose the saturation
;;; voltage under a simulator/model-specific synonym.  This list is a
;;; hard-coded probe surface: it is intentionally small, contains
;;; true-vdsat-shaped names plus a few nearby diagnostics, and never
;;; enumerates arbitrary model/OP keys.
;;; ----------------------------------------------------------------
procedure(safeReadOpPoint_vdsatProbeCandidates()
    list(
        "vdsat" "vdssat" "vdsat_eff" "vdsatEff" "Vdsat"
        "vdsatcv" "vdsatCV" "vdsat_cv" "vdsatc" "vdsatC"
        "vdsat_c" "VDSAT"
        "vdsat_vadj" "vdsat_vdl"
        "vdseff" "vdsEff" "vds_eff" "vgst" "vgsteff" "vgstEff"
    )
)

procedure(safeReadOpPoint_trueVdsatCandidates()
    list(
        "vdsat" "vdssat" "vdsat_eff" "vdsatEff" "Vdsat"
        "vdsatcv" "vdsatCV" "vdsat_cv" "vdsatc" "vdsatC"
        "vdsat_c" "VDSAT"
    )
)

procedure(safeReadOpPoint_stringInListp(name items)
    let((found item)
        found = nil
        when(and(stringp(name) listp(items))
            foreach(item items
                when(equal(name item)
                    found = t
                )
            )
        )
        found
    )
)

procedure(safeReadOpPoint_vdsatProbeCandidateAllowedp(name)
    safeReadOpPoint_stringInListp(name safeReadOpPoint_vdsatProbeCandidates())
)

procedure(safeReadOpPoint_trueVdsatCandidatep(name)
    safeReadOpPoint_stringInListp(name safeReadOpPoint_trueVdsatCandidates())
)


;;; ----------------------------------------------------------------
;;; Internal: read one safe numeric OP parameter from an instance.
;;;
;;; Different Spectre result containers expose different shapes:
;;; - tranOp often supports handle->prop directly.
;;; - dc/instance sometimes returns a sparse handle where only region is
;;;   available, while pv()/OP()/getData("inst:param") can still expose
;;;   the numeric operating-point fields.
;;;
;;; Keep this helper request-scoped and key-scoped: callers pass a single
;;; explicit instance path and a safeKey from safeHelpers_safeOpKeys.
;;; ----------------------------------------------------------------
procedure(safeReadOpPoint_paramAliases(safeKey)
    prog((key)
        key = symbolToString(safeKey)
        cond(
            ;; Some Spectre/model combinations do not expose the
            ;; saturation-voltage scalar under exactly "vdsat" even after
            ;; saveOpPoint(... ?operatingPoints "..."). Probe only this
            ;; bounded synonym set; never enumerate or return arbitrary
            ;; OP keys.
            (equal(key "vdsat")
                return(list("vdssat" "vdsat_eff" "vdsatEff" "Vdsat"))
            )
            (t
                return(nil)
            )
        )
    )
)

procedure(safeReadOpPoint_readCandidateParam(instName paramName h primaryResult altResult)
    prog((rv dataPath sep resultLabel seenPrimary)
        unless(safeReadOpPoint_vdsatProbeCandidateAllowedp(paramName)
            return(nil)
        )

        when(h
            rv = errset(safeReadOpPoint_handleGet(h stringToSymbol(paramName)) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
        )

        ;; Nearby diagnostic candidates are not true vdsat aliases and are
        ;; not explicitly requested by saveOpPoint.  Keep them handle-only:
        ;; some OCEAN calculator accessors become unstable when asked for
        ;; non-saved model-specific names.
        unless(safeReadOpPoint_trueVdsatCandidatep(paramName)
            return(nil)
        )

        when(isCallable('pv)
            seenPrimary = nil
            foreach(resultLabel list(primaryResult altResult)
                when(and(stringp(resultLabel) (length(resultLabel) > 0))
                    unless(and(seenPrimary equal(resultLabel primaryResult))
                        rv = errset(pv(instName paramName ?result resultLabel) t)
                        when(and(rv numberp(car(rv)))
                            return(car(rv))
                        )
                        rv = errset(pv(instName stringToSymbol(paramName) ?result resultLabel) t)
                        when(and(rv numberp(car(rv)))
                            return(car(rv))
                        )
                    )
                    when(equal(resultLabel primaryResult)
                        seenPrimary = t
                    )
                )
            )
            rv = errset(pv(instName paramName) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
            rv = errset(pv(instName stringToSymbol(paramName)) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
        )

        when(isCallable('OS)
            rv = errset(OS(instName paramName) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
            rv = errset(OS(instName stringToSymbol(paramName)) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
        )

        when(isCallable('OP)
            rv = errset(OP(instName paramName) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
            rv = errset(OP(instName stringToSymbol(paramName)) t)
            when(and(rv numberp(car(rv)))
                return(car(rv))
            )
        )

        foreach(resultLabel list(primaryResult altResult)
            when(and(stringp(resultLabel) (length(resultLabel) > 0))
                foreach(sep list(":" ".")
                    dataPath = strcat(instName sep paramName)
                    rv = errset(getData(dataPath ?result resultLabel) t)
                    when(and(rv numberp(car(rv)))
                        return(car(rv))
                    )
                )
            )
        )

        return(nil)
    )
)

procedure(safeReadOpPoint_getNumericParam(instName safeKey h primaryResult altResult)
    prog((paramName paramVal rv dataPath sep resultLabel seenPrimary paramNames)
        paramName = symbolToString(safeKey)
        paramNames = cons(paramName safeReadOpPoint_paramAliases(safeKey))

        when(h
            foreach(paramName paramNames
                rv = errset(safeReadOpPoint_handleGet(h stringToSymbol(paramName)) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
            )
        )

        ;; Try pv() for the named result containers. Some IC/Spectre
        ;; versions expose DC OP values this way even when handle->prop
        ;; only exposes region.
        when(isCallable('pv)
            seenPrimary = nil
            foreach(resultLabel list(primaryResult altResult)
                when(and(stringp(resultLabel) (length(resultLabel) > 0))
                    unless(and(seenPrimary equal(resultLabel primaryResult))
                        foreach(paramName paramNames
                            rv = errset(pv(instName paramName ?result resultLabel) t)
                            when(and(rv numberp(car(rv)))
                                return(car(rv))
                            )
                            rv = errset(pv(instName stringToSymbol(paramName) ?result resultLabel) t)
                            when(and(rv numberp(car(rv)))
                                return(car(rv))
                            )
                        )
                    )
                    when(equal(resultLabel primaryResult)
                        seenPrimary = t
                    )
                )
            )
            foreach(paramName paramNames
                rv = errset(pv(instName paramName) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
                rv = errset(pv(instName stringToSymbol(paramName)) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
            )
        )

        ;; Operating point parameters explicitly requested via
        ;; saveOpPoint(inst ?operatingPoints "...") are commonly read
        ;; through OS(inst param) in ADE/OCEAN.
        when(isCallable('OS)
            foreach(paramName paramNames
                rv = errset(OS(instName paramName) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
                rv = errset(OS(instName stringToSymbol(paramName)) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
            )
        )

        ;; Calculator OP() is available in many ADE/OCEAN sessions.
        when(isCallable('OP)
            foreach(paramName paramNames
                rv = errset(OP(instName paramName) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
                rv = errset(OP(instName stringToSymbol(paramName)) t)
                when(and(rv numberp(car(rv)))
                    return(car(rv))
                )
            )
        )

        ;; Last resort: common PSF scalar names.
        foreach(resultLabel list(primaryResult altResult)
            when(and(stringp(resultLabel) (length(resultLabel) > 0))
                foreach(paramName paramNames
                    foreach(sep list(":" ".")
                        dataPath = strcat(instName sep paramName)
                        rv = errset(getData(dataPath ?result resultLabel) t)
                        when(and(rv numberp(car(rv)))
                            return(car(rv))
                        )
                    )
                )
            )
        )

        return(nil)
    )
)

procedure(safeReadOpPoint_collectInstData(instName h primaryResult altResult)
    prog((safeInstData safeKey paramName paramVal)
        safeInstData = nil
        foreach(safeKey safeHelpers_safeOpKeys
            paramName = symbolToString(safeKey)
            paramVal = safeReadOpPoint_getNumericParam(
                instName safeKey h primaryResult altResult
            )
            when(numberp(paramVal)
                safeInstData = cons(list(paramName paramVal) safeInstData)
            )
        )
        when(safeInstData
            return(reverse(safeInstData))
        )
        return(nil)
    )
)

procedure(safeReadOpPoint_collectHandleData(instName h)
    prog((safeInstData safeKey paramName paramVal rv)
        safeInstData = nil
        when(h
            foreach(safeKey safeHelpers_safeOpKeys
                paramName = symbolToString(safeKey)
                rv = errset(safeReadOpPoint_handleGet(h safeKey) t)
                when(and(rv numberp(car(rv)))
                    paramVal = car(rv)
                    safeInstData = cons(list(paramName paramVal) safeInstData)
                )
            )
        )
        when(safeInstData
            return(reverse(safeInstData))
        )
        return(nil)
    )
)


;;; ----------------------------------------------------------------
;;; safeReadMaestroDcOpPoint(historyName netList instList)
;;;
;;; Read DC scalar node values and allow-listed instance operating-point
;;; fields from an already-run Maestro history. Security posture:
;;;   - Opens only the requested history name.
;;;   - Selects only 'dc and 'instance result containers.
;;;   - Never selects 'model or 'primitives.
;;;   - Reads only explicit caller-requested paths.
;;;   - Instance fields still pass through safeReadOpPoint_handleGet(),
;;;     the hard-coded allow-list dispatcher.
;;; ----------------------------------------------------------------
procedure(safeReadMaestroDcOpPoint(historyName netList instList)
    prog((openRv resultsDir availResults issues resultKinds instanceSelected
          nodeData instData errMsg dcAvail instanceAvail
          netName nodeVal instName safeInstData h
          paramName paramVal jsonStr)

        unless(safeReadOpPoint_validHistoryName(historyName)
            return("{\"ok\":false,\"error\":\"Invalid Maestro history name\"}")
        )
        errMsg = safeReadOpPoint_validatePathList(netList "netList" 64)
        when(errMsg
            return(strcat("{\"ok\":false,\"error\":\""
                          safeHelpers_jsonEscape(errMsg) "\"}"))
        )
        errMsg = safeReadOpPoint_validatePathList(instList "instList" 128)
        when(errMsg
            return(strcat("{\"ok\":false,\"error\":\""
                          safeHelpers_jsonEscape(errMsg) "\"}"))
        )

        unless(isCallable('maeOpenResults)
            return("{\"ok\":false,\"error\":\"maeOpenResults is unavailable\"}")
        )

        openRv = errset(maeOpenResults(?history historyName) t)
        unless(openRv
            return("{\"ok\":false,\"error\":\"maeOpenResults failed for requested history\"}")
        )

        resultsDir = nil
        when(and(isCallable('asiGetResultsDir) isCallable('asiGetCurrentSession))
            errset(resultsDir = asiGetResultsDir(asiGetCurrentSession()) t)
        )
        when(and(null(resultsDir) isCallable('asiGetResultDir))
            errset(resultsDir = asiGetResultDir() t)
        )
        unless(and(resultsDir stringp(resultsDir))
            return("{\"ok\":false,\"error\":\"No Maestro results directory found for requested history\"}")
        )

        openRv = errset(openResults(resultsDir) t)
        unless(and(openRv car(openRv))
            return("{\"ok\":false,\"error\":\"openResults failed for Maestro history\"}")
        )

        availResults = nil
        let((rv)
            rv = errset(results() t)
            when(and(rv listp(car(rv)))
                availResults = car(rv)
            )
        )
        dcAvail = safeReadOpPoint_resultAvailable(availResults 'dc)
        instanceAvail = safeReadOpPoint_resultAvailable(availResults 'instance)
        unless(dcAvail
            safeReadOpPoint_unselectIfCallable()
            return("{\"ok\":false,\"error\":\"dc result not available in Maestro history\"}")
        )

        issues = nil
        resultKinds = list("dc")
        nodeData = nil
        instData = nil

        unless(errset(selectResult('dc) t)
            safeReadOpPoint_unselectIfCallable()
            return("{\"ok\":false,\"error\":\"selectResult('dc) failed\"}")
        )
        foreach(netName netList
            nodeVal = nil
            let((rv)
                rv = errset(v(netName) t)
                when(and(rv numberp(car(rv)))
                    nodeVal = car(rv)
                )
            )
            unless(numberp(nodeVal)
                let((rv)
                    rv = errset(getData(netName) t)
                    when(and(rv numberp(car(rv)))
                        nodeVal = car(rv)
                    )
                )
            )
            when(numberp(nodeVal)
                nodeData = cons(list(netName nodeVal) nodeData)
            )
        )
        nodeData = reverse(nodeData)

        if(instanceAvail then
            resultKinds = cons("instance" resultKinds)
            instanceSelected = errset(selectResult('instance) t)
            unless(instanceSelected
                issues = cons("selectResult('instance) failed" issues)
            )
            when(instanceSelected
                foreach(instName instList
                    safeInstData = nil
                    h = nil
                    let((rv)
                        rv = errset(getData(instName) t)
                        when(and(rv car(rv))
                            h = car(rv)
                        )
                    )
                    safeInstData = safeReadOpPoint_collectInstData(
                        instName h "instance" "dc"
                    )
                    unless(safeInstData
                        h = nil
                        let((rv)
                            rv = errset(getData(instName ?result "dc") t)
                            when(and(rv car(rv))
                                h = car(rv)
                            )
                        )
                        safeInstData = safeReadOpPoint_collectInstData(
                            instName h "dc" "instance"
                        )
                    )
                    when(safeInstData
                        instData = cons(list(instName safeInstData)
                                        instData)
                    )
                )
            )
        else
            when(instList
                issues = cons("instance result not available" issues)
            )
        )
        instData = reverse(instData)

        safeReadOpPoint_unselectIfCallable()

        jsonStr = strcat(
            "{"
            "\"ok\":true,"
            "\"history\":\"" safeHelpers_jsonEscape(historyName) "\","
            "\"analysis\":\"dc\","
            "\"resultKinds\":" safeReadOpPoint_stringListToJson(reverse(resultKinds)) ","
            "\"nodes\":" safeReadOpPoint_nodeToJson(nodeData) ","
            "\"instances\":" safeReadOpPoint_instToJson(instData) ","
            "\"issues\":" safeReadOpPoint_stringListToJson(reverse(issues))
            "}"
        )
        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; safeReadDcOpPointFromResults(psfDir netList instList)
;;;
;;; Read DC scalar node values and allow-listed instance operating-point
;;; fields from the currently generated PSF directory. This is the
;;; per-run companion to safeReadMaestroDcOpPoint(): agent AC/DC loops
;;; should use this path after safeOceanRun so feedback comes from the
;;; just-completed simulation rather than a possibly stale ADE history.
;;;
;;; Security posture:
;;;   - Opens only the caller-provided PSF result directory.
;;;   - Selects only 'dc and 'instance result containers.
;;;   - Never selects 'model or 'primitives.
;;;   - Reads only explicit caller-requested net/instance paths.
;;;   - Instance fields still pass through safeReadOpPoint_handleGet().
;;; ----------------------------------------------------------------
procedure(safeReadDcOpPointFromResults(psfDir netList instList)
    prog((openRv availResults issues resultKinds instanceSelected
          nodeData instData errMsg dcAvail instanceAvail
          dcResultName dcResultString candidate
          netName nodeVal instName safeInstData h
          paramName paramVal jsonStr)

        unless(and(stringp(psfDir) (length(psfDir) > 0))
            return("{\"ok\":false,\"error\":\"safeReadDcOpPointFromResults requires a non-empty psfDir string\"}")
        )
        errMsg = safeReadOpPoint_validatePathList(netList "netList" 64)
        when(errMsg
            return(strcat("{\"ok\":false,\"error\":\""
                          safeHelpers_jsonEscape(errMsg) "\"}"))
        )
        errMsg = safeReadOpPoint_validatePathList(instList "instList" 128)
        when(errMsg
            return(strcat("{\"ok\":false,\"error\":\""
                          safeHelpers_jsonEscape(errMsg) "\"}"))
        )

        openRv = errset(openResults(psfDir) t)
        unless(and(openRv car(openRv))
            return("{\"ok\":false,\"error\":\"openResults failed for requested PSF directory\"}")
        )

        availResults = nil
        let((rv)
            rv = errset(results() t)
            when(and(rv listp(car(rv)))
                availResults = car(rv)
            )
        )
        dcResultName = nil
        foreach(candidate list('dc 'dcOp 'dcOpInfo 'finalTimeOP)
            when(and(null(dcResultName)
                     safeReadOpPoint_resultAvailable(availResults candidate))
                dcResultName = candidate
            )
        )
        dcAvail = if(dcResultName then t else nil)
        if(dcResultName then
            dcResultString = symbolToString(dcResultName)
        else
            dcResultString = "dc"
        )
        instanceAvail = safeReadOpPoint_resultAvailable(availResults 'instance)
        unless(dcAvail
            safeReadOpPoint_unselectIfCallable()
            return("{\"ok\":false,\"error\":\"no dc/dcOp/dcOpInfo operating-point result available in opened PSF results\"}")
        )

        issues = nil
        resultKinds = list(dcResultString)
        unless(equal(dcResultName 'dc)
            issues = cons(strcat("used " dcResultString
                                 " result for operating point") issues)
        )
        nodeData = nil
        instData = nil

        unless(errset(selectResult(dcResultName) t)
            safeReadOpPoint_unselectIfCallable()
            return("{\"ok\":false,\"error\":\"selectResult on operating-point result failed\"}")
        )
        foreach(netName netList
            nodeVal = nil
            let((rv)
                rv = errset(v(netName) t)
                when(and(rv numberp(car(rv)))
                    nodeVal = car(rv)
                )
            )
            unless(numberp(nodeVal)
                let((rv)
                    rv = errset(getData(netName) t)
                    when(and(rv numberp(car(rv)))
                        nodeVal = car(rv)
                    )
                )
            )
            when(numberp(nodeVal)
                nodeData = cons(list(netName nodeVal) nodeData)
            )
        )
        nodeData = reverse(nodeData)

        if(instanceAvail then
            resultKinds = cons("instance" resultKinds)
            instanceSelected = errset(selectResult('instance) t)
            unless(instanceSelected
                issues = cons("selectResult('instance) failed" issues)
            )
            when(instanceSelected
                foreach(instName instList
                    safeInstData = nil
                    h = nil
                    let((rv)
                        rv = errset(getData(instName) t)
                        when(and(rv car(rv))
                            h = car(rv)
                        )
                    )
                    safeInstData = safeReadOpPoint_collectInstData(
                        instName h "instance" dcResultString
                    )
                    unless(safeInstData
                        h = nil
                        let((rv)
                            rv = errset(getData(instName ?result dcResultString) t)
                            when(and(rv car(rv))
                                h = car(rv)
                            )
                        )
                        safeInstData = safeReadOpPoint_collectInstData(
                            instName h dcResultString "instance"
                        )
                    )
                    when(safeInstData
                        instData = cons(list(instName safeInstData)
                                        instData)
                    )
                )
            )
        else
            when(instList
                issues = cons("instance result not available; trying operating-point result handles" issues)
                unless(errset(selectResult(dcResultName) t)
                    issues = cons("selectResult operating-point fallback failed" issues)
                )
                foreach(instName instList
                    safeInstData = nil
                    h = nil
                    let((rv)
                        rv = errset(getData(instName ?result dcResultString) t)
                        when(and(rv car(rv))
                            h = car(rv)
                        )
                    )
                    unless(h
                        let((rv)
                            rv = errset(getData(instName) t)
                            when(and(rv car(rv))
                                h = car(rv)
                            )
                        )
                    )
                    safeInstData = safeReadOpPoint_collectInstData(
                        instName h dcResultString nil
                    )
                    when(safeInstData
                        instData = cons(list(instName safeInstData)
                                        instData)
                    )
                )
            )
        )
        instData = reverse(instData)

        safeReadOpPoint_unselectIfCallable()

        jsonStr = strcat(
            "{"
            "\"ok\":true,"
            "\"source\":\"psf\","
            "\"analysis\":\"dc\","
            "\"resultKinds\":" safeReadOpPoint_stringListToJson(reverse(resultKinds)) ","
            "\"nodes\":" safeReadOpPoint_nodeToJson(nodeData) ","
            "\"instances\":" safeReadOpPoint_instToJson(instData) ","
            "\"issues\":" safeReadOpPoint_stringListToJson(reverse(issues))
            "}"
        )
        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; safeReadVdsatAliasProbeFromResults(psfDir instList)
;;;
;;; Bounded current-PSF probe for the real Spectre OP name that carries
;;; MOS saturation voltage. It reads only caller-provided instance paths
;;; and only the hard-coded names in safeReadOpPoint_vdsatProbeCandidates().
;;; It intentionally does not select 'primitives or 'modelParameter: in
;;; this PDK those files contain model/primitive parameter name tables, and
;;; enumerating/selecting them is both leak-prone and unstable in CIW.
;;; ----------------------------------------------------------------
procedure(safeReadVdsatAliasProbeFromResults(psfDir instList)
    prog((openRv availResults issues resultKinds instanceSelected
          errMsg dcAvail instanceAvail dcResultName dcResultString
          candidate instName h val hits firstVal candidateParts valuesParts
          rowJson actualName actualNameJson trueJson primaryResult altResult
          jsonStr)

        unless(and(stringp(psfDir) (length(psfDir) > 0))
            return("{\"ok\":false,\"error\":\"safeReadVdsatAliasProbeFromResults requires a non-empty psfDir string\"}")
        )
        errMsg = safeReadOpPoint_validatePathList(instList "instList" 128)
        when(errMsg
            return(strcat("{\"ok\":false,\"error\":\""
                          safeHelpers_jsonEscape(errMsg) "\"}"))
        )
        unless(and(listp(instList) greaterp(length(instList) 0))
            return("{\"ok\":false,\"error\":\"safeReadVdsatAliasProbeFromResults requires at least one instance path\"}")
        )

        openRv = errset(openResults(psfDir) t)
        unless(and(openRv car(openRv))
            return("{\"ok\":false,\"error\":\"openResults failed for requested PSF directory\"}")
        )

        availResults = nil
        let((rv)
            rv = errset(results() t)
            when(and(rv listp(car(rv)))
                availResults = car(rv)
            )
        )

        dcResultName = nil
        foreach(candidate list('dc 'dcOp 'dcOpInfo 'finalTimeOP)
            when(and(null(dcResultName)
                     safeReadOpPoint_resultAvailable(availResults candidate))
                dcResultName = candidate
            )
        )
        dcAvail = if(dcResultName then t else nil)
        if(dcResultName then
            dcResultString = symbolToString(dcResultName)
        else
            dcResultString = ""
        )
        instanceAvail = safeReadOpPoint_resultAvailable(availResults 'instance)
        unless(or(dcAvail instanceAvail)
            safeReadOpPoint_unselectIfCallable()
            return("{\"ok\":false,\"error\":\"no dc/instance operating-point result available in opened PSF results\"}")
        )

        issues = nil
        resultKinds = nil
        when(dcAvail
            resultKinds = cons(dcResultString resultKinds)
        )
        when(instanceAvail
            resultKinds = cons("instance" resultKinds)
        )

        if(instanceAvail then
            instanceSelected = errset(selectResult('instance) t)
            unless(instanceSelected
                issues = cons("selectResult('instance) failed; using operating-point result fallback" issues)
            )
        else
            instanceSelected = nil
        )
        unless(instanceSelected
            when(dcResultName
                unless(errset(selectResult(dcResultName) t)
                    issues = cons("selectResult operating-point fallback failed" issues)
                )
            )
        )

        actualName = nil
        candidateParts = nil
        foreach(candidate safeReadOpPoint_vdsatProbeCandidates()
            hits = 0
            firstVal = nil
            valuesParts = nil
            foreach(instName instList
                h = nil
                if(instanceSelected then
                    let((rv)
                        rv = errset(getData(instName ?result "instance") t)
                        when(and(rv car(rv))
                            h = car(rv)
                        )
                    )
                    primaryResult = "instance"
                    altResult = dcResultString
                else
                    when(dcResultName
                        let((rv)
                            rv = errset(getData(instName ?result dcResultString) t)
                            when(and(rv car(rv))
                                h = car(rv)
                            )
                        )
                    )
                    primaryResult = dcResultString
                    altResult = nil
                )

                val = safeReadOpPoint_readCandidateParam(
                    instName candidate h primaryResult altResult
                )
                when(numberp(val)
                    hits = hits + 1
                    unless(numberp(firstVal)
                        firstVal = val
                    )
                    valuesParts = cons(
                        strcat("\"" safeHelpers_jsonEscape(instName) "\":"
                               safeHelpers_numToJson(val))
                        valuesParts
                    )
                )
            )

            when(and(null(actualName)
                     safeReadOpPoint_trueVdsatCandidatep(candidate)
                     greaterp(hits 0))
                actualName = candidate
            )
            trueJson = if(safeReadOpPoint_trueVdsatCandidatep(candidate)
                         then "true" else "false")
            rowJson = strcat(
                "{\"hits\":" sprintf(nil "%d" hits)
                ",\"example\":" if(numberp(firstVal)
                                   then safeHelpers_numToJson(firstVal)
                                   else "null")
                ",\"trueVdsat\":" trueJson
                ",\"values\":{"
                buildString(reverse(valuesParts) ",")
                "}}"
            )
            candidateParts = cons(
                strcat("\"" safeHelpers_jsonEscape(candidate) "\":" rowJson)
                candidateParts
            )
        )

        safeReadOpPoint_unselectIfCallable()

        actualNameJson = if(stringp(actualName)
                           then strcat("\"" safeHelpers_jsonEscape(actualName) "\"")
                           else "null")
        jsonStr = strcat(
            "{"
            "\"ok\":true,"
            "\"source\":\"psf\","
            "\"analysis\":\"dc\","
            "\"resultKinds\":" safeReadOpPoint_stringListToJson(reverse(resultKinds)) ","
            "\"instancesTested\":" sprintf(nil "%d" length(instList)) ","
            "\"actualName\":" actualNameJson ","
            "\"candidates\":{" buildString(reverse(candidateParts) ",") "},"
            "\"issues\":" safeReadOpPoint_stringListToJson(reverse(issues))
            "}"
        )
        return(jsonStr)
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
        (vdssat  h->vdssat)
        (vdsat_eff h->vdsat_eff)
        (vdsatEff h->vdsatEff)
        (Vdsat   h->Vdsat)
        (vdsatcv h->vdsatcv)
        (vdsatCV h->vdsatCV)
        (vdsat_cv h->vdsat_cv)
        (vdsatc  h->vdsatc)
        (vdsatC  h->vdsatC)
        (vdsat_c h->vdsat_c)
        (VDSAT   h->VDSAT)
        (vdsat_vadj h->vdsat_vadj)
        (vdsat_vdl h->vdsat_vdl)
        ;; Nearby diagnostics only; never canonicalized to true vdsat.
        (vdseff  h->vdseff)
        (vdsEff  h->vdsEff)
        (vds_eff h->vds_eff)
        (vgst    h->vgst)
        (vgsteff h->vgsteff)
        (vgstEff h->vgstEff)
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


procedure(safeReadOpPoint_unselectIfCallable()
    when(isCallable('unselectResult)
        errset(unselectResult() t)
    )
    t
)

procedure(safeReadOpPoint_resultAvailable(resultList resultName)
    let((found item itemStr wantStr)
        found = nil
        wantStr = symbolToString(resultName)
        when(listp(resultList)
            foreach(item resultList
                unless(found
                    cond(
                        (listp(item)
                            when(safeReadOpPoint_resultAvailable(item resultName)
                                found = t
                            )
                        )
                        (t
                            itemStr = cond(
                                (symbolp(item) symbolToString(item))
                                (stringp(item) item)
                                (t sprintf(nil "%L" item))
                            )
                            when(equal(itemStr wantStr)
                                found = t
                            )
                        )
                    )
                )
            )
        )
        found
    )
)

procedure(safeReadOpPointAfterTran(psfDir)
    prog((origSelect availResults instNames safeInstData allInstData
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

        ;; Avoid noisy OCN-6038/6034/6082 warnings: selectResult('tranOp)
        ;; prints CIW warnings when tranOp is absent, even inside errset.
        availResults = nil
        when(and(origSelect car(origSelect))
            availResults = car(origSelect)
        )
        unless(safeReadOpPoint_resultAvailable(availResults 'tranOp)
            safeReadOpPoint_unselectIfCallable()
            return("{\"error\":\"tranOp result not available in opened PSF results\"}")
        )

        ;; Switch into tranOp. If selectResult returns nil or errors out,
        ;; bail with an explicit error string.
        unless(errset(selectResult('tranOp) t)
            safeReadOpPoint_unselectIfCallable()
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
            safeReadOpPoint_unselectIfCallable()
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
                        paramVal = safeReadOpPoint_getNumericParam(
                            instName safeKey h "tranOp" nil
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
        safeReadOpPoint_unselectIfCallable()

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
