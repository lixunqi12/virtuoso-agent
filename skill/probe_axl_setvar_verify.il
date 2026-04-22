;;; probe_axl_setvar_verify.il — Second-round probe for Maestro setup DB
;;; design-variable write verification.
;;;
;;; PURPOSE: R13 dual-write reported setupDbWritten:8 but GUI still shows
;;; initial values. We need to:
;;;   A. Find an API that can READ the actual value from a setup DB var handle
;;;   B. Test which axlSetVarValue signature actually mutates the value
;;;      (read-before, write, read-after comparison — not just errset non-nil)
;;;   C. Test GUI refresh candidates
;;;   D. Restore original values at the end
;;;
;;; RUN: In CIW with Maestro open for pll/LC_VCO_tb:
;;;   load("/path/to/probe_axl_setvar_verify.il")
;;;
;;; HOW TO READ OUTPUT:
;;;   - Section A: look for any reader that returns "1f" (the current GUI value
;;;     for C). That's the reader we need.
;;;   - Section B: for each signature, compare readBefore vs readAfter.
;;;     If readAfter == "77.77f", that signature WORKS.
;;;   - Section C: after a successful write, check if GUI updates.
;;;     If a refresh call makes the GUI show "77.77f", we need that call.
;;;   - All values are restored at the end (Section D).

printf("\n===== probe_axl_setvar_verify BEGIN =====\n")

;; ---- Setup: resolve session + sdb + var handle ----
probe_v2_session = safeMaestro_findSessionForScope("pll" "LC_VCO_tb")
printf("[SETUP] session = %L\n" probe_v2_session)

probe_v2_sdb = nil
errset(probe_v2_sdb = axlGetMainSetupDB(probe_v2_session))
printf("[SETUP] sdb = %L\n" probe_v2_sdb)

probe_v2_varH = nil
errset(probe_v2_varH = axlGetVar(probe_v2_sdb "C"))
printf("[SETUP] varHandle for 'C' = %L  type=%L\n" probe_v2_varH type(probe_v2_varH))

;; Also get maeGetVar baseline for comparison
probe_v2_maeVal = nil
errset(probe_v2_maeVal = maeGetVar("C" ?session probe_v2_session))
printf("[SETUP] maeGetVar('C') = %L  (runtime dict baseline)\n" probe_v2_maeVal)


;; ================================================================
;; Section A: Find a reader API that returns the ACTUAL VALUE from
;; the setup DB var handle. We try every plausible candidate.
;; Expected: one of these should return "1f" (the current default).
;; ================================================================
printf("\n===== SECTION A: setup DB var VALUE reader discovery =====\n")

probe_v2_rA1 = nil
errset(probe_v2_rA1 = axlGetValue(probe_v2_varH))
printf("  axlGetValue(varH)              = %L\n" probe_v2_rA1)

probe_v2_rA2 = nil
errset(probe_v2_rA2 = axlGetElementValue(probe_v2_varH))
printf("  axlGetElementValue(varH)       = %L\n" probe_v2_rA2)

probe_v2_rA3 = nil
errset(probe_v2_rA3 = axlGetVarValue(probe_v2_sdb "C"))
printf("  axlGetVarValue(sdb, 'C')       = %L\n" probe_v2_rA3)

probe_v2_rA4 = nil
errset(probe_v2_rA4 = axlGetVarValueForName(probe_v2_sdb "C"))
printf("  axlGetVarValueForName(sdb,'C') = %L\n" probe_v2_rA4)

probe_v2_rA5 = nil
errset(probe_v2_rA5 = axlGetElement(probe_v2_sdb "C"))
printf("  axlGetElement(sdb, 'C')        = %L\n" probe_v2_rA5)

probe_v2_rA6 = nil
errset(probe_v2_rA6 = probe_v2_varH->value)
printf("  varH->value                    = %L\n" probe_v2_rA6)

probe_v2_rA7 = nil
errset(probe_v2_rA7 = getq(probe_v2_varH value))
printf("  getq(varH, value)              = %L\n" probe_v2_rA7)

probe_v2_rA8 = nil
errset(probe_v2_rA8 = get(probe_v2_varH 'value))
printf("  get(varH, 'value)              = %L\n" probe_v2_rA8)

;; Try to discover what fields/attributes the handle has
probe_v2_rA9 = nil
errset(probe_v2_rA9 = axlGetObjFields(probe_v2_varH))
printf("  axlGetObjFields(varH)          = %L\n" probe_v2_rA9)

probe_v2_rA10 = nil
errset(probe_v2_rA10 = axlGetAllProperties(probe_v2_varH))
printf("  axlGetAllProperties(varH)      = %L\n" probe_v2_rA10)

probe_v2_rA11 = nil
errset(probe_v2_rA11 = axlGetPropertyList(probe_v2_varH))
printf("  axlGetPropertyList(varH)       = %L\n" probe_v2_rA11)

;; describe() prints to CIW directly
printf("  --- describe(varH) output below ---\n")
errset(describe(probe_v2_varH))
printf("  --- end describe ---\n")

;; Try listAttribs if it exists
probe_v2_rA12 = nil
errset(probe_v2_rA12 = listAttribs(probe_v2_varH))
printf("  listAttribs(varH)              = %L\n" probe_v2_rA12)

;; axlGetName — might reveal something about the handle
probe_v2_rA13 = nil
errset(probe_v2_rA13 = axlGetName(probe_v2_varH))
printf("  axlGetName(varH)               = %L\n" probe_v2_rA13)

;; axlGetType — might tell us what kind of object this is
probe_v2_rA14 = nil
errset(probe_v2_rA14 = axlGetType(probe_v2_varH))
printf("  axlGetType(varH)               = %L\n" probe_v2_rA14)

;; Try reading value directly from maeGetVar with no session (global)
probe_v2_rA15 = nil
errset(probe_v2_rA15 = maeGetVar("C"))
printf("  maeGetVar('C') [no session]    = %L\n" probe_v2_rA15)

;; Try axlGetVarValue with just var handle
probe_v2_rA16 = nil
errset(probe_v2_rA16 = axlGetVarValue(probe_v2_varH))
printf("  axlGetVarValue(varH)           = %L\n" probe_v2_rA16)

;; Summary of callability for reader candidates
printf("\n  --- isCallable summary for readers ---\n")
printf("  axlGetValue:          %L\n" isCallable('axlGetValue))
printf("  axlGetElementValue:   %L\n" isCallable('axlGetElementValue))
printf("  axlGetVarValue:       %L\n" isCallable('axlGetVarValue))
printf("  axlGetVarValueForName:%L\n" isCallable('axlGetVarValueForName))
printf("  axlGetElement:        %L\n" isCallable('axlGetElement))
printf("  axlGetObjFields:      %L\n" isCallable('axlGetObjFields))
printf("  axlGetAllProperties:  %L\n" isCallable('axlGetAllProperties))
printf("  axlGetPropertyList:   %L\n" isCallable('axlGetPropertyList))
printf("  axlGetName:           %L\n" isCallable('axlGetName))
printf("  axlGetType:           %L\n" isCallable('axlGetType))


;; ================================================================
;; Section B: Test three axlSetVarValue signatures with before/after
;; read comparison. Use "77.77f" as a canary value.
;;
;; We use maeGetVar as reader (known working for runtime dict) AND
;; whichever Section A reader works for setup DB.
;; Even if no reader works, the GUI is the ultimate judge.
;; ================================================================
printf("\n===== SECTION B: axlSetVarValue signature verification =====\n")

;; Save the original maeGetVar value to restore later
probe_v2_origMae = nil
errset(probe_v2_origMae = maeGetVar("C" ?session probe_v2_session))
printf("[ORIG] maeGetVar('C') = %L\n" probe_v2_origMae)

;; ---- Signature A: axlSetVarValue(sdb, name, value) ----
printf("\n--- Sig A: axlSetVarValue(sdb, name, value) ---\n")
probe_v2_sigA_rv = nil
errset(probe_v2_sigA_rv = axlSetVarValue(probe_v2_sdb "C" "77.77f"))
printf("  return value = %L\n" probe_v2_sigA_rv)
;; Read back from every source
probe_v2_sigA_mae = nil
errset(probe_v2_sigA_mae = maeGetVar("C" ?session probe_v2_session))
printf("  maeGetVar after    = %L\n" probe_v2_sigA_mae)
probe_v2_sigA_axlH = nil
errset(probe_v2_sigA_axlH = axlGetVar(probe_v2_sdb "C"))
probe_v2_sigA_axlV = nil
errset(probe_v2_sigA_axlV = axlGetVarValue(probe_v2_sdb "C"))
printf("  axlGetVarValue after = %L\n" probe_v2_sigA_axlV)
probe_v2_sigA_axlV2 = nil
errset(probe_v2_sigA_axlV2 = axlGetVarValue(probe_v2_sigA_axlH))
printf("  axlGetVarValue(handle) after = %L\n" probe_v2_sigA_axlV2)

;; Restore via same signature
errset(axlSetVarValue(probe_v2_sdb "C" probe_v2_origMae))
;; Also restore runtime dict
errset(maeSetVar("C" probe_v2_origMae ?session probe_v2_session))

;; ---- Signature B: axlSetVarValue(varHandle, value) ----
printf("\n--- Sig B: axlSetVarValue(varHandle, value) ---\n")
probe_v2_varH_B = nil
errset(probe_v2_varH_B = axlGetVar(probe_v2_sdb "C"))
probe_v2_sigB_rv = nil
errset(probe_v2_sigB_rv = axlSetVarValue(probe_v2_varH_B "77.77f"))
printf("  return value = %L\n" probe_v2_sigB_rv)
probe_v2_sigB_mae = nil
errset(probe_v2_sigB_mae = maeGetVar("C" ?session probe_v2_session))
printf("  maeGetVar after    = %L\n" probe_v2_sigB_mae)
probe_v2_sigB_axlV = nil
errset(probe_v2_sigB_axlV = axlGetVarValue(probe_v2_sdb "C"))
printf("  axlGetVarValue after = %L\n" probe_v2_sigB_axlV)
probe_v2_sigB_axlV2 = nil
errset(probe_v2_sigB_axlV2 = axlGetVarValue(probe_v2_varH_B))
printf("  axlGetVarValue(handle) after = %L\n" probe_v2_sigB_axlV2)

;; Restore
errset(axlSetVarValue(probe_v2_varH_B probe_v2_origMae))
errset(maeSetVar("C" probe_v2_origMae ?session probe_v2_session))

;; ---- Signature C: axlSetVarValue(sdb, varHandle, value) ----
printf("\n--- Sig C: axlSetVarValue(sdb, varHandle, value) ---\n")
probe_v2_varH_C = nil
errset(probe_v2_varH_C = axlGetVar(probe_v2_sdb "C"))
probe_v2_sigC_rv = nil
errset(probe_v2_sigC_rv = axlSetVarValue(probe_v2_sdb probe_v2_varH_C "77.77f"))
printf("  return value = %L\n" probe_v2_sigC_rv)
probe_v2_sigC_mae = nil
errset(probe_v2_sigC_mae = maeGetVar("C" ?session probe_v2_session))
printf("  maeGetVar after    = %L\n" probe_v2_sigC_mae)
probe_v2_sigC_axlV = nil
errset(probe_v2_sigC_axlV = axlGetVarValue(probe_v2_sdb "C"))
printf("  axlGetVarValue after = %L\n" probe_v2_sigC_axlV)
probe_v2_sigC_axlV2 = nil
errset(probe_v2_sigC_axlV2 = axlGetVarValue(probe_v2_varH_C))
printf("  axlGetVarValue(handle) after = %L\n" probe_v2_sigC_axlV2)

;; Restore
errset(axlSetVarValue(probe_v2_sdb probe_v2_varH_C probe_v2_origMae))
errset(maeSetVar("C" probe_v2_origMae ?session probe_v2_session))

printf("\n  >>> CHECK GUI NOW: does Design Variables panel show C=77.77f?\n")
printf("  >>> If yes for any sig above, that's the winner.\n")
printf("  >>> If no, we need Section C refresh calls.\n")


;; ================================================================
;; Section C: GUI refresh candidates
;; Try these after writing "88.88f" to see if any triggers a GUI update.
;; ================================================================
printf("\n===== SECTION C: GUI refresh candidates =====\n")

;; Write a distinct value so we can tell if refresh makes it appear
errset(axlSetVarValue(probe_v2_sdb "C" "88.88f"))
errset(axlSetVarValue(probe_v2_varH "88.88f"))
errset(axlSetVarValue(probe_v2_sdb probe_v2_varH "88.88f"))
printf("  Wrote C=88.88f via all three sigs. Now trying refresh calls:\n")

printf("\n  --- refresh isCallable ---\n")
printf("  axlRefresh:           %L\n" isCallable('axlRefresh))
printf("  axlRedrawSetup:       %L\n" isCallable('axlRedrawSetup))
printf("  maeRefreshSetup:      %L\n" isCallable('maeRefreshSetup))
printf("  maeUpdate:            %L\n" isCallable('maeUpdate))
printf("  hiRedraw:             %L\n" isCallable('hiRedraw))
printf("  maeSetActiveSession:  %L\n" isCallable('maeSetActiveSession))
printf("  maeRefresh:           %L\n" isCallable('maeRefresh))
printf("  axlUpdateView:        %L\n" isCallable('axlUpdateView))
printf("  axlUpdate:            %L\n" isCallable('axlUpdate))
printf("  axlCommit:            %L\n" isCallable('axlCommit))

printf("\n  --- calling refresh candidates ---\n")
probe_v2_rf1 = nil
errset(probe_v2_rf1 = axlRefresh(probe_v2_sdb))
printf("  axlRefresh(sdb)                = %L\n" probe_v2_rf1)

probe_v2_rf2 = nil
errset(probe_v2_rf2 = axlRedrawSetup(probe_v2_session))
printf("  axlRedrawSetup(session)        = %L\n" probe_v2_rf2)

probe_v2_rf3 = nil
errset(probe_v2_rf3 = maeRefreshSetup(probe_v2_session))
printf("  maeRefreshSetup(session)       = %L\n" probe_v2_rf3)

probe_v2_rf4 = nil
errset(probe_v2_rf4 = maeUpdate(probe_v2_session))
printf("  maeUpdate(session)             = %L\n" probe_v2_rf4)

probe_v2_rf5 = nil
errset(probe_v2_rf5 = hiRedraw())
printf("  hiRedraw()                     = %L\n" probe_v2_rf5)

probe_v2_rf6 = nil
errset(probe_v2_rf6 = maeSetActiveSession(probe_v2_session))
printf("  maeSetActiveSession(session)   = %L\n" probe_v2_rf6)

probe_v2_rf7 = nil
errset(probe_v2_rf7 = maeRefresh(probe_v2_session))
printf("  maeRefresh(session)            = %L\n" probe_v2_rf7)

probe_v2_rf8 = nil
errset(probe_v2_rf8 = axlUpdateView(probe_v2_sdb))
printf("  axlUpdateView(sdb)             = %L\n" probe_v2_rf8)

probe_v2_rf9 = nil
errset(probe_v2_rf9 = axlUpdate(probe_v2_sdb))
printf("  axlUpdate(sdb)                 = %L\n" probe_v2_rf9)

probe_v2_rf10 = nil
errset(probe_v2_rf10 = axlCommit(probe_v2_sdb))
printf("  axlCommit(sdb)                 = %L\n" probe_v2_rf10)

printf("\n  >>> CHECK GUI NOW: does Design Variables panel show C=88.88f?\n")
printf("  >>> If yes, the refresh call that made it appear is the one we need.\n")


;; ================================================================
;; Section D: Restore original values
;; ================================================================
printf("\n===== SECTION D: Restoring original values =====\n")

;; Restore runtime dict
errset(maeSetVar("C" probe_v2_origMae ?session probe_v2_session))
printf("  Restored maeSetVar('C') = %L\n" probe_v2_origMae)

;; Try to restore setup DB via all three sigs
errset(axlSetVarValue(probe_v2_sdb "C" probe_v2_origMae))
errset(axlSetVarValue(probe_v2_varH probe_v2_origMae))
errset(axlSetVarValue(probe_v2_sdb probe_v2_varH probe_v2_origMae))

;; Save to persist the restore
errset(maeSaveSetup(?lib "pll" ?cell "LC_VCO_tb" ?view "maestro"
                    ?session probe_v2_session))
printf("  maeSaveSetup called to persist restore.\n")

;; Verify final state
probe_v2_final = nil
errset(probe_v2_final = maeGetVar("C" ?session probe_v2_session))
printf("  Final maeGetVar('C') = %L\n" probe_v2_final)

printf("\n===== probe_axl_setvar_verify END =====\n")
t
