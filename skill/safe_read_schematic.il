;;; safe_read_schematic.il — Read cellView topology, return only safe data
;;; Compatible with Cadence IC23.1 SKILL (no SKILL++ syntax)
;;;
;;; Requires: helpers.il loaded first
;;;
;;; Usage:
;;;   safeReadSchematic("myLib" "myCell")
;;;   => Returns JSON string with sanitized schematic data
;;;
;;; Security guarantees:
;;;   - Foundry cell names replaced with generic aliases (NMOS/PMOS/etc.)
;;;   - Library names replaced with "GENERIC_PDK"
;;;   - Only whitelisted design parameters returned (w, l, nf, m, multi, wf)
;;;   - No BSIM4 model parameters leak

procedure(safeReadSchematic(libName cellName @optional (viewName "schematic"))
    prog((cv instances instList inst instData cellN genericCell
         params safeParams paramName paramVal nets netList
         pin pinData resultPairs jsonStr)

        ;; Input validation
        unless(and(stringp(libName) stringp(cellName) stringp(viewName))
            return(strcat("{\"error\":\"Invalid arguments: lib, cell, view must be strings\"}"))
        )

        ;; Open the cellView read-only
        cv = dbOpenCellViewByType(libName cellName viewName nil "r")
        unless(cv
            return(sprintf(nil "{\"error\":\"Cannot open cellView %s/%s/%s\"}"
                libName cellName viewName))
        )

        ;; ---- Collect instances ----
        instances = cv~>instances
        instList = nil

        foreach(inst instances
            ;; Get the foundry cell name and map to generic
            cellN = inst~>cellName
            ;; Skip schematic decoration (ground/supply symbols, I/O pins).
            ;; These are not DUT devices; leaving them in pollutes the
            ;; LLM-facing topology with non-actionable rows like
            ;; "I3 (GND_SYM) gnd!->gnd!".
            unless(member(cellN safeHelpers_skipCells)
                if(safeHelpers_isGenericName(cellN) then
                    genericCell = cellN
                else
                    genericCell = safeHelpers_cellAlias(cellN)
                )

                ;; Extract only whitelisted parameters
                params = inst~>prop
                safeParams = nil
                foreach(param params
                    paramName = lowerCase(param~>name)
                    when(member(stringToSymbol(paramName) safeHelpers_allowedParams)
                        paramVal = param~>value
                        cond(
                            (stringp(paramVal)
                                safeParams = cons(list(paramName paramVal) safeParams)
                            )
                            (numberp(paramVal)
                                safeParams = cons(list(paramName paramVal) safeParams)
                            )
                        )
                    )
                )
                safeParams = reverse(safeParams)

                ;; Collect net connections for this instance
                nets = nil
                foreach(instTerm inst~>instTerms
                    nets = cons(
                        list(instTerm~>name
                             if(instTerm~>net then instTerm~>net~>name else "UNCONNECTED"))
                        nets
                    )
                )
                nets = reverse(nets)

                ;; Build the instance data as assoc list
                instData = list(
                    list("instName"  inst~>name)
                    list("cell"      genericCell)
                    list("lib"       "GENERIC_PDK")
                    list("params"    safeParams)
                    list("nets"      nets)
                )
                instList = cons(instData instList)
            )
        )
        instList = reverse(instList)

        ;; ---- Collect top-level pins ----
        pinData = nil
        foreach(term cv~>terminals
            pin = list(
                list("name"      term~>name)
                list("direction" term~>direction)
            )
            pinData = cons(pin pinData)
        )
        pinData = reverse(pinData)

        ;; ---- Build JSON result ----
        ;; We build it manually for the top level since it has arrays
        jsonStr = strcat(
            "{"
            "\"cell\":\"" safeHelpers_jsonEscape(cellName) "\","
            "\"lib\":\"GENERIC_PDK\","
            "\"view\":\"" safeHelpers_jsonEscape(viewName) "\","
            "\"instances\":" safeReadSchematic_instancesToJson(instList) ","
            "\"pins\":" safeReadSchematic_pinsToJson(pinData)
            "}"
        )

        ;; Close the cellView
        dbClose(cv)

        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; Internal: convert instance list to JSON array
;;; ----------------------------------------------------------------
procedure(safeReadSchematic_instancesToJson(instList)
    let((parts inst jsonInst paramsJson netsJson)
        if(instList == nil then
            "[]"
        else
            parts = nil
            foreach(inst instList
                ;; inst is a list of (key value) pairs
                ;; Extract fields by position (instName, cell, lib, params, nets)
                paramsJson = safeReadSchematic_paramsToJson(cadr(nth(3 inst)))
                netsJson   = safeReadSchematic_netsToJson(cadr(nth(4 inst)))

                jsonInst = strcat(
                    "{"
                    "\"instName\":\"" safeHelpers_jsonEscape(cadr(car(inst))) "\","
                    "\"cell\":\""     safeHelpers_jsonEscape(cadr(cadr(inst))) "\","
                    "\"lib\":\""      safeHelpers_jsonEscape(cadr(nth(2 inst))) "\","
                    "\"params\":"     paramsJson ","
                    "\"nets\":"       netsJson
                    "}"
                )
                parts = cons(jsonInst parts)
            )
            strcat("[" buildString(reverse(parts) ",") "]")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Internal: convert params assoc list to JSON object
;;; ----------------------------------------------------------------
procedure(safeReadSchematic_paramsToJson(paramsList)
    let((parts entry k v)
        if(paramsList == nil then
            "{}"
        else
            parts = nil
            foreach(entry paramsList
                k = safeHelpers_jsonEscape(car(entry))
                v = cadr(entry)
                cond(
                    (numberp(v)
                        parts = cons(
                            strcat("\"" k "\":" safeHelpers_numToJson(v))
                            parts)
                    )
                    (stringp(v)
                        parts = cons(
                            strcat("\"" k "\":\"" safeHelpers_jsonEscape(v) "\"")
                            parts)
                    )
                )
            )
            strcat("{" buildString(reverse(parts) ",") "}")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Internal: convert nets list to JSON object {termName: netName}
;;; ----------------------------------------------------------------
procedure(safeReadSchematic_netsToJson(netsList)
    let((parts entry)
        if(netsList == nil then
            "{}"
        else
            parts = nil
            foreach(entry netsList
                parts = cons(
                    strcat("\"" safeHelpers_jsonEscape(car(entry))
                           "\":\"" safeHelpers_jsonEscape(cadr(entry)) "\"")
                    parts)
            )
            strcat("{" buildString(reverse(parts) ",") "}")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Internal: convert pins list to JSON array
;;; ----------------------------------------------------------------
procedure(safeReadSchematic_pinsToJson(pinsList)
    let((parts pin)
        if(pinsList == nil then
            "[]"
        else
            parts = nil
            foreach(pin pinsList
                parts = cons(
                    strcat("{"
                        "\"name\":\"" safeHelpers_jsonEscape(cadr(car(pin))) "\","
                        "\"direction\":\"" safeHelpers_jsonEscape(cadr(cadr(pin))) "\""
                    "}")
                    parts)
            )
            strcat("[" buildString(reverse(parts) ",") "]")
        )
    )
)


;;; ================================================================
;;; safeReadSchematicDeep — hierarchical, same-library BFS traversal
;;; ----------------------------------------------------------------
;;; Returns the root cellview plus every unique user subcell reached
;;; by following same-library instance masters, up to ``maxDepth``.
;;;
;;; Shape:
;;;   {
;;;     "ok": true,
;;;     "root": { "lib": "GENERIC_PDK", "cell": "<root>",
;;;               "handle": "ROOT", "depth": 0,
;;;               "instances": [...], "pins": [...] },
;;;     "subcells": [ { "handle":"SUBCELL_0", "lib":"GENERIC_PDK",
;;;                     "cell":"...", "depth":1,
;;;                     "instances":[...], "pins":[...] }, ... ],
;;;     "max_depth": <caller arg>,
;;;     "max_depth_reached": <deepest depth actually visited>,
;;;     "depth_limit_hit": <t|nil>
;;;   }
;;;
;;; Instance rows extend the safeReadSchematic shape with an optional
;;;   "subcell":"SUBCELL_N"
;;; field when the master cellview is a same-library schematic that
;;; will appear in the subcells[] list. Otherwise the row carries
;;; "cell":"NMOS" / "GENERIC_DEVICE" / ... exactly as safeReadSchematic
;;; would have emitted it.
;;;
;;; PDK safety invariants (mirrors safeReadSchematic):
;;;   - Cross-library masters are NEVER opened — they stay leaves with
;;;     cellAlias() naming. "foundry cell name never leaves remote host".
;;;   - Only whitelisted instance params (w, l, nf, ...) are exposed.
;;;   - Net names + terminal names + user-authored subcell names are
;;;     the only identifiers that flow outward. All are user-authored.
;;;   - Subcell names that fail safeHelpers_matchesSafeVarPattern are
;;;     NOT emitted — the SUBCELL_N handle is the only way to name it.
;;;
;;; BFS state is prog-local (no side-effect on safeHelpers_* defvars).
;;; ================================================================


;;; ----------------------------------------------------------------
;;; Internal: walk a single cellview, produce its per-cell JSON dict.
;;; Assigns new SUBCELL handles as it encounters unseen masters, and
;;; enqueues them for later traversal when depth allows.
;;;
;;; Returns a 4-element list:
;;;   ( jsonStr  newSeenMap  newQueue  depthLimitHit )
;;;
;;; Queue and seenMap are accumulators passed in.
;;; ----------------------------------------------------------------
procedure(safeReadSchematicDeep_walkOne(
        cv rootLibName myHandle myDepth maxDepth seenMap queue
        nextSubcellId depthLimitHit)
    prog((instances instList inst instData cellN libN genericCell
          params safeParams paramName paramVal nets
          pinData term pin subCv key existing handle
          jsonInstances jsonPins jsonStr
          outSeenMap outQueue outNextId outHit subcellRef)
        outSeenMap  = seenMap
        outQueue    = queue
        outNextId   = nextSubcellId
        outHit      = depthLimitHit

        ;; ---- Collect instances ----
        instances = cv~>instances
        instList = nil

        foreach(inst instances
            cellN = inst~>cellName
            libN  = inst~>libName
            unless(member(cellN safeHelpers_skipCells)
                genericCell = nil
                subcellRef  = nil

                cond(
                    ;; Cross-library master: never recurse, alias only.
                    (not(equal(libN rootLibName))
                        if(safeHelpers_isGenericName(cellN) then
                            genericCell = cellN
                        else
                            genericCell = safeHelpers_cellAlias(cellN)
                        )
                    )
                    ;; Same-lib, named primitive (NMOS / PMOS / ...).
                    (safeHelpers_isGenericName(cellN)
                        genericCell = cellN
                    )
                    ;; Same-lib, user cell name fails safe-var pattern:
                    ;; cannot be emitted; fall back to alias, no recurse.
                    (not(safeHelpers_matchesSafeVarPattern(cellN))
                        genericCell = safeHelpers_cellAlias(cellN)
                    )
                    ;; Same-lib, safe-pattern name — open ONCE, keep cv
                    ;; in the queue (closed by main BFS loop after the
                    ;; walkOne invocation that serializes it). This
                    ;; guarantees every emitted SUBCELL_n ref has a
                    ;; matching subcells[] entry — no dangling refs.
                    (t
                        key = strcat(libN "/" cellN)
                        existing = assoc(key outSeenMap)
                        if(existing then
                            ;; Already allocated on a shallower path:
                            ;; reuse handle, no re-open.
                            subcellRef = cadr(existing)
                        else
                            ;; Depth budget check BEFORE opening. At cap
                            ;; we do NOT allocate a handle and do NOT
                            ;; open — the row collapses to a leaf using
                            ;; cellAlias, and depth_limit_hit is set.
                            if(myDepth + 1 <= maxDepth then
                                subCv = dbOpenCellViewByType(
                                    libN cellN "schematic" nil "r")
                                if(subCv then
                                    handle = sprintf(nil "SUBCELL_%d"
                                                     outNextId)
                                    outNextId = outNextId + 1
                                    outSeenMap = cons(
                                        list(key handle libN cellN)
                                        outSeenMap)
                                    ;; Queue entry now carries the open
                                    ;; cv as its 3rd element so the main
                                    ;; BFS loop never re-opens. Keep
                                    ;; subCv OPEN; main loop closes it
                                    ;; after walkOne serializes it.
                                    outQueue = append(outQueue
                                        list(list(handle myDepth+1 subCv
                                                  libN cellN)))
                                    subcellRef = handle
                                else
                                    ;; No schematic view — leaf, alias.
                                    genericCell = safeHelpers_cellAlias(
                                        cellN)
                                )
                            else
                                ;; Depth cap — no open, no handle.
                                outHit = t
                                genericCell = safeHelpers_cellAlias(cellN)
                            )
                        )
                    )
                )

                ;; Extract whitelisted params (same logic as flat reader).
                params = inst~>prop
                safeParams = nil
                foreach(param params
                    paramName = lowerCase(param~>name)
                    when(member(stringToSymbol(paramName)
                                 safeHelpers_allowedParams)
                        paramVal = param~>value
                        cond(
                            (stringp(paramVal)
                                safeParams = cons(
                                    list(paramName paramVal) safeParams))
                            (numberp(paramVal)
                                safeParams = cons(
                                    list(paramName paramVal) safeParams))
                        )
                    )
                )
                safeParams = reverse(safeParams)

                ;; Net connections.
                nets = nil
                foreach(instTerm inst~>instTerms
                    nets = cons(
                        list(instTerm~>name
                             if(instTerm~>net
                                then instTerm~>net~>name
                                else "UNCONNECTED"))
                        nets
                    )
                )
                nets = reverse(nets)

                instData = list(
                    list("instName"  inst~>name)
                    list("cell"      genericCell)
                    list("subcell"   subcellRef)
                    list("lib"       "GENERIC_PDK")
                    list("params"    safeParams)
                    list("nets"      nets)
                )
                instList = cons(instData instList)
            )
        )
        instList = reverse(instList)

        ;; ---- Top-level pins ----
        pinData = nil
        foreach(term cv~>terminals
            pin = list(
                list("name"      term~>name)
                list("direction" term~>direction)
            )
            pinData = cons(pin pinData)
        )
        pinData = reverse(pinData)

        jsonInstances = safeReadSchematicDeep_instancesToJson(instList)
        jsonPins      = safeReadSchematic_pinsToJson(pinData)

        jsonStr = strcat(
            "{"
            "\"handle\":\""  safeHelpers_jsonEscape(myHandle) "\","
            "\"depth\":"     sprintf(nil "%d" myDepth) ","
            "\"lib\":\"GENERIC_PDK\","
            "\"cell\":\""    safeHelpers_jsonEscape(cv~>cellName) "\","
            "\"instances\":" jsonInstances ","
            "\"pins\":"      jsonPins
            "}"
        )

        return(list(jsonStr outSeenMap outQueue outNextId outHit))
    )
)


;;; ----------------------------------------------------------------
;;; Internal: per-instance JSON serializer. Same as the flat one but
;;; also emits an optional "subcell" field when a handle is attached.
;;; ----------------------------------------------------------------
procedure(safeReadSchematicDeep_instancesToJson(instList)
    let((parts inst paramsJson netsJson subRef jsonInst cellVal)
        if(instList == nil then
            "[]"
        else
            parts = nil
            foreach(inst instList
                ;; inst layout: instName cell subcell lib params nets
                paramsJson = safeReadSchematic_paramsToJson(cadr(nth(4 inst)))
                netsJson   = safeReadSchematic_netsToJson(cadr(nth(5 inst)))
                subRef     = cadr(nth(2 inst))
                cellVal    = cadr(cadr(inst))

                jsonInst = strcat(
                    "{"
                    "\"instName\":\"" safeHelpers_jsonEscape(cadr(car(inst)))
                    "\","
                )
                if(cellVal then
                    jsonInst = strcat(jsonInst
                        "\"cell\":\"" safeHelpers_jsonEscape(cellVal) "\",")
                )
                if(subRef then
                    jsonInst = strcat(jsonInst
                        "\"subcell\":\"" safeHelpers_jsonEscape(subRef) "\",")
                )
                jsonInst = strcat(jsonInst
                    "\"lib\":\""      safeHelpers_jsonEscape(cadr(nth(3 inst)))
                    "\","
                    "\"params\":"     paramsJson ","
                    "\"nets\":"       netsJson
                    "}"
                )
                parts = cons(jsonInst parts)
            )
            strcat("[" buildString(reverse(parts) ",") "]")
        )
    )
)


procedure(safeReadSchematicDeep(libName rootCellName @optional
                                 (maxDepth 10) (viewName "schematic"))
    prog((cv seenMap queue entry handle depth lib cell
          walkRes jsonStr rootJson subcellJsons
          nextSubcellId depthLimitHit maxDepthReached
          effectiveMax)
        unless(and(stringp(libName) stringp(rootCellName)
                    stringp(viewName))
            return(strcat("{\"ok\":false,\"error\":"
                          "\"Invalid arguments: lib, cell, view must be "
                          "strings\"}"))
        )
        unless(fixp(maxDepth)
            return(strcat("{\"ok\":false,\"error\":"
                          "\"maxDepth must be an integer\"}"))
        )
        ;; Clamp to [1, 50]. Lower bound tightened in H1 round-2 after
        ;; codex review: `max_depth=0` is redundant with the flat
        ;; `--depth 1` path and was dropped from the public contract.
        ;; 50 is an arbitrary hard cap that keeps BFS bounded even if a
        ;; same-lib cycle somehow bypasses the seenMap guard.
        effectiveMax = maxDepth
        when(effectiveMax < 1  effectiveMax = 1)
        when(effectiveMax > 50 effectiveMax = 50)

        cv = dbOpenCellViewByType(libName rootCellName viewName nil "r")
        unless(cv
            return(sprintf(nil "{\"ok\":false,\"error\":"
                               "\"Cannot open cellView %s/%s/%s\"}"
                libName rootCellName viewName))
        )

        seenMap = list(list(strcat(libName "/" rootCellName) "ROOT"
                            libName rootCellName))
        queue = nil
        nextSubcellId = 0
        depthLimitHit = nil
        maxDepthReached = 0

        walkRes = safeReadSchematicDeep_walkOne(
            cv libName "ROOT" 0 effectiveMax seenMap queue
            nextSubcellId depthLimitHit)
        rootJson       = nth(0 walkRes)
        seenMap        = nth(1 walkRes)
        queue          = nth(2 walkRes)
        nextSubcellId  = nth(3 walkRes)
        depthLimitHit  = nth(4 walkRes)
        dbClose(cv)

        ;; BFS queue entries are 5-tuples:
        ;;   (handle depth cv libName cellName)
        ;; The cv was opened — and guaranteed non-nil — by the walkOne
        ;; invocation that enqueued it. Main loop pops, serializes via
        ;; walkOne, then dbCloses the cv. Every enqueued (handle, cv)
        ;; therefore becomes exactly one subcells[] JSON entry, so the
        ;; invariant "every SUBCELL_n ref has a matching subcells entry"
        ;; holds by construction.
        subcellJsons = nil
        while(queue
            entry  = car(queue)
            queue  = cdr(queue)
            handle = nth(0 entry)
            depth  = nth(1 entry)
            cv     = nth(2 entry)
            lib    = nth(3 entry)
            cell   = nth(4 entry)
            when(depth > maxDepthReached  maxDepthReached = depth)
            walkRes = safeReadSchematicDeep_walkOne(
                cv libName handle depth effectiveMax seenMap queue
                nextSubcellId depthLimitHit)
            subcellJsons  = cons(nth(0 walkRes) subcellJsons)
            seenMap       = nth(1 walkRes)
            queue         = nth(2 walkRes)
            nextSubcellId = nth(3 walkRes)
            depthLimitHit = nth(4 walkRes)
            dbClose(cv)
        )
        subcellJsons = reverse(subcellJsons)

        return(strcat(
            "{"
            "\"ok\":true,"
            "\"root\":" rootJson ","
            "\"subcells\":["
            if(subcellJsons then buildString(subcellJsons ",") else "")
            "],"
            "\"max_depth\":"          sprintf(nil "%d" effectiveMax)   ","
            "\"max_depth_reached\":"  sprintf(nil "%d" maxDepthReached) ","
            "\"depth_limit_hit\":"    if(depthLimitHit "true" "false")
            "}"
        ))
    )
)


printf("safe_read_schematic.il loaded successfully.\n")
