;;; probe_sev_designvar_api.il — Fourth-round probe targeting sev* namespace.
;;;
;;; Round 3 confirmed: sev* is the Maestro namespace, OA cellview doesn't
;;; store design vars, and these 5 APIs are callable:
;;;   sevSetVariables, sevAddVar, sevVariables,
;;;   asiWriteDesignVar, asiSaveDesignVars
;;;
;;; This probe: discover signatures, test read/write, verify GUI update.
;;;
;;; RUN: In CIW with Maestro open for pll/LC_VCO_tb:
;;;   load("/path/to/probe_sev_designvar_api.il")

printf("\n===== probe_sev_designvar_api BEGIN =====\n")

;; ---- Common setup ----
probe_sv_session = safeMaestro_findSessionForScope("pll" "LC_VCO_tb")
printf("[SETUP] session = %L\n" probe_sv_session)


;; ================================================================
;; Section A: getd / argspec / describe for each candidate
;; ================================================================
printf("\n========== SECTION A: function signatures ==========\n")

foreach(fn '(sevSetVariables sevAddVar sevVariables sevDeleteVar
             asiWriteDesignVar asiSaveDesignVars
             sevGetVariables sevListVariables sevGetVar sevSetVar)
    let((fnObj)
        printf("\n[%s]\n" symbolToString(fn))
        printf("  isCallable = %L\n" isCallable(fn))
        fnObj = nil
        errset(fnObj = getd(fn))
        printf("  getd = %L\n" if(fnObj "non-nil" "nil"))
        when(fnObj
            errset(printf("  argList = %L\n" fnObj->argList))
            errset(printf("  type = %L\n" type(fnObj)))
        )
        printf("  --- describe ---\n")
        errset(describe(fn))
        printf("  --- end describe ---\n")
    )
)


;; ================================================================
;; Section B: Reader discovery — is sevVariables the reader?
;; ================================================================
printf("\n========== SECTION B: reader discovery ==========\n")

;; Try sevVariables with different arg patterns
probe_sv_r1 = nil
errset(probe_sv_r1 = sevVariables(probe_sv_session))
printf("  sevVariables(session)           = %L\n" probe_sv_r1)

probe_sv_r2 = nil
errset(probe_sv_r2 = sevVariables(probe_sv_session "C"))
printf("  sevVariables(session, 'C')      = %L\n" probe_sv_r2)

;; No-arg
probe_sv_r3 = nil
errset(probe_sv_r3 = sevVariables())
printf("  sevVariables()                  = %L\n" probe_sv_r3)

;; Try sevGetVariables too
probe_sv_r4 = nil
errset(probe_sv_r4 = sevGetVariables(probe_sv_session))
printf("  sevGetVariables(session)        = %L\n" probe_sv_r4)

;; Try sevListVariables
probe_sv_r5 = nil
errset(probe_sv_r5 = sevListVariables(probe_sv_session))
printf("  sevListVariables(session)       = %L\n" probe_sv_r5)

;; Try sevGetVar
probe_sv_r6 = nil
errset(probe_sv_r6 = sevGetVar(probe_sv_session "C"))
printf("  sevGetVar(session, 'C')         = %L\n" probe_sv_r6)

probe_sv_r7 = nil
errset(probe_sv_r7 = sevGetVar("C" ?session probe_sv_session))
printf("  sevGetVar('C' ?session sess)    = %L\n" probe_sv_r7)

;; If sevVariables returned a list, dump it nicely
when(listp(probe_sv_r1)
    printf("\n  --- sevVariables full dump ---\n")
    printf("  length = %d\n" length(probe_sv_r1))
    foreach(entry probe_sv_r1
        printf("    %L\n" entry)
    )
)


;; ================================================================
;; Section C: Setter trial — write "999.999f" to C, read back, check GUI
;; ================================================================
printf("\n========== SECTION C: setter trial ==========\n")

;; Save original for restore
probe_sv_origC = nil
errset(probe_sv_origC = maeGetVar("C" ?session probe_sv_session))
printf("[ORIG] maeGetVar('C') = %L\n" probe_sv_origC)

;; ---- Trial 1: sevSetVariables(session, varList) ----
printf("\n--- Trial 1: sevSetVariables(session, list(list(name val))) ---\n")
probe_sv_t1 = nil
errset(probe_sv_t1 = sevSetVariables(probe_sv_session list(list("C" "999.999f"))))
printf("  return = %L\n" probe_sv_t1)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))
printf("  >>> CHECK GUI NOW: is C=999.999f? <<<\n")

;; Restore
errset(sevSetVariables(probe_sv_session list(list("C" probe_sv_origC))))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))

;; ---- Trial 2: sevSetVariables(session, name, value) ----
printf("\n--- Trial 2: sevSetVariables(session, name, value) ---\n")
probe_sv_t2 = nil
errset(probe_sv_t2 = sevSetVariables(probe_sv_session "C" "999.999f"))
printf("  return = %L\n" probe_sv_t2)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))
printf("  >>> CHECK GUI NOW: is C=999.999f? <<<\n")

;; Restore
errset(sevSetVariables(probe_sv_session "C" probe_sv_origC))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))

;; ---- Trial 3: sevAddVar(session, name, value) ----
printf("\n--- Trial 3: sevAddVar(session, name, value) ---\n")
probe_sv_t3 = nil
errset(probe_sv_t3 = sevAddVar(probe_sv_session "C" "999.999f"))
printf("  return = %L\n" probe_sv_t3)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))
printf("  >>> CHECK GUI NOW: is C=999.999f? <<<\n")

;; Restore
errset(sevAddVar(probe_sv_session "C" probe_sv_origC))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))

;; ---- Trial 4: sevAddVar(session, list(name value)) ----
printf("\n--- Trial 4: sevAddVar(session, list(name, value)) ---\n")
probe_sv_t4 = nil
errset(probe_sv_t4 = sevAddVar(probe_sv_session list("C" "999.999f")))
printf("  return = %L\n" probe_sv_t4)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))
printf("  >>> CHECK GUI NOW: is C=999.999f? <<<\n")

;; Restore
errset(sevAddVar(probe_sv_session list("C" probe_sv_origC)))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))

;; ---- Trial 5: asiWriteDesignVar(session, name, value) ----
printf("\n--- Trial 5: asiWriteDesignVar(session, name, value) ---\n")
probe_sv_t5 = nil
errset(probe_sv_t5 = asiWriteDesignVar(probe_sv_session "C" "999.999f"))
printf("  return = %L\n" probe_sv_t5)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))
printf("  >>> CHECK GUI NOW: is C=999.999f? <<<\n")

;; Restore
errset(asiWriteDesignVar(probe_sv_session "C" probe_sv_origC))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))

;; ---- Trial 6: sevSetVar(session, name, value) ----
printf("\n--- Trial 6: sevSetVar(session, name, value) ---\n")
probe_sv_t6 = nil
errset(probe_sv_t6 = sevSetVar(probe_sv_session "C" "999.999f"))
printf("  return = %L\n" probe_sv_t6)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))

;; Restore
errset(sevSetVar(probe_sv_session "C" probe_sv_origC))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))

;; ---- Trial 7: sevSetVar with keyword args ----
printf("\n--- Trial 7: sevSetVar(name value ?session sess) ---\n")
probe_sv_t7 = nil
errset(probe_sv_t7 = sevSetVar("C" "999.999f" ?session probe_sv_session))
printf("  return = %L\n" probe_sv_t7)
errset(printf("  sevVariables after = %L\n" sevVariables(probe_sv_session)))
errset(printf("  maeGetVar after    = %L\n" maeGetVar("C" ?session probe_sv_session)))

;; Restore
errset(sevSetVar("C" probe_sv_origC ?session probe_sv_session))
errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))


;; ================================================================
;; Section D: Menu callback reverse-engineering
;; ================================================================
printf("\n========== SECTION D: menu callback discovery ==========\n")

;; Try to access the menu object directly
probe_sv_menu = nil
errset(probe_sv_menu = sevVariablesMenu31)
printf("  sevVariablesMenu31 = %L  type=%L\n" probe_sv_menu type(probe_sv_menu))

;; Inspect menu object
when(probe_sv_menu
    printf("\n  --- menu object inspection ---\n")
    errset(printf("  ->? = %L\n" probe_sv_menu->?))
    errset(printf("  ->items = %L\n" probe_sv_menu->items))
    errset(printf("  ->subItems = %L\n" probe_sv_menu->subItems))
    errset(printf("  ->itemList = %L\n" probe_sv_menu->itemList))
    errset(printf("  ->callback = %L\n" probe_sv_menu->callback))
    errset(printf("  ->name = %L\n" probe_sv_menu->name))
    printf("  --- describe ---\n")
    errset(describe(probe_sv_menu))
    printf("  --- end describe ---\n")
)

;; Try hiGetMenuItems
printf("\n  --- hiGetMenuItems ---\n")
printf("  isCallable(hiGetMenuItems) = %L\n" isCallable('hiGetMenuItems))
probe_sv_mitems = nil
errset(probe_sv_mitems = hiGetMenuItems(probe_sv_menu))
printf("  hiGetMenuItems = %L\n" probe_sv_mitems)

when(listp(probe_sv_mitems)
    foreach(it probe_sv_mitems
        printf("\n    item = %L\n" it)
        errset(printf("      ->name = %L\n" it->name))
        errset(printf("      ->callback = %L\n" it->callback))
        errset(printf("      ->? = %L\n" it->?))
    )
)

;; Try hiGetMenuItem patterns
printf("\n  isCallable(hiGetMenuItemCallback) = %L\n" isCallable('hiGetMenuItemCallback))


;; ================================================================
;; Section E: Persistence test (if any trial in C succeeded)
;; Only runs if we detect a successful write from Section C.
;; The user should manually verify GUI after each Section C trial,
;; then re-run with the winning trial hardcoded here if needed.
;; For now, try asiSaveDesignVars as the persistence call.
;; ================================================================
printf("\n========== SECTION E: persistence API test ==========\n")

printf("  isCallable(asiSaveDesignVars) = %L\n" isCallable('asiSaveDesignVars))

;; Try asiSaveDesignVars with different arg patterns
probe_sv_save1 = nil
errset(probe_sv_save1 = asiSaveDesignVars(probe_sv_session))
printf("  asiSaveDesignVars(session)      = %L\n" probe_sv_save1)

probe_sv_save2 = nil
errset(probe_sv_save2 = asiSaveDesignVars())
printf("  asiSaveDesignVars()             = %L\n" probe_sv_save2)

;; Also check if maeSaveSetup still works
printf("  (maeSaveSetup is the existing save — verified working)\n")

;; Try sevSave variants
printf("\n  --- sevSave* candidates ---\n")
foreach(fn '(sevSave sevSaveSetup sevSaveState sevSaveSession
             sevSaveVariables sevSaveDesignVars sevSaveAll)
    printf("  %s: callable=%L\n" symbolToString(fn) isCallable(fn))
)


;; ================================================================
;; Section F: Final restore — make sure C is back to original
;; ================================================================
printf("\n========== SECTION F: restore original values ==========\n")

errset(maeSetVar("C" probe_sv_origC ?session probe_sv_session))
printf("  Restored maeSetVar('C') = %L\n" probe_sv_origC)

;; Also try restoring via sev* if any trial worked
errset(sevSetVariables(probe_sv_session list(list("C" probe_sv_origC))))
errset(sevAddVar(probe_sv_session "C" probe_sv_origC))

;; Save
errset(maeSaveSetup(?lib "pll" ?cell "LC_VCO_tb" ?view "maestro"
                    ?session probe_sv_session))
printf("  maeSaveSetup called.\n")

;; Final readback
probe_sv_finalMae = nil
errset(probe_sv_finalMae = maeGetVar("C" ?session probe_sv_session))
printf("  Final maeGetVar('C') = %L\n" probe_sv_finalMae)

printf("\n===== probe_sev_designvar_api END =====\n")
t
