;;; safe_maestro.il — Atomic design-variable writeback to the Maestro
;;; (ADE Assembler / ADE Explorer) session that matches a specific
;;; (lib, cell) testbench scope.
;;;
;;; Compatible with Cadence IC23.1 and IC6.1.8 SKILL. API signatures
;;; confirmed from the local virtuoso_bridge reference:
;;;   hiGetWindowList()
;;;   axlGetWindowSession(win)       ;; window -> session string
;;;   hiGetWindowName(win)            ;; window -> title string (contains lib/cell)
;;;   maeSetVar("name" "value" ?session sess)
;;;   maeSaveSetup(?lib "L" ?cell "C" ?view "maestro" ?session sess)
;;;
;;; Requires: helpers.il loaded first.
;;;
;;; Entry point (single, atomic):
;;;   safeMaeWriteAndSave(libName cellName varList)
;;;     varList is list of (name value) pairs.
;;;
;;; Why only one entry point
;;; ------------------------
;;; Previous two-step design (safeMaeSetVars + safeMaeSaveSetup) had two
;;; independent `car(maeGetSessions())` calls. Between them, session-list
;;; ordering is not guaranteed, so the write could land on session A and
;;; the save could land on session B — masking the exact OCEAN-to-Maestro
;;; desynchronization bug this module exists to fix. Merging into one
;;; SKILL call resolves the session once and pins it across write+save.
;;;
;;; Returns JSON:
;;;   {"ok":true, "varsWritten":N, "saved":true}
;;;   {"ok":false, "error":"..."}
;;;
;;; Security guarantees:
;;;   - Caller cannot supply a session handle; session is resolved on
;;;     remote host by matching (libName, cellName) against open ADE window
;;;     titles. LLM cannot target another user's running Maestro.
;;;   - lib/cell names must pass safeSetParam_validateName.
;;;   - Every var name must pass safeHelpers_validateParamName.
;;;   - Every value must pass safeHelpers_validateParamValue.
;;;   - If no Maestro session matches the scope, fail fast with a
;;;     human-actionable message (caller must open Maestro first).
;;;     The SKILL side does NOT auto-open a GUI window — that would
;;;     pop a modal on the user's desktop and risk ASSEMBLER-8127
;;;     lock conflicts documented in virtuoso_bridge/session.py.

;;; ----------------------------------------------------------------
;;; Internal: literal substring test.
;;;
;;; We avoid rexMatchp here because validated scope names are allowed
;;; to contain "." and "-" (see _NAME_RE), which are regex metachars.
;;; `pll.v1` as a regex would spuriously match titles like `pllXv1`,
;;; defeating the precise-session-binding guarantee this layer gives.
;;; ----------------------------------------------------------------
procedure(safeMaestro_substrp(haystack needle)
    let((hayLen needleLen maxStart i found)
        found = nil
        unless(and(stringp(haystack) stringp(needle))
            return(nil)
        )
        hayLen = strlen(haystack)
        needleLen = strlen(needle)
        when(zerop(needleLen)
            return(nil)
        )
        maxStart = hayLen - needleLen + 1
        when(lessp(maxStart 1)
            return(nil)
        )
        for(i 1 maxStart
            unless(found
                when(equal(substring(haystack i needleLen) needle)
                    found = t
                )
            )
        )
        found
    )
)


;;; ----------------------------------------------------------------
;;; Internal: find the open Maestro session whose ADE window title
;;; matches (libName, cellName). Mirrors the approach used in
;;; virtuoso_bridge/maestro/session.py::open_gui_session
;;; (literal substring, not regex).
;;;
;;; `axlGetWindowSession(w)` returning non-nil already proves the
;;; window is an ADE/Maestro session, so the title-level "maestro"
;;; check is redundant and was dropped (it was also a false-negative
;;; risk across localized / mode-specific title variants).
;;;
;;; Returns the session string on match, nil if no open window
;;; corresponds to the scope.
;;; ----------------------------------------------------------------
procedure(safeMaestro_findSessionForScope(libName cellName)
    let((windows matched sess title)
        matched = nil
        errset(windows = hiGetWindowList())
        when(windows
            foreach(w windows
                unless(matched
                    sess = nil
                    title = nil
                    errset(sess = axlGetWindowSession(w))
                    errset(title = hiGetWindowName(w))
                    when(and(stringp(sess) stringp(title))
                        when(and(safeMaestro_substrp(title libName)
                                 safeMaestro_substrp(title cellName))
                            matched = sess
                        )
                    )
                )
            )
        )
        matched
    )
)


;;; ----------------------------------------------------------------
;;; safeMaeWriteAndSave — atomic write + save against the Maestro
;;; session matching (libName, cellName). Write and save both use
;;; the same resolved session string.
;;; cellName here MUST be the testbench cell (the one that owns
;;; the Maestro setup), NOT the DUT schematic cell.
;;;
;;; Dual-write strategy (R13, 2026-04-21):
;;;   1. axlSetVarValue  — best-effort write to the in-memory setup DB.
;;;      NOT persistent across session reopen (5-round probe confirmed
;;;      maeSaveSetup does not persist axl* changes). Kept as defensive
;;;      measure in case a future Cadence version lifts the edit lock.
;;;   2. maeSetVar        — writes the runtime substitution dict (what
;;;      OCEAN's desVar() reads during simulation). This is the path
;;;      that matters for simulation correctness.
;;; mae* is essential; axl* is best-effort / forward-compatible.
;;;
;;; axlSetVarValue signature discovery (probe_desvar_write_api.il):
;;;   Three candidate signatures are tried via errset; the first
;;;   non-nil result wins. Probe on IC23.1 remote host confirmed
;;;   isCallable('axlSetVarValue) = t.
;;; ----------------------------------------------------------------
procedure(safeMaeWriteAndSave(libName cellName varList)
    prog((session sdb errors appliedCount setupDbCount
           varName varVal varNameLower result varHandle
           sigA sigB sigC setupDbNames snapshotParts)

        ;; ---- Validate scope names ----
        unless(safeSetParam_validateName(libName "lib")
            return("{\"ok\":false,\"error\":\"Invalid library name\"}")
        )
        unless(safeSetParam_validateName(cellName "cell")
            return("{\"ok\":false,\"error\":\"Invalid cell name\"}")
        )

        ;; ---- Resolve session once, pin for the whole operation ----
        session = safeMaestro_findSessionForScope(libName cellName)
        unless(session
            return(strcat(
                "{\"ok\":false,\"error\":\""
                "No open Maestro session for scope -- open Maestro for the "
                "scoped testbench cell in ADE before retrying."
                "\"}"
            ))
        )

        ;; ---- Get the setup DB for axl* writes ----
        sdb = nil
        errset(sdb = axlGetMainSetupDB(session))
        ;; sdb may be nil on older Virtuoso; axl* path will be skipped.
        ;; maeSetVar path still runs as fallback.

        ;; ---- Validate all (name value) pairs first ----
        errors = nil
        foreach(pair varList
            varName = car(pair)
            varVal  = cadr(pair)
            unless(stringp(varName)
                errors = cons(sprintf(nil "Var name must be string, got %L" varName) errors)
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

        ;; ---- Apply each var: dual-write (axl* + mae*) ----
        appliedCount = 0
        setupDbCount = 0
        foreach(pair varList
            varName = car(pair)
            varVal  = cadr(pair)
            when(numberp(varVal)
                varVal = sprintf(nil "%.12g" varVal)
            )

            ;; ---- Path 1: axl* setup DB write (GUI + persistence) ----
            when(sdb
                ;; Ensure var exists in setup DB: axlGetVar, create with axlPutVar if needed
                varHandle = nil
                errset(varHandle = axlGetVar(sdb varName))
                unless(varHandle
                    errset(varHandle = axlPutVar(sdb varName))
                )

                ;; Try three axlSetVarValue signatures via errset.
                ;; Signature A: axlSetVarValue(sdb name value)
                ;; Signature B: axlSetVarValue(varHandle value)
                ;; Signature C: axlSetVarValue(sdb varHandle value)
                sigA = nil
                sigB = nil
                sigC = nil
                errset(sigA = axlSetVarValue(sdb varName varVal))
                unless(sigA
                    when(varHandle
                        errset(sigB = axlSetVarValue(varHandle varVal))
                    )
                )
                unless(or(sigA sigB)
                    when(varHandle
                        errset(sigC = axlSetVarValue(sdb varHandle varVal))
                    )
                )
                when(or(sigA sigB sigC)
                    setupDbCount = setupDbCount + 1
                )
            )

            ;; ---- Path 2: maeSetVar runtime dict write (OCEAN compat) ----
            result = nil
            unless(errset(result = maeSetVar(varName varVal ?session session))
                return(strcat(
                    "{\"ok\":false,\"error\":\"maeSetVar raised for '"
                    safeHelpers_jsonEscape(varName) "'\"}"
                ))
            )
            unless(result
                return(strcat(
                    "{\"ok\":false,\"error\":\"maeSetVar returned nil for '"
                    safeHelpers_jsonEscape(varName) "'\"}"
                ))
            )
            appliedCount = appliedCount + 1
        )

        ;; ---- Save against the SAME pinned session ----
        result = nil
        unless(errset(result = maeSaveSetup(?lib libName
                                            ?cell cellName
                                            ?view "maestro"
                                            ?session session))
            return("{\"ok\":false,\"error\":\"maeSaveSetup raised\"}")
        )
        unless(result
            return("{\"ok\":false,\"error\":\"maeSaveSetup returned nil\"}")
        )

        ;; ---- Build setup DB var snapshot for verification ----
        setupDbNames = nil
        when(sdb
            let((varsResult)
                errset(varsResult = axlGetVars(sdb))
                ;; axlGetVars returns (handle (name1 name2 ...))
                when(and(listp(varsResult) cdr(varsResult))
                    setupDbNames = cadr(varsResult)
                )
            )
        )
        snapshotParts = nil
        when(listp(setupDbNames)
            foreach(n setupDbNames
                when(stringp(n)
                    snapshotParts = cons(
                        strcat("\"" safeHelpers_jsonEscape(n) "\"")
                        snapshotParts
                    )
                )
            )
        )

        return(strcat(
            "{\"ok\":true,"
            "\"varsWritten\":" sprintf(nil "%d" appliedCount) ","
            "\"setupDbWritten\":" sprintf(nil "%d" setupDbCount) ","
            "\"saved\":true,"
            "\"setupDbVars\":[" buildString(reverse(snapshotParts) ",") "]"
            "}"
        ))
    )
)


;;; ----------------------------------------------------------------
;;; safeMae_debugInfo — Read-only diagnostic for session binding.
;;; Prints human-readable lines to CIW via printf (avoids JSON
;;; serialization of window titles that may contain control chars).
;;; Returns a short JSON ack so the Python caller doesn't crash.
;;;
;;; R13: prints both maeGetVar (runtime dict) and axlGetVar (setup DB)
;;; side-by-side so we can confirm dual-write landed in both containers.
;;; ----------------------------------------------------------------
procedure(safeMae_debugInfo(libName cellName)
    prog((windows resolvedSess sdb winCount readVal axlHandle)

        printf("\n===== safeMae_debugInfo START =====\n")
        printf("[SCOPE] lib=%L cell=%L\n" libName cellName)

        ;; Enumerate all windows
        winCount = 0
        errset(windows = hiGetWindowList())
        when(windows
            foreach(w windows
                let((sess title hasLib hasCell)
                    sess = nil
                    title = nil
                    errset(sess = axlGetWindowSession(w))
                    errset(title = hiGetWindowName(w))
                    hasLib = nil
                    hasCell = nil
                    when(and(stringp(title) stringp(libName))
                        hasLib = safeMaestro_substrp(title libName)
                    )
                    when(and(stringp(title) stringp(cellName))
                        hasCell = safeMaestro_substrp(title cellName)
                    )
                    printf("[WIN %d] title=%L session=%L matchLib=%L matchCell=%L matchBoth=%L\n"
                           winCount title sess hasLib hasCell and(hasLib hasCell))
                    winCount = winCount + 1
                )
            )
        )
        printf("[WIN-TOTAL] %d windows\n" winCount)

        ;; Resolve session via production code path
        resolvedSess = safeMaestro_findSessionForScope(libName cellName)
        printf("[RESOLVED-SESSION] %L\n" resolvedSess)

        ;; Get setup DB handle
        sdb = nil
        when(stringp(resolvedSess)
            errset(sdb = axlGetMainSetupDB(resolvedSess))
        )
        printf("[SETUP-DB] sdb=%L\n" sdb)

        ;; List all vars in setup DB
        when(sdb
            let((varsResult)
                errset(varsResult = axlGetVars(sdb))
                printf("[SETUP-DB-VARS] %L\n" varsResult)
            )
        )

        ;; Probe vars: maeGetVar (runtime) vs axlGetVar (setup DB)
        when(stringp(resolvedSess)
            foreach(vn '("C" "Ibias" "nfin_cc" "nfin_mirror" "nfin_neg" "nfin_tail" "R" "L")
                readVal = nil
                axlHandle = nil
                errset(readVal = maeGetVar(vn ?session resolvedSess))
                when(sdb
                    errset(axlHandle = axlGetVar(sdb vn))
                )
                printf("[PROBE] %s  maeGetVar=%L  axlGetVar=%L\n" vn readVal axlHandle)
            )
        )

        printf("===== safeMae_debugInfo END =====\n\n")

        return("{\"ok\":true,\"note\":\"see CIW log for window details\"}")
    )
)


printf("safe_maestro.il loaded successfully.\n")
