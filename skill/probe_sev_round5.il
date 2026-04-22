;;; probe_sev_round5.il — Fifth-round probe: final targeted discovery for
;;; Maestro Design Variable persistent write API.
;;;
;;; Round 4 confirmed: sevSetVariables has argList
;;;   (session variables @key (updateEnvironment 't) eventDriven)
;;; but all sev* calls with string session return nil.
;;;
;;; This probe tests 5 hypotheses:
;;;   A: maeMakeEditable prerequisite
;;;   B: Multiple variable format trials for sevSetVariables
;;;   C: Session type discovery (window handle vs string)
;;;   D: Menu item deep inspection for Edit callback
;;;   E: Schematic cellview property dump (CopyFromCellview hint)
;;;   F: Restore
;;;
;;; RUN: In CIW with Maestro open for pll/LC_VCO_tb:
;;;   load("/path/to/probe_sev_round5.il")

printf("\n===== probe_sev_round5 BEGIN =====\n")

;; ---- Common setup ----
probe_r5_session = safeMaestro_findSessionForScope("pll" "LC_VCO_tb")
printf("[SETUP] session = %L  type = %L\n" probe_r5_session type(probe_r5_session))

probe_r5_sdb = nil
errset(probe_r5_sdb = axlGetMainSetupDB(probe_r5_session))
printf("[SETUP] sdb = %L\n" probe_r5_sdb)

;; Save original C value for restore
probe_r5_origC = nil
errset(probe_r5_origC = maeGetVar("C" ?session probe_r5_session))
printf("[SETUP] original C = %L\n" probe_r5_origC)

;; Find the Maestro window handle for Section C
probe_r5_win = nil
foreach(w hiGetWindowList()
    unless(probe_r5_win
        let((sess title)
            sess = nil
            title = nil
            errset(sess = axlGetWindowSession(w))
            errset(title = hiGetWindowName(w))
            when(and(stringp(sess) stringp(title)
                     safeMaestro_substrp(title "LC_VCO_tb"))
                probe_r5_win = w
            )
        )
    )
)
printf("[SETUP] maestro window = %L  type = %L\n" probe_r5_win type(probe_r5_win))


;; ================================================================
;; Section A: maeMakeEditable prerequisite test
;; ================================================================
printf("\n========== SECTION A: maeMakeEditable prerequisite ==========\n")

printf("  isCallable(maeMakeEditable) = %L\n" isCallable('maeMakeEditable))

probe_r5_editResult = nil
errset(probe_r5_editResult = maeMakeEditable(probe_r5_session))
printf("  maeMakeEditable(session) = %L\n" probe_r5_editResult)

;; Also try with no args and with window
errset(printf("  maeMakeEditable() = %L\n" maeMakeEditable()))
when(probe_r5_win
    errset(printf("  maeMakeEditable(win) = %L\n" maeMakeEditable(probe_r5_win)))
)

;; After maeMakeEditable, retry sevVariables and sevSetVariables
printf("\n  --- post-maeMakeEditable retry ---\n")
probe_r5_postEdit1 = nil
errset(probe_r5_postEdit1 = sevVariables(probe_r5_session))
printf("  sevVariables(session) = %L\n" probe_r5_postEdit1)

probe_r5_postEdit2 = nil
errset(probe_r5_postEdit2 = sevSetVariables(probe_r5_session list(list("C" "999.999f"))))
printf("  sevSetVariables(session, list(list(C 999.999f))) = %L\n" probe_r5_postEdit2)

;; Read back after attempt
errset(printf("  maeGetVar(C) after = %L\n" maeGetVar("C" ?session probe_r5_session)))

;; Restore
errset(maeSetVar("C" probe_r5_origC ?session probe_r5_session))


;; ================================================================
;; Section B: Multiple variable format trials for sevSetVariables
;; ================================================================
printf("\n========== SECTION B: variable format trials ==========\n")

;; sevSetVariables argList = (session variables @key (updateEnvironment 't) eventDriven)
;; Try 7 different formats for the "variables" argument

;; Trial B1: list of (name value) pairs as list of lists
printf("\n--- B1: list(list(name value)) ---\n")
probe_r5_b1 = nil
errset(probe_r5_b1 = sevSetVariables(probe_r5_session list(list("C" "999.999f"))))
printf("  return = %L\n" probe_r5_b1)

;; Trial B2: association list  '((name . value))
printf("\n--- B2: list(name . value) (dotted pair / assoc) ---\n")
probe_r5_b2 = nil
errset(probe_r5_b2 = sevSetVariables(probe_r5_session list(cons("C" "999.999f"))))
printf("  return = %L\n" probe_r5_b2)

;; Trial B3: flat list  '(name value)
printf("\n--- B3: flat list (name value) ---\n")
probe_r5_b3 = nil
errset(probe_r5_b3 = sevSetVariables(probe_r5_session list("C" "999.999f")))
printf("  return = %L\n" probe_r5_b3)

;; Trial B4: single string "C=999.999f"
printf("\n--- B4: string name=value ---\n")
probe_r5_b4 = nil
errset(probe_r5_b4 = sevSetVariables(probe_r5_session "C=999.999f"))
printf("  return = %L\n" probe_r5_b4)

;; Trial B5: list of strings  '("C=999.999f")
printf("\n--- B5: list of strings name=value ---\n")
probe_r5_b5 = nil
errset(probe_r5_b5 = sevSetVariables(probe_r5_session list("C=999.999f")))
printf("  return = %L\n" probe_r5_b5)

;; Trial B6: property-list style  '(name "C" value "999.999f")
printf("\n--- B6: plist style ---\n")
probe_r5_b6 = nil
errset(probe_r5_b6 = sevSetVariables(probe_r5_session list('name "C" 'value "999.999f")))
printf("  return = %L\n" probe_r5_b6)

;; Trial B7: keyword args on sevSetVariables itself
printf("\n--- B7: keyword args ---\n")
probe_r5_b7 = nil
errset(probe_r5_b7 = sevSetVariables(probe_r5_session list(list("C" "999.999f")) ?updateEnvironment t))
printf("  return = %L\n" probe_r5_b7)

;; Read back after all trials
errset(printf("  maeGetVar(C) after B trials = %L\n" maeGetVar("C" ?session probe_r5_session)))

;; Restore
errset(maeSetVar("C" probe_r5_origC ?session probe_r5_session))


;; ================================================================
;; Section C: Session type discovery — sev* may need different handle
;; ================================================================
printf("\n========== SECTION C: session type discovery ==========\n")

;; C1: Try sevGetSession on window handle
printf("  isCallable(sevGetSession) = %L\n" isCallable('sevGetSession))
probe_r5_sevSess1 = nil
when(probe_r5_win
    errset(probe_r5_sevSess1 = sevGetSession(probe_r5_win))
    printf("  sevGetSession(win) = %L  type=%L\n" probe_r5_sevSess1 type(probe_r5_sevSess1))
)

;; C2: sevGetCurrentSession
printf("  isCallable(sevGetCurrentSession) = %L\n" isCallable('sevGetCurrentSession))
probe_r5_sevSess2 = nil
errset(probe_r5_sevSess2 = sevGetCurrentSession())
printf("  sevGetCurrentSession() = %L  type=%L\n" probe_r5_sevSess2 type(probe_r5_sevSess2))

;; C3: asiGetCurrentSession
printf("  isCallable(asiGetCurrentSession) = %L\n" isCallable('asiGetCurrentSession))
probe_r5_sevSess3 = nil
errset(probe_r5_sevSess3 = asiGetCurrentSession())
printf("  asiGetCurrentSession() = %L  type=%L\n" probe_r5_sevSess3 type(probe_r5_sevSess3))

;; C4: asiGetSession on window
printf("  isCallable(asiGetSession) = %L\n" isCallable('asiGetSession))
probe_r5_sevSess4 = nil
when(probe_r5_win
    errset(probe_r5_sevSess4 = asiGetSession(probe_r5_win))
    printf("  asiGetSession(win) = %L  type=%L\n" probe_r5_sevSess4 type(probe_r5_sevSess4))
)

;; C5: Try the window handle directly as session arg to sevVariables
printf("\n  --- using alternative session handles with sevVariables ---\n")
foreach(altSess list(probe_r5_win probe_r5_sevSess1 probe_r5_sevSess2
                     probe_r5_sevSess3 probe_r5_sevSess4 probe_r5_sdb)
    when(altSess
        let((r)
            r = nil
            errset(r = sevVariables(altSess))
            printf("  sevVariables(%L [%L]) = %L\n" altSess type(altSess) r)
        )
    )
)

;; C6: Try sevSetVariables with each alternative session handle
printf("\n  --- using alternative session handles with sevSetVariables ---\n")
foreach(altSess list(probe_r5_win probe_r5_sevSess1 probe_r5_sevSess2
                     probe_r5_sevSess3 probe_r5_sevSess4 probe_r5_sdb)
    when(altSess
        let((r)
            r = nil
            errset(r = sevSetVariables(altSess list(list("C" "999.999f"))))
            printf("  sevSetVariables(%L [%L], ...) = %L\n" altSess type(altSess) r)
            ;; If non-nil, check if C changed
            when(r
                errset(printf("    >>> SUCCESS! maeGetVar(C) = %L\n"
                              maeGetVar("C" ?session probe_r5_session)))
            )
        )
    )
)

;; C7: Try maeGetSessions and use each as sev* session
printf("\n  --- maeGetSessions variants ---\n")
printf("  isCallable(maeGetSessions) = %L\n" isCallable('maeGetSessions))
probe_r5_allSess = nil
errset(probe_r5_allSess = maeGetSessions())
printf("  maeGetSessions() = %L\n" probe_r5_allSess)

;; C8: sevSession / sevGetEnv
printf("  isCallable(sevSession) = %L\n" isCallable('sevSession))
printf("  isCallable(sevGetEnv) = %L\n" isCallable('sevGetEnv))
probe_r5_sevEnv = nil
errset(probe_r5_sevEnv = sevGetEnv(probe_r5_session))
printf("  sevGetEnv(session) = %L  type=%L\n" probe_r5_sevEnv type(probe_r5_sevEnv))
when(probe_r5_win
    errset(printf("  sevGetEnv(win) = %L\n" sevGetEnv(probe_r5_win)))
)

;; C9: sevEnvironment
printf("  isCallable(sevEnvironment) = %L\n" isCallable('sevEnvironment))
probe_r5_sevEnv2 = nil
errset(probe_r5_sevEnv2 = sevEnvironment(probe_r5_session))
printf("  sevEnvironment(session) = %L  type=%L\n" probe_r5_sevEnv2 type(probe_r5_sevEnv2))

;; Restore
errset(maeSetVar("C" probe_r5_origC ?session probe_r5_session))


;; ================================================================
;; Section D: Menu item deep inspection for Edit callback
;; ================================================================
printf("\n========== SECTION D: menu item deep inspection ==========\n")

;; D1: Try hiGetMenuItem / hiGetMenuItemByName for "Design Variables"
printf("  isCallable(hiGetMenuItem) = %L\n" isCallable('hiGetMenuItem))
printf("  isCallable(hiGetMenuItemByName) = %L\n" isCallable('hiGetMenuItemByName))

;; D2: Try to get the banner menu and iterate deeply
when(probe_r5_win
    printf("\n  --- window menu exploration ---\n")

    ;; Try hiGetBannerMenu
    printf("  isCallable(hiGetBannerMenu) = %L\n" isCallable('hiGetBannerMenu))
    probe_r5_banner = nil
    errset(probe_r5_banner = hiGetBannerMenu(probe_r5_win))
    printf("  hiGetBannerMenu(win) = %L  type=%L\n" probe_r5_banner type(probe_r5_banner))

    ;; Try hiGetPulldownMenu
    printf("  isCallable(hiGetPulldownMenu) = %L\n" isCallable('hiGetPulldownMenu))

    ;; Try to access the form/menu via hiGetForm
    printf("  isCallable(hiGetForm) = %L\n" isCallable('hiGetForm))

    ;; Try window slots
    printf("\n  --- window slots ---\n")
    errset(printf("  win->? = %L\n" probe_r5_win->?))
    errset(printf("  win->menus = %L\n" probe_r5_win->menus))
    errset(printf("  win->menuBar = %L\n" probe_r5_win->menuBar))
    errset(printf("  win->bannerMenus = %L\n" probe_r5_win->bannerMenus))
    errset(printf("  win->pulldownMenus = %L\n" probe_r5_win->pulldownMenus))
)

;; D3: Hardcoded callback name candidates — check if any are callable
printf("\n  --- hardcoded callback candidates ---\n")
foreach(fn '(
    sevEditVariables sevEditDesignVars sevEditDesignVariables
    sevEditVariable sevModifyVariables sevModifyDesignVars
    sevOpenVariablesForm sevVariablesForm sevShowVariablesForm
    sevApplyVariables sevUpdateVariables sevCommitVariables
    sevVariablesApply sevVariablesOk sevVariablesCommit
    sevDesignVarEditor sevDesignVarForm
    sevVariablesFormApply sevVariablesFormOK
    adeEditDesignVars adeEditVariables adeModifyDesignVars
    maeEditDesignVars maeEditVariables maeModifyDesignVars
    asiEditDesignVars asiEditVariables
    )
    when(isCallable(fn)
        printf("  [CALLABLE] %s\n" symbolToString(fn))
        errset(printf("    getd = %L\n" getd(fn)))
        errset(printf("    argList = %L\n" getd(fn)->argList))
    )
)

;; D4: Try to find the menu by name in a broader search
printf("\n  --- sevVariablesMenu* symbol search ---\n")
foreach(suffix '("" "31" "32" "30" "Item" "Items" "Edit"
                 "EditItem" "Apply" "Ok" "Callback")
    let((fname sym val)
        fname = strcat("sevVariablesMenu" suffix)
        sym = stringToSymbol(fname)
        when(boundp(sym)
            val = nil
            errset(val = eval(sym))
            printf("  %s = %L  type=%L\n" fname val type(val))
            when(val
                ;; Deep inspect the menu object
                errset(printf("    ->? = %L\n" val->?))
                errset(printf("    ->callback = %L\n" val->callback))
                errset(printf("    ->_callback = %L\n" val->_callback))
                errset(printf("    ->_mapCallback = %L\n" val->_mapCallback))
                errset(printf("    ->itemList = %L\n" val->itemList))
                errset(printf("    ->_menuItemList = %L\n" val->_menuItemList))
                errset(printf("    ->subMenus = %L\n" val->subMenus))

                ;; If it has items, inspect each deeply
                let((items)
                    items = nil
                    errset(items = val->itemList)
                    unless(items errset(items = val->_menuItemList))
                    unless(items errset(items = val->items))
                    unless(items errset(items = val->subItems))
                    when(listp(items)
                        foreach(it items
                            printf("\n    [ITEM] %L\n" it)
                            errset(printf("      ->? = %L\n" it->?))
                            errset(printf("      ->name = %L\n" it->name))
                            errset(printf("      ->callback = %L\n" it->callback))
                            errset(printf("      ->_callback = %L\n" it->_callback))
                            errset(printf("      ->value = %L\n" it->value))
                        )
                    )
                )
            )
        )
    )
)


;; ================================================================
;; Section E: Schematic cellview property dump
;; ================================================================
printf("\n========== SECTION E: schematic cellview properties ==========\n")

;; The "CopyFromCellview" menu item suggests design vars may have defaults
;; stored in the schematic cellview. Check what properties exist there.

probe_r5_schCv = nil
errset(probe_r5_schCv = dbOpenCellViewByType("pll" "LC_VCO_tb" "schematic" "" "r"))
printf("  schematic cv = %L  type=%L\n" probe_r5_schCv type(probe_r5_schCv))

when(probe_r5_schCv
    ;; Dump all properties
    printf("\n  --- schematic cv->prop ---\n")
    errset(
        let((props)
            props = probe_r5_schCv->prop
            printf("  prop count = %L\n" if(listp(props) length(props) props))
            when(listp(props)
                foreach(p props
                    errset(printf("    name=%L value=%L type=%L\n"
                                  p->name p->value type(p->value)))
                )
            )
        )
    )

    ;; Check specific design-var-related properties
    printf("\n  --- specific property lookups ---\n")
    foreach(pn '(designVars designVariables varDef varList variables
                  parameters parameterList simVars desVars globalVars
                  cdfParameters instParameters desVar)
        let((val)
            val = nil
            errset(val = dbGetq(probe_r5_schCv pn))
            when(val
                printf("  dbGetq(cv, %s) = %L\n" symbolToString(pn) val)
            )
        )
    )

    ;; Check instances — design vars might be defined on instances
    printf("\n  --- schematic instances ---\n")
    errset(
        let((insts count)
            insts = probe_r5_schCv->instances
            count = if(listp(insts) length(insts) 0)
            printf("  instance count = %d\n" count)
            ;; Print first 10 instances
            let((i)
                i = 0
                when(listp(insts)
                    foreach(inst insts
                        when(lessp(i 10)
                            errset(printf("    [%d] name=%L cellName=%L\n"
                                          i inst->name inst->cellName))
                            ;; Check for parameters on the instance
                            errset(
                                let((iprops)
                                    iprops = inst->prop
                                    when(listp(iprops)
                                        foreach(ip iprops
                                            errset(printf("         prop: %L = %L\n"
                                                          ip->name ip->value))
                                        )
                                    )
                                )
                            )
                            i = i + 1
                        )
                    )
                )
            )
        )
    )

    ;; Check CDF parameters (Component Description Format)
    printf("\n  --- CDF parameters ---\n")
    printf("  isCallable(cdfGetCellCDF) = %L\n" isCallable('cdfGetCellCDF))
    errset(
        let((cdf)
            cdf = cdfGetCellCDF(ddGetObj("pll" "LC_VCO_tb"))
            printf("  cdfGetCellCDF = %L  type=%L\n" cdf type(cdf))
            when(cdf
                errset(printf("  cdf->? = %L\n" cdf->?))
                errset(printf("  cdf->parameters = %L\n" cdf->parameters))
            )
        )
    )

    ;; List cellview file on disk
    printf("\n  --- schematic cellview file ---\n")
    errset(printf("  fileName = %L\n" probe_r5_schCv->fileName))

    dbClose(probe_r5_schCv)
    printf("  (schematic cv closed)\n")
)

;; Also check the config/adexl view which might store var definitions
printf("\n  --- config/adexl views ---\n")
foreach(vt '("adexl" "config" "maestro")
    let((cv2)
        cv2 = nil
        errset(cv2 = dbOpenCellViewByType("pll" "LC_VCO_tb" vt "" "r"))
        printf("  %s cv = %L\n" vt cv2)
        when(cv2
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
            dbClose(cv2)
        )
    )
)


;; ================================================================
;; Section F: Restore original values
;; ================================================================
printf("\n========== SECTION F: restore ==========\n")

errset(maeSetVar("C" probe_r5_origC ?session probe_r5_session))
printf("  Restored maeSetVar('C') = %L\n" probe_r5_origC)

;; Also restore via sev* in case any trial worked
errset(sevSetVariables(probe_r5_session list(list("C" probe_r5_origC))))
errset(sevAddVar(probe_r5_session "C" probe_r5_origC))

;; Save
errset(maeSaveSetup(?lib "pll" ?cell "LC_VCO_tb" ?view "maestro"
                    ?session probe_r5_session))
printf("  maeSaveSetup called.\n")

;; Final readback
probe_r5_finalC = nil
errset(probe_r5_finalC = maeGetVar("C" ?session probe_r5_session))
printf("  Final maeGetVar('C') = %L\n" probe_r5_finalC)

printf("\n===== probe_sev_round5 END =====\n")
t
