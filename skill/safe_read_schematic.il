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


printf("safe_read_schematic.il loaded successfully.\n")
