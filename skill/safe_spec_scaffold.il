;;; safe_spec_scaffold.il - Extract spec-scaffolding data from a DUT +
;;; testbench pair, PDK-safe. Returns only user-authored identifiers:
;;; top-level pin names + directions for both the DUT cell and the
;;; testbench cell. NO instance properties, NO model cards, NO param
;;; values. Design variables and analyses come from input.scs via the
;;; pre-existing safeOceanListDesignVars / safeOceanListAnalyses helpers
;;; in safe_ocean.il - the Python wrapper composes them.
;;;
;;; Compatible with Cadence IC23.1 SKILL (no SKILL++ syntax).
;;; Requires: helpers.il loaded first.
;;;
;;; Usage:
;;;   safeGenerateSpecScaffold("pll" "LC_VCO" "LC_VCO_tb")
;;;   => JSON string {"ok":true, "dut":{...}, "tb":{...}}
;;;
;;; JSON return shape:
;;;   {
;;;     "ok": true,
;;;     "dut": {
;;;       "lib": "GENERIC_PDK",
;;;       "cell": "<alias>",
;;;       "pins": [{"name":"Vout_p","direction":"output"}, ...]
;;;     },
;;;     "tb":  { same shape, for the testbench cell }
;;;   }
;;;   {"ok": false, "error": "..."} on any failure.
;;;
;;; PDK safety:
;;;   - Instance list is NOT walked: we never emit foundry cell names,
;;;     w/l/nf values, or BSIM model params. Only top-level terminals
;;;     of the cellview are returned - those are user-authored by the
;;;     schematic entry author, never foundry-private.
;;;   - cellName is run through safeHelpers_cellAlias as defense in
;;;     depth (the DUT cell is typically the user's own name like
;;;     "LC_VCO", but aliasing keeps a corrupted call from leaking).
;;;   - lib is always replaced with "GENERIC_PDK" on output.

procedure(safeGenerateSpecScaffold(libName cellName tbCellName)
    prog((dutData tbData jsonStr)

        ;; Input validation.
        unless(and(stringp(libName) stringp(cellName) stringp(tbCellName))
            return("{\"ok\":false,\"error\":\"lib/cell/tb_cell must be strings\"}")
        )

        dutData = safeSpecScaffold_readCellPins(libName cellName)
        unless(dutData
            return(sprintf(nil
                "{\"ok\":false,\"error\":\"Cannot open DUT cellView %s/%s\"}"
                libName cellName))
        )

        tbData = safeSpecScaffold_readCellPins(libName tbCellName)
        unless(tbData
            return(sprintf(nil
                "{\"ok\":false,\"error\":\"Cannot open TB cellView %s/%s\"}"
                libName tbCellName))
        )

        jsonStr = strcat(
            "{\"ok\":true,"
            "\"dut\":" dutData ","
            "\"tb\":"  tbData
            "}"
        )
        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; Internal: open a cellView read-only, emit {lib, cell, pins} JSON.
;;; Returns nil on open failure so the caller can frame the error.
;;; cellView is always closed before return.
;;; ----------------------------------------------------------------
procedure(safeSpecScaffold_readCellPins(libName cellName)
    prog((cv pinList term pinEntry genericCell pinsJson jsonStr)
        cv = dbOpenCellViewByType(libName cellName "schematic" nil "r")
        unless(cv
            return(nil)
        )

        pinList = nil
        foreach(term cv~>terminals
            pinEntry = list(term~>name
                           if(term~>direction then term~>direction else "unknown"))
            pinList = cons(pinEntry pinList)
        )
        pinList = reverse(pinList)

        dbClose(cv)

        if(safeHelpers_isGenericName(cellName) then
            genericCell = cellName
        else
            genericCell = safeHelpers_cellAlias(cellName)
        )
        ;; The DUT / testbench cell names are user-authored, not foundry
        ;; cellnames, so safeHelpers_cellAlias will fall back to
        ;; "GENERIC_DEVICE" for them - that would be unhelpful. Keep the
        ;; user's name when the map doesn't know it AND the name passes
        ;; the safe-identifier pattern. Otherwise use the generic alias.
        when(and(equal(genericCell safeHelpers_genericCellName)
                 safeHelpers_matchesSafeVarPattern(cellName))
            genericCell = cellName
        )

        pinsJson = safeSpecScaffold_pinsToJson(pinList)
        jsonStr = strcat(
            "{"
            "\"lib\":\"GENERIC_PDK\","
            "\"cell\":\"" safeHelpers_jsonEscape(genericCell) "\","
            "\"pins\":" pinsJson
            "}"
        )
        return(jsonStr)
    )
)


;;; ----------------------------------------------------------------
;;; Internal: convert pin list (entries = (name directionSymbol)) to
;;; a JSON array of {"name":..,"direction":..} objects.
;;; ----------------------------------------------------------------
procedure(safeSpecScaffold_pinsToJson(pinList)
    let((parts pin nameStr dirStr)
        if(pinList == nil then
            "[]"
        else
            parts = nil
            foreach(pin pinList
                nameStr = car(pin)
                dirStr  = cadr(pin)
                ;; term~>direction returns a symbol in IC23.1 (e.g.
                ;; 'input / 'output / 'inputOutput). Stringify defensively
                ;; so the JSON value is always a literal string.
                when(symbolp(dirStr)
                    dirStr = symbolToString(dirStr)
                )
                unless(stringp(dirStr)
                    dirStr = "unknown"
                )
                unless(stringp(nameStr)
                    nameStr = ""
                )
                parts = cons(
                    strcat("{"
                        "\"name\":\""      safeHelpers_jsonEscape(nameStr) "\","
                        "\"direction\":\"" safeHelpers_jsonEscape(dirStr)  "\""
                    "}")
                    parts)
            )
            strcat("[" buildString(reverse(parts) ",") "]")
        )
    )
)


printf("safe_spec_scaffold.il loaded successfully.\n")
