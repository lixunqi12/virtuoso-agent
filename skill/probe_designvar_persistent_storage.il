;;; probe_designvar_persistent_storage.il — Third-round probe to find the
;;; persistent storage layer for Maestro Design Variables.
;;;
;;; BACKGROUND: axlSetVarValue writes to in-memory setup DB (confirmed by
;;; probe round 2), but maeSaveSetup does NOT persist those values to disk.
;;; After reopening Maestro, Design Variables revert to initial values.
;;; We need to find WHERE the values are actually stored (OA cellview
;;; property? separate file? different API?) and HOW to write them.
;;;
;;; RUN: In CIW with Maestro open for pll/LC_VCO_tb:
;;;   load("/path/to/probe_designvar_persistent_storage.il")
;;;
;;; ALL SECTIONS ARE READ-ONLY. No state is modified.

printf("\n===== probe_designvar_persistent_storage BEGIN =====\n")

;; Common setup
probe_ps_session = safeMaestro_findSessionForScope("pll" "LC_VCO_tb")
printf("[SETUP] session = %L\n" probe_ps_session)
probe_ps_sdb = nil
errset(probe_ps_sdb = axlGetMainSetupDB(probe_ps_session))
printf("[SETUP] sdb = %L\n" probe_ps_sdb)


;; ================================================================
;; Section A: Enumerate all DesignVar-related callable symbols
;; ================================================================
printf("\n========== SECTION A: DesignVar symbol enumeration ==========\n")

;; Try oblist() first (may not exist on IC23.1)
printf("  isCallable(oblist) = %L\n" isCallable('oblist))
probe_ps_oblistOk = nil
errset(
    foreach(sym oblist()
        let((sname)
            sname = symbolToString(sym)
            when(or(pcreMatchp("[Dd]esign[Vv]ar" sname)
                    pcreMatchp("[Vv]arDef" sname)
                    pcreMatchp("[Vv]ariable.*[Ss]et" sname))
                printf("  [OBLIST] %s  callable=%L  getd=%L\n"
                       sname isCallable(sym) if(getd(sym) "non-nil" "nil"))
            )
        )
    )
    probe_ps_oblistOk = t
)
printf("  oblist scan completed = %L\n" probe_ps_oblistOk)

;; Hardcoded cross-product: prefix x suffix
printf("\n  --- hardcoded candidate scan ---\n")
probe_ps_prefixes = '(
    "asi" "ade" "mae" "amsd" "axl" "sev" "sim" "ocn" "sch" "db"
    "Asi" "Ade" "Mae" "Amsd" "Axl" "Sev" "Sim" "Ocn" "Sch" "Db"
)
probe_ps_suffixes = '(
    "DesignVar" "DesignVars" "DesignVariable" "DesignVariables"
    "Variable" "Variables" "Vars" "VarDef" "VarDefs"
    "SetDesignVar" "SetDesignVars" "SetDesignVariable"
    "GetDesignVar" "GetDesignVars" "GetDesignVariable"
    "AddDesignVar" "AddDesignVars" "AddVariable"
    "UpdateDesignVar" "UpdateVariable" "SaveDesignVar"
    "WriteDesignVar" "WriteDesignVars"
    "PutDesignVar" "PutVar" "PutVariable"
)
probe_ps_verbs = '(
    "Set" "Get" "Add" "Update" "Save" "Write" "Put" "Delete" "Remove" "List"
)

foreach(pfx probe_ps_prefixes
    foreach(sfx probe_ps_suffixes
        let((fname sym)
            fname = strcat(pfx sfx)
            sym = stringToSymbol(fname)
            when(isCallable(sym)
                printf("  [FOUND] %s  callable=t  getd=%L\n"
                       fname if(getd(sym) "non-nil" "nil"))
            )
        )
    )
    ;; Also try prefix + verb + "DesignVar" etc.
    foreach(verb probe_ps_verbs
        foreach(noun '("DesignVar" "DesignVars" "DesignVariable" "DesignVariables" "Variable" "Variables")
            let((fname sym)
                fname = strcat(pfx verb noun)
                sym = stringToSymbol(fname)
                when(isCallable(sym)
                    printf("  [FOUND] %s  callable=t  getd=%L\n"
                           fname if(getd(sym) "non-nil" "nil"))
                )
            )
        )
    )
)

;; Extra specific candidates from Cadence docs / forum references
printf("\n  --- extra specific candidates ---\n")
foreach(fn '(
    maeGetDesignVars maeSetDesignVars maeAddDesignVar maeDeleteDesignVar
    maeGetGlobalVars maeSetGlobalVars maeAddGlobalVar
    maeGetVarValue maeSetVarValue
    maeGetVariables maeSetVariables maeAddVariable
    axlGetDesignVars axlSetDesignVars axlAddDesignVar
    axlGetGlobalVars axlSetGlobalVars
    axlGetVariableList axlSetVariableList
    axlPutDesignVar axlPutVariable
    asiSetDesignVar asiGetDesignVar asiAddDesignVar
    asiSetDesignVarValue asiGetDesignVarValue
    asiDesignVarList asiListDesignVars
    sevSetDesignVar sevGetDesignVar sevAddDesignVar
    sevSetVar sevGetVar sevAddVar sevListVars
    adeSetVar adeGetVar adeAddVar adeSetDesignVar
    maeWriteState maeLoadState maeGetState maeSetState
    maeMakeEditable maeRestoreHistory
    axlSaveState axlLoadState axlGetState axlSetState
    axlSaveSetup axlLoadSetup
    axlWriteSetup axlReadSetup
    )
    when(isCallable(fn)
        printf("  [FOUND] %s  callable=t  getd=%L\n"
               symbolToString(fn) if(getd(fn) "non-nil" "nil"))
    )
)


;; ================================================================
;; Section B: OA cellview properties — open maestro view and dump
;; ================================================================
printf("\n========== SECTION B: OA cellview property dump ==========\n")

probe_ps_cv = nil
errset(probe_ps_cv = dbOpenCellViewByType("pll" "LC_VCO_tb" "maestro" "" "r"))
printf("  dbOpenCellViewByType('pll' 'LC_VCO_tb' 'maestro' '' 'r') = %L\n" probe_ps_cv)
printf("  type = %L\n" type(probe_ps_cv))

when(probe_ps_cv
    ;; Dump all slot names via ->?
    printf("\n  --- cv->? (all slots) ---\n")
    errset(
        let((slots)
            slots = probe_ps_cv->?
            printf("  cv->? = %L\n" slots)
            ;; Print first 50 slots with values
            let((count)
                count = 0
                foreach(s slots
                    when(lessp(count 50)
                        errset(printf("    %s = %L\n" s get(probe_ps_cv s)))
                        count = count + 1
                    )
                )
            )
        )
    )

    ;; Dump cv->prop (user-defined properties)
    printf("\n  --- cv->prop (user properties) ---\n")
    errset(
        let((props)
            props = probe_ps_cv->prop
            printf("  cv->prop count = %L\n" if(listp(props) length(props) props))
            when(listp(props)
                foreach(p props
                    errset(printf("    name=%L value=%L type=%L\n"
                                  p->name p->value type(p->value)))
                )
            )
        )
    )

    ;; Try specific property names
    printf("\n  --- specific property lookups ---\n")
    foreach(pn '(designVars designVariables varDef varList variables
                  maestroVars globalVars simVars desVars desVar
                  setupVars setupVariables parameterList parameters)
        let((val)
            val = nil
            errset(val = dbGetq(probe_ps_cv pn))
            when(val
                printf("  dbGetq(cv, %s) = %L\n" symbolToString(pn) val)
            )
        )
    )

    ;; List all cellview files in the cell directory
    printf("\n  --- cellview files ---\n")
    errset(printf("  cv->cellName = %L\n" probe_ps_cv->cellName))
    errset(printf("  cv->libName  = %L\n" probe_ps_cv->libName))
    errset(printf("  cv->viewName = %L\n" probe_ps_cv->viewName))
    errset(printf("  cv->fileName = %L\n" probe_ps_cv->fileName))

    dbClose(probe_ps_cv)
    printf("  (cellview closed)\n")
)


;; ================================================================
;; Section C: Try other cellview types
;; ================================================================
printf("\n========== SECTION C: alternative cellview types ==========\n")

foreach(vt '("maestro" "adexl" "ade" "amsDcfg" "setup" "config" "state")
    let((cv2)
        cv2 = nil
        errset(cv2 = dbOpenCellViewByType("pll" "LC_VCO_tb" vt "" "r"))
        printf("  type=%-12s cv=%L\n" vt cv2)
        when(cv2
            ;; Quick property dump
            errset(
                let((props)
                    props = cv2->prop
                    printf("    prop count = %L\n" if(listp(props) length(props) props))
                    when(listp(props)
                        foreach(p props
                            errset(printf("      name=%L value=%L\n" p->name p->value))
                        )
                    )
                )
            )
            errset(printf("    fileName = %L\n" cv2->fileName))
            dbClose(cv2)
        )
    )
)


;; ================================================================
;; Section D: axl handle chain — top-level element types
;; ================================================================
printf("\n========== SECTION D: axl handle chain exploration ==========\n")

when(probe_ps_sdb
    ;; What top-level element types are in the setup DB?
    printf("  isCallable(axlGetTopLevelElementTypes) = %L\n"
           isCallable('axlGetTopLevelElementTypes))
    errset(
        let((types)
            types = axlGetTopLevelElementTypes(probe_ps_sdb)
            printf("  axlGetTopLevelElementTypes(sdb) = %L\n" types)
        )
    )

    ;; Try to get sub-DBs or children
    printf("  isCallable(axlGetChildren) = %L\n" isCallable('axlGetChildren))
    errset(printf("  axlGetChildren(sdb) = %L\n" axlGetChildren(probe_ps_sdb)))

    printf("  isCallable(axlGetElements) = %L\n" isCallable('axlGetElements))
    errset(printf("  axlGetElements(sdb) = %L\n" axlGetElements(probe_ps_sdb)))

    printf("  isCallable(axlGetAllElements) = %L\n" isCallable('axlGetAllElements))
    errset(printf("  axlGetAllElements(sdb) = %L\n" axlGetAllElements(probe_ps_sdb)))

    ;; Try getting the "variables" sub-container specifically
    printf("  isCallable(axlGetVarDB) = %L\n" isCallable('axlGetVarDB))
    errset(printf("  axlGetVarDB(sdb) = %L\n" axlGetVarDB(probe_ps_sdb)))

    ;; axlGetSetupDB variants
    printf("  isCallable(axlGetSetupDB) = %L\n" isCallable('axlGetSetupDB))
    errset(printf("  axlGetSetupDB(session) = %L\n"
                  axlGetSetupDB(probe_ps_session)))

    ;; axlSave / axlCommit on the sdb
    printf("  isCallable(axlSave) = %L\n" isCallable('axlSave))
    printf("  isCallable(axlCommit) = %L\n" isCallable('axlCommit))
    printf("  isCallable(axlFlush) = %L\n" isCallable('axlFlush))
    printf("  isCallable(axlSaveSetup) = %L\n" isCallable('axlSaveSetup))
    printf("  isCallable(axlSaveSession) = %L\n" isCallable('axlSaveSession))
)


;; ================================================================
;; Section E: Find "Design Variables" menu callback
;; ================================================================
printf("\n========== SECTION E: menu callback discovery ==========\n")

printf("  isCallable(hiGetAllMenus) = %L\n" isCallable('hiGetAllMenus))
errset(printf("  hiGetAllMenus() = %L\n" hiGetAllMenus()))

;; Try to find menu bar of the Maestro/ADE window
foreach(w hiGetWindowList()
    let((title sess)
        errset(title = hiGetWindowName(w))
        errset(sess = axlGetWindowSession(w))
        when(and(stringp(title)
                 safeMaestro_substrp(title "LC_VCO_tb"))
            printf("\n  Window: %L\n" title)
            printf("  isCallable(hiGetMenuBar) = %L\n" isCallable('hiGetMenuBar))
            errset(printf("    hiGetMenuBar(w) = %L\n" hiGetMenuBar(w)))
            printf("  isCallable(hiGetPulldownMenus) = %L\n" isCallable('hiGetPulldownMenus))
            errset(printf("    hiGetPulldownMenus(w) = %L\n" hiGetPulldownMenus(w)))
            printf("  isCallable(hiGetBannerMenus) = %L\n" isCallable('hiGetBannerMenus))
            errset(printf("    hiGetBannerMenus(w) = %L\n" hiGetBannerMenus(w)))
        )
    )
)


;; ================================================================
;; Section F: Session object attribute exploration
;; ================================================================
printf("\n========== SECTION F: session attribute exploration ==========\n")

;; The session is a string (e.g. "fnxSession3") — try treating it as
;; a symbol or looking up attributes on it
printf("  session type = %L  value = %L\n" type(probe_ps_session) probe_ps_session)

;; Try converting session string to a SKILL object/symbol
errset(
    let((sessSym)
        sessSym = stringToSymbol(probe_ps_session)
        printf("  stringToSymbol(session) = %L\n" sessSym)
        printf("  isCallable = %L\n" isCallable(sessSym))
        errset(printf("  getd = %L\n" getd(sessSym)))
        errset(printf("  boundp = %L\n" boundp(sessSym)))
        when(boundp(sessSym)
            errset(printf("  eval = %L\n" eval(sessSym)))
            errset(
                let((sessObj)
                    sessObj = eval(sessSym)
                    printf("  type of eval = %L\n" type(sessObj))
                    errset(printf("  sessObj->? = %L\n" sessObj->?))
                )
            )
        )
    )
)

;; Try maeGetSetup to see the setup structure
printf("\n  --- maeGetSetup output ---\n")
errset(
    let((setup)
        setup = maeGetSetup(?session probe_ps_session)
        printf("  maeGetSetup = %L\n" setup)
        printf("  type = %L  length = %L\n" type(setup)
               if(listp(setup) length(setup) "N/A"))
    )
)

;; Try reading the maestro cellview file directly via system()
printf("\n  --- maestro cellview file listing ---\n")
errset(
    let((libPath)
        libPath = ddGetObjReadPath(ddGetObj("pll"))
        printf("  pll lib path = %L\n" libPath)
        when(stringp(libPath)
            system(strcat("ls -la " libPath "/LC_VCO_tb/maestro/ 2>/dev/null || echo 'dir not found'"))
            system(strcat("cat " libPath "/LC_VCO_tb/maestro/ade_setup.json 2>/dev/null | head -50 || echo 'no ade_setup.json'"))
            system(strcat("cat " libPath "/LC_VCO_tb/maestro/data.dm 2>/dev/null | head -50 || echo 'no data.dm'"))
            system(strcat("ls -la " libPath "/LC_VCO_tb/ 2>/dev/null || echo 'cell dir not found'"))
        )
    )
)


printf("\n===== probe_designvar_persistent_storage END =====\n")
t
