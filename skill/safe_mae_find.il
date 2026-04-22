;;; safe_mae_find.il -- Auto-discover a Maestro-produced input.scs for a
;;; (lib, tb_cell) pair. Removes the need for users to hand-pass
;;; --scs-path on every run_agent invocation.
;;;
;;; Compatible with Cadence IC23.1 / IC6.1.8 SKILL (no SKILL++).
;;;
;;; Requires: helpers.il (jsonEscape), safe_set_param.il (validateName).
;;;
;;; Entry point:
;;;   safeMaeFindInputScs(libName tbCellName)
;;;
;;; Enumerates three standard IC23.1 output layouts under $HOME/simulation
;;; and returns the best input.scs by tier priority, then newest mtime
;;; within the chosen tier:
;;;   {"ok":true, "path":"<abs>", "tier":"maestro|ade_flat|ade_explorer",
;;;    "mtime":N, "numCandidates":K}
;;; or:
;;;   {"ok":false, "error":"..."}
;;;
;;; Tier catalog (priority order):
;;;   maestro      -- $HOME/simulation/<lib>/<tb>/maestro/results/maestro/
;;;                   ExplorerRun.<N>/<pt>/<lib>_<tb>_<pi>/netlist/input.scs
;;;                   Has spectre.fc alongside -> best for Plan Auto.
;;;   ade_explorer -- $HOME/simulation/<tb>/ADE_Explorer/schematic/psf/
;;;                   ExplorerRun.<N>/input.scs (rare).
;;;   ade_flat     -- $HOME/simulation/<tb>/spectre/schematic/netlist/input.scs
;;;                   Agent's own safeOceanRun writes here -> last resort,
;;;                   otherwise self-feedback shadows the real testbench.
;;;
;;; Security:
;;;   - libName / tbCellName validated via safeSetParam_validateName.
;;;   - No shell execution, no recursion outside $HOME/simulation.
;;;   - Read-only filesystem inspection; no mutation.


procedure(safeMaeFind_pushCandidate(candList scsPath tier)
    ;; Helper: append (path tier mtime) triple if file exists and is readable.
    let((mtime)
        when(isFile(scsPath)
            mtime = 0
            errset(mtime = fileTimeModified(scsPath))
            unless(numberp(mtime) mtime = 0)
            candList = cons(list(scsPath tier mtime) candList)
        )
        candList
    )
)


procedure(safeMaeFind_collectMaestro(simRoot libName tbCellName candList)
    ;; Walk $simRoot/<lib>/<tb>/maestro/results/maestro/ExplorerRun.*/*/*/netlist/input.scs
    let((maeBase explorerDirs runPath pointDirs pPath ptDirs scs)
        maeBase = sprintf(nil "%s/%s/%s/maestro/results/maestro"
                          simRoot libName tbCellName)
        when(isDir(maeBase)
            explorerDirs = getDirFiles(maeBase)
            foreach(runDir explorerDirs
                when(and(stringp(runDir)
                         (strlen(runDir) >= 12)
                         equal(substring(runDir 1 12) "ExplorerRun."))
                    runPath = strcat(maeBase "/" runDir)
                    when(isDir(runPath)
                        pointDirs = getDirFiles(runPath)
                        foreach(p pointDirs
                            when(and(stringp(p)
                                     not(equal(p "."))
                                     not(equal(p "..")))
                                pPath = strcat(runPath "/" p)
                                when(isDir(pPath)
                                    ptDirs = getDirFiles(pPath)
                                    foreach(pt ptDirs
                                        when(and(stringp(pt)
                                                 not(equal(pt "."))
                                                 not(equal(pt "..")))
                                            scs = strcat(pPath "/" pt "/netlist/input.scs")
                                            candList = safeMaeFind_pushCandidate(
                                                candList scs "maestro")
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        candList
    )
)


procedure(safeMaeFind_collectFlat(simRoot tbCellName candList)
    let((scs)
        scs = sprintf(nil "%s/%s/spectre/schematic/netlist/input.scs"
                      simRoot tbCellName)
        safeMaeFind_pushCandidate(candList scs "ade_flat")
    )
)


procedure(safeMaeFind_collectAdeExplorer(simRoot tbCellName candList)
    let((base runDirs scs)
        base = sprintf(nil "%s/%s/ADE_Explorer/schematic/psf"
                       simRoot tbCellName)
        when(isDir(base)
            runDirs = getDirFiles(base)
            foreach(runDir runDirs
                when(and(stringp(runDir)
                         (strlen(runDir) >= 12)
                         equal(substring(runDir 1 12) "ExplorerRun."))
                    scs = strcat(base "/" runDir "/input.scs")
                    candList = safeMaeFind_pushCandidate(candList scs "ade_explorer")
                )
            )
        )
        candList
    )
)


procedure(safeMaeFindInputScs(libName tbCellName)
    prog((home simRoot candidates best numCand)
        ;; ---- Validate names (same regex as safeSetParam) ----
        unless(safeSetParam_validateName(libName "lib")
            return("{\"ok\":false,\"error\":\"Invalid lib name\"}")
        )
        unless(safeSetParam_validateName(tbCellName "tb_cell")
            return("{\"ok\":false,\"error\":\"Invalid tb_cell name\"}")
        )

        home = getShellEnvVar("HOME")
        unless(home
            return("{\"ok\":false,\"error\":\"HOME env var not set\"}")
        )
        simRoot = strcat(home "/simulation")
        unless(isDir(simRoot)
            return(sprintf(nil "{\"ok\":false,\"error\":\"sim root missing: %s\"}"
                           safeHelpers_jsonEscape(simRoot)))
        )

        candidates = nil
        candidates = safeMaeFind_collectMaestro(simRoot libName tbCellName candidates)
        candidates = safeMaeFind_collectFlat(simRoot tbCellName candidates)
        candidates = safeMaeFind_collectAdeExplorer(simRoot tbCellName candidates)

        numCand = length(candidates)
        when(numCand == 0
            return(sprintf(nil
                "{\"ok\":false,\"error\":\"no input.scs found for %s/%s under %s\"}"
                safeHelpers_jsonEscape(libName)
                safeHelpers_jsonEscape(tbCellName)
                safeHelpers_jsonEscape(simRoot)))
        )

        ;; ---- Tier-priority selection (then newest mtime within tier) ----
        ;; Order: maestro > ade_explorer > ade_flat. We deliberately do NOT
        ;; rank by mtime across tiers: safeOceanRun writes its own bare
        ;; [tran] netlist into $simRoot/<tb>/spectre/schematic/netlist/
        ;; (the ade_flat tier) on every iter, which makes that file
        ;; strictly newer than the user's Maestro-authored netlist after
        ;; one iter. Picking by mtime would create a self-feedback loop
        ;; where each iter shadows the real testbench with the agent's
        ;; previous iteration's stripped-down netlist -> no saves,
        ;; OCN-6038 tran-not-available, every measurement null.
        best = nil
        foreach(tierName list("maestro" "ade_explorer" "ade_flat")
            when(null(best)
                foreach(c candidates
                    when(equal(nth(1 c) tierName)
                        when(or(null(best) nth(2 c) > nth(2 best))
                            best = c
                        )
                    )
                )
            )
        )

        return(sprintf(nil
            "{\"ok\":true,\"path\":\"%s\",\"tier\":\"%s\",\"mtime\":%d,\"numCandidates\":%d}"
            safeHelpers_jsonEscape(nth(0 best))
            safeHelpers_jsonEscape(nth(1 best))
            nth(2 best)
            numCand))
    )
)


;;; ----------------------------------------------------------------
;;; Load-time self-test: just make sure the validation rejects junk.
;;; A "real" test needs the filesystem, which we can't assume here.
;;; ----------------------------------------------------------------
let((jsonOut)
    ;; Bad lib name -> must return ok:false with "Invalid lib name".
    jsonOut = safeMaeFindInputScs("bad;rm" "LC_VCO_tb")
    unless(pcreMatchp("Invalid lib name" jsonOut)
        error("SELF-TEST FAIL: safeMaeFindInputScs accepted 'bad;rm' as lib")
    )
    ;; Bad tb name -> must return ok:false with "Invalid tb_cell name".
    jsonOut = safeMaeFindInputScs("pll" "bad;rm")
    unless(pcreMatchp("Invalid tb_cell name" jsonOut)
        error("SELF-TEST FAIL: safeMaeFindInputScs accepted 'bad;rm' as tb_cell")
    )
)
