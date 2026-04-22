;;; safe_set_param.il — Safely set instance parameters with whitelist validation
;;; Compatible with Cadence IC23.1 SKILL (no SKILL++ syntax)
;;;
;;; Requires: helpers.il loaded first
;;;
;;; Usage:
;;;   safeSetParam("myLib" "myCell" "M0" list(list("w" "10u") list("l" "500n")))
;;;   => Returns JSON string with success/failure result
;;;
;;; Security guarantees:
;;;   - Only whitelisted parameters accepted (w, l, nf, m, multi, wf)
;;;   - Parameter values validated against injection patterns
;;;   - Instance/cell/lib names validated for safe characters
;;;   - Returns JSON result — never raises unhandled errors

;;; ----------------------------------------------------------------
;;; Name validation — only alphanumeric, underscore, dot, hyphen
;;; Matches Python-side _NAME_RE = r"^[a-zA-Z0-9_.\-]+$"
;;; ----------------------------------------------------------------
procedure(safeSetParam_validateName(name label)
    ;; Rev 6.2.2 (Route C, pcreMatchp + \z, dual-APPROVE'd 2026-04-17).
    ;; Mirrors Python _NAME_RE = r"^[a-zA-Z0-9_.\-]+\Z" — single source
    ;; of truth for the accept set. \z (not $) rejects trailing \n,
    ;; closing the defense-in-depth gap that $ would leave open
    ;; ("LC_VCO_tb\n" would otherwise slip through). Probe 5a confirmed
    ;; \z is supported on this IC23.1 install.
    if(and(stringp(name) strlen(name) > 0)
        pcreMatchp("^[a-zA-Z0-9_.\\-]+\\z" name)
        nil
    )
)


;;; ----------------------------------------------------------------
;;; Main entry point: set parameters on an instance
;;;
;;; Arguments:
;;;   libName   - library name (string)
;;;   cellName  - cell name (string)
;;;   instName  - instance name (string)
;;;   paramList - list of (paramName paramValue) pairs
;;;               e.g. list(list("w" "10u") list("l" "500n") list("nf" 4))
;;;
;;; Returns: JSON string with result
;;; ----------------------------------------------------------------
procedure(safeSetParam(libName cellName instName paramList
                       @optional (viewName "schematic"))
    prog((cv inst errors validated paramName paramVal paramNameLower
         setCount resultJson)

        ;; ---- Validate all names ----
        unless(safeSetParam_validateName(libName "lib")
            return("{\"ok\":false,\"error\":\"Invalid library name\"}")
        )
        unless(safeSetParam_validateName(cellName "cell")
            return("{\"ok\":false,\"error\":\"Invalid cell name\"}")
        )
        unless(safeSetParam_validateName(instName "instance")
            return("{\"ok\":false,\"error\":\"Invalid instance name\"}")
        )

        ;; ---- Validate all parameters before making any changes ----
        errors = nil
        validated = nil

        foreach(paramPair paramList
            paramName = car(paramPair)
            paramVal  = cadr(paramPair)

            ;; Check parameter name is a string
            unless(stringp(paramName)
                errors = cons(sprintf(nil "Parameter name must be a string, got: %L" paramName) errors)
            )

            when(stringp(paramName)
                paramNameLower = lowerCase(paramName)

                ;; Check against whitelist
                unless(safeHelpers_validateParamName(paramNameLower)
                    errors = cons(sprintf(nil "Parameter '%s' is not allowed. Allowed: w, l, nf, m, multi, wf" paramName) errors)
                )

                ;; Check value is safe
                unless(safeHelpers_validateParamValue(paramVal)
                    errors = cons(sprintf(nil "Unsafe value for parameter '%s': %L" paramName paramVal) errors)
                )

                ;; Collect validated entries
                when(and(
                    safeHelpers_validateParamName(paramNameLower)
                    safeHelpers_validateParamValue(paramVal)
                )
                    validated = cons(list(paramNameLower paramVal) validated)
                )
            )
        )
        validated = reverse(validated)

        ;; If any validation errors, return immediately without modifying anything
        when(errors
            errors = reverse(errors)
            return(strcat(
                "{\"ok\":false,\"error\":\""
                safeHelpers_jsonEscape(buildString(errors "; "))
                "\",\"rejected_count\":" sprintf(nil "%d" length(errors))
                "}"
            ))
        )

        ;; ---- Open cellView for editing ----
        cv = dbOpenCellViewByType(libName cellName viewName nil "a")
        unless(cv
            return(sprintf(nil "{\"ok\":false,\"error\":\"Cannot open cellView %s/%s/%s for editing\"}"
                libName cellName viewName))
        )

        ;; ---- Find the instance ----
        inst = dbFindInstByName(cv instName)
        unless(inst
            dbClose(cv)
            return(sprintf(nil "{\"ok\":false,\"error\":\"Instance '%s' not found in %s/%s\"}"
                instName cellName viewName))
        )

        ;; ---- Apply each validated parameter ----
        setCount = 0
        foreach(vp validated
            paramNameLower = car(vp)
            paramVal = cadr(vp)

            ;; Convert number to string if needed for cdfSetParam
            when(numberp(paramVal)
                paramVal = sprintf(nil "%.12g" paramVal)
            )

            ;; Use dbSetInstParamValue which is the standard IC23.1 API
            dbSetInstParamValue(inst paramNameLower paramVal)
            setCount = setCount + 1
        )

        ;; Save and close
        dbSave(cv)
        dbClose(cv)

        ;; ---- Return success JSON ----
        resultJson = strcat(
            "{\"ok\":true,"
            "\"instance\":\"" safeHelpers_jsonEscape(instName) "\","
            "\"params_set\":" sprintf(nil "%d" setCount) ","
            "\"details\":" safeSetParam_detailsJson(validated)
            "}"
        )
        return(resultJson)
    )
)


;;; ----------------------------------------------------------------
;;; Internal: build JSON for the details of what was set
;;; ----------------------------------------------------------------
procedure(safeSetParam_detailsJson(validated)
    let((parts entry k v)
        if(validated == nil then
            "{}"
        else
            parts = nil
            foreach(entry validated
                k = car(entry)
                v = cadr(entry)
                cond(
                    (numberp(v)
                        parts = cons(
                            strcat("\"" safeHelpers_jsonEscape(k) "\":"
                                   safeHelpers_numToJson(v))
                            parts)
                    )
                    (stringp(v)
                        parts = cons(
                            strcat("\"" safeHelpers_jsonEscape(k) "\":\""
                                   safeHelpers_jsonEscape(v) "\"")
                            parts)
                    )
                )
            )
            strcat("{" buildString(reverse(parts) ",") "}")
        )
    )
)


;;; ----------------------------------------------------------------
;;; Rev 6.1: load-time self-test for safeSetParam_validateName. Same
;;; purpose as the helpers.il self-tests — catch symbol-vs-int
;;; comparison bugs at load rather than call time. Placed before the
;;; "loaded successfully" printf.
;;; ----------------------------------------------------------------
unless(safeSetParam_validateName("LC_VCO_tb" "cell")
    error("SELF-TEST FAIL: validateName rejected 'LC_VCO_tb'")
)
when(safeSetParam_validateName("bad;rm" "cell")
    error("SELF-TEST FAIL: validateName accepted 'bad;rm'")
)

printf("safe_set_param.il loaded successfully.\n")
