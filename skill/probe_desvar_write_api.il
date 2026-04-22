;;; probe_desvar_write_api.il — One-shot probe to discover the correct
;;; Maestro design-variable WRITE API on IC23.1 remote host.
;;;
;;; Run in CIW with Maestro open for pll/LC_VCO_tb:
;;;   load("/path/to/probe_desvar_write_api.il")
;;;
;;; Outputs a structured report to CIW showing which APIs exist,
;;; what the setup DB structure looks like, and whether we can
;;; read/write design variables through the axl* layer.

printf("\n===== probe_desvar_write_api BEGIN =====\n")

;; ---- Step 1: Resolve session ----
printf("\n--- Step 1: resolve session ---\n")
probe_dv_session = safeMaestro_findSessionForScope("pll" "LC_VCO_tb")
printf("  session = %L\n" probe_dv_session)

;; ---- Step 2: axlGetMainSetupDB ----
printf("\n--- Step 2: axlGetMainSetupDB ---\n")
printf("  isCallable(axlGetMainSetupDB) = %L\n" isCallable('axlGetMainSetupDB))
probe_dv_sdb = nil
when(stringp(probe_dv_session)
    errset(probe_dv_sdb = axlGetMainSetupDB(probe_dv_session))
)
printf("  sdb = %L\n" probe_dv_sdb)
printf("  type(sdb) = %L\n" type(probe_dv_sdb))

;; ---- Step 3: axlGetVar — read existing var from setup DB ----
printf("\n--- Step 3: axlGetVar (read 'C' from setup DB) ---\n")
printf("  isCallable(axlGetVar) = %L\n" isCallable('axlGetVar))
probe_dv_varC = nil
when(probe_dv_sdb
    errset(probe_dv_varC = axlGetVar(probe_dv_sdb "C"))
)
printf("  axlGetVar(sdb, 'C') = %L\n" probe_dv_varC)
printf("  type = %L\n" type(probe_dv_varC))

;; If we got a var element, inspect its properties
when(probe_dv_varC
    printf("  Inspecting var element properties:\n")
    errset(printf("    ->value    = %L\n" probe_dv_varC->value))
    errset(printf("    ->name     = %L\n" probe_dv_varC->name))
    errset(printf("    ->type     = %L\n" probe_dv_varC->type))
    errset(printf("    ->cellName = %L\n" probe_dv_varC->cellName))
)

;; ---- Step 4: axlPutVar — does it exist? ----
printf("\n--- Step 4: axlPutVar (create/get var) ---\n")
printf("  isCallable(axlPutVar) = %L\n" isCallable('axlPutVar))
printf("  getd(axlPutVar) = %L\n" if(getd('axlPutVar) "non-nil" "nil"))
;; Try calling it (non-destructive: use existing var name so no new state)
probe_dv_putC = nil
when(probe_dv_sdb
    errset(probe_dv_putC = axlPutVar(probe_dv_sdb "C"))
)
printf("  axlPutVar(sdb, 'C') result = %L\n" probe_dv_putC)

;; ---- Step 5: try setting value via ->value property ----
printf("\n--- Step 5: set value via ->value property ---\n")
probe_dv_old_val = nil
probe_dv_write_ok = nil
when(probe_dv_varC
    errset(probe_dv_old_val = probe_dv_varC->value)
    printf("  old value = %L\n" probe_dv_old_val)
    ;; Try writing a test value (will be reverted)
    errset(
        probe_dv_varC->value = "99.99f"
        probe_dv_write_ok = t
    )
    printf("  write via ->value = %L\n" probe_dv_write_ok)
    ;; Read back
    errset(printf("  readback after write = %L\n" probe_dv_varC->value))
    ;; Revert
    when(probe_dv_write_ok
        errset(probe_dv_varC->value = probe_dv_old_val)
        printf("  reverted to %L\n" probe_dv_old_val)
    )
)

;; ---- Step 6: alternative write APIs ----
printf("\n--- Step 6: alternative write API availability ---\n")
printf("  axlSetVarValue:       isCallable=%L  getd=%L\n"
       isCallable('axlSetVarValue) if(getd('axlSetVarValue) "non-nil" "nil"))
printf("  axlSetValue:          isCallable=%L  getd=%L\n"
       isCallable('axlSetValue) if(getd('axlSetValue) "non-nil" "nil"))
printf("  axlAddVar:            isCallable=%L  getd=%L\n"
       isCallable('axlAddVar) if(getd('axlAddVar) "non-nil" "nil"))
printf("  axlCreateVar:         isCallable=%L  getd=%L\n"
       isCallable('axlCreateVar) if(getd('axlCreateVar) "non-nil" "nil"))
printf("  maeAddDesignVar:      isCallable=%L  getd=%L\n"
       isCallable('maeAddDesignVar) if(getd('maeAddDesignVar) "non-nil" "nil"))
printf("  maeUpdateDesignVar:   isCallable=%L  getd=%L\n"
       isCallable('maeUpdateDesignVar) if(getd('maeUpdateDesignVar) "non-nil" "nil"))
printf("  maeDeleteDesignVar:   isCallable=%L  getd=%L\n"
       isCallable('maeDeleteDesignVar) if(getd('maeDeleteDesignVar) "non-nil" "nil"))
printf("  maeGetDesignVarList:  isCallable=%L  getd=%L\n"
       isCallable('maeGetDesignVarList) if(getd('maeGetDesignVarList) "non-nil" "nil"))
printf("  asiSetDesignVarValue: isCallable=%L  getd=%L\n"
       isCallable('asiSetDesignVarValue) if(getd('asiSetDesignVarValue) "non-nil" "nil"))

;; ---- Step 7: enumerate all vars in setup DB ----
printf("\n--- Step 7: enumerate vars via axlGetVars ---\n")
printf("  isCallable(axlGetVars) = %L\n" isCallable('axlGetVars))
probe_dv_vars = nil
when(probe_dv_sdb
    errset(probe_dv_vars = axlGetVars(probe_dv_sdb))
)
printf("  axlGetVars(sdb) = %L\n" probe_dv_vars)
when(listp(probe_dv_vars)
    printf("  count = %d\n" length(probe_dv_vars))
    foreach(v probe_dv_vars
        errset(printf("    name=%L  value=%L  type=%L\n"
                       v->name v->value type(v)))
    )
)

;; ---- Step 8: compare maeGetVar vs axlGetVar readback ----
printf("\n--- Step 8: maeGetVar vs axlGetVar comparison ---\n")
foreach(vn '("C" "Ibias" "nfin_cc" "R")
    let((maeVal axlObj axlVal)
        maeVal = nil
        axlVal = nil
        errset(maeVal = maeGetVar(vn ?session probe_dv_session))
        when(probe_dv_sdb
            errset(axlObj = axlGetVar(probe_dv_sdb vn))
            when(axlObj errset(axlVal = axlObj->value))
        )
        printf("  %s: maeGetVar=%L  axlGetVar->value=%L  same=%L\n"
               vn maeVal axlVal equal(maeVal axlVal))
    )
)

printf("\n===== probe_desvar_write_api END =====\n")
t
