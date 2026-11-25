# At-once lists against the reference pass

Made when the reference pass (docs/natives/README.md) was integrated, 2026-11-25. "list" is what x3kc/targets.py treated the native as before this review (in x3kc/sync_natives_ap.txt / sync_natives_fl.txt or not), "pass" is the spec field `waits`. AP and FL disagreed in the same 118 natives (the FL list is the AP list plus FL's two B3D quality natives, both at once in both).

## Applied

Only the two natives that wait although the lists said at once were changed: SA_FlyObjectBeforeContainer and SA_FlyObjectIntoContainer are no longer in sync_natives_ap.txt and sync_natives_fl.txt. The cause was docs/natives/sync_scan.py: `rt cases` also lists the switches nested in handlers (SA_CalcReward 0x00468e19, SA_DamageObject 0x004640c8), labelled with the group's names by case number, and the scan let those cases 0..10 overwrite the dispatcher's own cases 0..10 (SA_AllocObject .. SA_FlyObjectBeforeContainer). The scan now keeps only the switch with the most cases; re-run for AP and FL it reproduces both lists exactly (the other nine overwritten natives come out the same as before).

The other 116 disagreements stay "may wait" on purpose: counting a native as possibly waiting only makes `--base` check more call sites (false alarms at worst), while a wrong "at once" hides a real savegame return site. Some verdicts also depend on reading the pass more strictly than the code allows: INS_CockpitSetTracking answers at once on its normal paths but never answers for an unknown cockpit (bugs.md B2), so a task can sit in that call for ever and a savegame can hold its pc.

Save check with the applied lists (x3_src main b18cfd621, `tools/kcbuild.py <ed> <obj> --x3kc <this checkout>`): base return sites ap/x3story 11542 (was 11538), ap/x3galedit 9706 (9702), fl/x3story 15701 (15697), fl/x3galedit 12896 (12892); all mapped, 0 gone, 0 WRONG. The four new ones in each object are the calls of the two natives in C2004.__LandInHangar and C2004.__EnterHyperspace.

## Counts

| group | list may wait, pass at once | list at once, pass waits |
|---|---|---|
| B3D | 1 | 0 |
| GS | 6 | 0 |
| INS | 1 | 0 |
| J3D | 19 | 0 |
| MOV | 11 | 0 |
| P | 30 | 0 |
| SA | 3 | 2 |
| SE | 1 | 0 |
| SFX | 37 | 0 |
| TI | 4 | 0 |
| X2 | 3 | 0 |
| all | 116 | 2 |

The engine's own natives (sync_natives_x3src.txt) agree with the pass. P_LoadLocation (a one-frame yield), the play natives of MOV/SFX, the J3D job and take natives, SE_AutoDoc and INS_AutoDoc (never answer) are "may wait" in both.

## Natives that wait although the list said at once (fixed)

These were missing savegame return sites: a savegame taken while a task waited in such a call held a pc that `x3kc compile --base` did not check.

| native | list | pass | evidence |
|---|---|---|---|
| SA_FlyObjectBeforeContainer | at once | waits | AP 0x00460e92 registers the task as the ship's waiter (obj+0x7c/+0x80) and jumps to the common exit 0x00462e00 past the ResumeTask call: the task waits for the order (1 arrived, 8 removed, SA_StopObject value). The old "at once" came from sync_scan reading a nested switch case (fixed). |
| SA_FlyObjectIntoContainer | at once | waits | AP 0x00460ca0 registers the waiter, 0x00460ca9 jumps to 0x00462e00 past the ResumeTask call (order 5): waits until docked. The old "at once": the same sync_scan bug. |

## Natives that answer at once although the list says may wait (kept)

Following the pass would remove their call sites (and the calls of methods that reach only such natives) from the return sites: fewer false alarms. Not applied (see "Applied").

| native | list | pass | evidence |
|---|---|---|---|
| B3D_MemStatistics | may wait | at once | nested switch (indirect JMP at AP 0x00495a4a) that sync_scan does not follow; every arm calls ResumeTaskWithIntVm. |
| GS_AddGate | may wait | at once | answers before the dispatcher returns (pass, case 0x00418be5); group never analysed |
| GS_RemoveGate | may wait | at once | answers before the dispatcher returns (pass, case 0x00418c37); group never analysed |
| GS_Route | may wait | at once | answers before the dispatcher returns (pass, case 0x00418c65); group never analysed |
| GS_RouteDist | may wait | at once | answers before the dispatcher returns (pass, case 0x00418c65); group never analysed |
| GS_RouteLen | may wait | at once | answers before the dispatcher returns (pass, case 0x00418c65); group never analysed |
| GS_SetSectorOwner | may wait | at once | answers before the dispatcher returns (pass, case 0x00418ba2); group never analysed |
| INS_CockpitSetTracking | may wait | at once | normal paths answer 0 at once, but an unknown cockpit returns without answering (AP 0x0042dd33, bugs.md B2): the task then waits for ever at this site. Keep "may wait" (a savegame can hold that pc). |
| J3D_FindPath | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049580b; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_FreeCut | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x00495870; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetNumPaths | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049515e; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathBody | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049518c; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathCut | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049518c; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathFather | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049518c; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathFlags | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049518c; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathName | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049528f; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathPosAndRot | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004952e0; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_GetPathSerial | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x0049518c; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_JobGetRemainingTime | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004957dc; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_JobPlayAsync | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004956da; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_JobPlayAsyncS | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004956da; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_JobStop | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004957bd; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_LoadCut | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x00495847; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_SetCutPos | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x00495891; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_TakeAlloc | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004958d4; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_TakeFree | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x004958f6; only job start/play and J3D_TakeStart wait); group never analysed |
| J3D_TakeStop | may wait | at once | answers before the dispatcher returns (pass, B3D dispatcher case 0x00495915; only job start/play and J3D_TakeStart wait); group never analysed |
| MOV_ConnectMovieToTexture | may wait | at once | answers before the dispatcher returns (pass, case 0x004998ee; only the play natives wait); group never analysed |
| MOV_FreeMovie | may wait | at once | answers before the dispatcher returns (pass, case 0x00499859; only the play natives wait); group never analysed |
| MOV_FreeVoiceInfo | may wait | at once | answers before the dispatcher returns (pass, case 0x00499a0a; only the play natives wait); group never analysed |
| MOV_GetVoiceLength | may wait | at once | answers before the dispatcher returns (pass, case 0x00499a68; only the play natives wait); group never analysed |
| MOV_GetVoiceStart | may wait | at once | answers before the dispatcher returns (pass, case 0x00499a23; only the play natives wait); group never analysed |
| MOV_GetVoiceStream | may wait | at once | answers before the dispatcher returns (pass, case 0x00499a8e; only the play natives wait); group never analysed |
| MOV_LoadMovie | may wait | at once | answers before the dispatcher returns (pass, case 0x004997dd; only the play natives wait); group never analysed |
| MOV_LoadVoiceInfo | may wait | at once | answers before the dispatcher returns (pass, case 0x004999ea; only the play natives wait); group never analysed |
| MOV_SetRateMovie | may wait | at once | answers before the dispatcher returns (pass, case 0x00499891; only the play natives wait); group never analysed |
| MOV_SetVolumeMovie | may wait | at once | answers before the dispatcher returns (pass, case 0x00499992; only the play natives wait); group never analysed |
| MOV_StopMovie | may wait | at once | answers before the dispatcher returns (pass, case 0x00499875; only the play natives wait); group never analysed |
| P_ClearEngineFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00498018); the group was never flow-analysed and counted as may wait |
| P_ClearScreen | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497d86); the group was never flow-analysed and counted as may wait |
| P_GetJoyAxisDeadzone | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497eb8); the group was never flow-analysed and counted as may wait |
| P_GetJoyDeadzone | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497e78); the group was never flow-analysed and counted as may wait |
| P_GetJoySensitivity | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497f41); the group was never flow-analysed and counted as may wait |
| P_GetKeyName | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497e4a); the group was never flow-analysed and counted as may wait |
| P_GetLocalizedKey | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497e27); the group was never flow-analysed and counted as may wait |
| P_GetSysD3DFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497c1d); the group was never flow-analysed and counted as may wait |
| P_GetSysD3DFlags2 | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497c68); the group was never flow-analysed and counted as may wait |
| P_GetSysFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497bd2); the group was never flow-analysed and counted as may wait |
| P_GetSysIORequirements | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497f81); the group was never flow-analysed and counted as may wait |
| P_GetSysProfilingFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497d3b); the group was never flow-analysed and counted as may wait |
| P_GetSysShaderQuality | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497d19); the group was never flow-analysed and counted as may wait |
| P_GetSysViewDistance | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497cb3); the group was never flow-analysed and counted as may wait |
| P_InitLoadingScreen | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497e00); the group was never flow-analysed and counted as may wait |
| P_Leave | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497ba8); the group was never flow-analysed and counted as may wait |
| P_PrepareInterruption | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497bb6); the group was never flow-analysed and counted as may wait |
| P_SetEngineFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00498018); the group was never flow-analysed and counted as may wait |
| P_SetJoyAxisDeadzone | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497f02); the group was never flow-analysed and counted as may wait |
| P_SetJoyDeadzone | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497e94); the group was never flow-analysed and counted as may wait |
| P_SetJoySensitivity | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497f5d); the group was never flow-analysed and counted as may wait |
| P_SetSysD3DFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497c3f); the group was never flow-analysed and counted as may wait |
| P_SetSysD3DFlags2 | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497c8a); the group was never flow-analysed and counted as may wait |
| P_SetSysFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497bf4); the group was never flow-analysed and counted as may wait |
| P_SetSysIORequirements | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497fa1); the group was never flow-analysed and counted as may wait |
| P_SetSysProfilingFlags | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497d5d); the group was never flow-analysed and counted as may wait |
| P_SetSysViewDistance | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497cd5); the group was never flow-analysed and counted as may wait |
| P_ShowNextLoadingScreen | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497e1d); the group was never flow-analysed and counted as may wait |
| P_ViewEBMFullscreen | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497da3); the group was never flow-analysed and counted as may wait |
| P_ViewLoadingScreen | may wait | at once | answers before the dispatcher returns (pass, Script::DispatchPCommand case 0x00497dd0); the group was never flow-analysed and counted as may wait |
| SA_CalcReward | may wait | at once | pass: every kind answers through ResumeTaskWithIntVm; the reason for sync_scan's "may wait" is not traced. |
| SA_SetRotSpeed | may wait | at once | the answer goes through NativeCompletion, which sync_scan does not count as a resume. |
| SA_ShowDummyExplosion | may wait | at once | the path leaves through a shared tail sync_scan does not follow; every path answers 0 (x3_src and AP read). |
| SE_KillTask | may wait | at once | resumes the caller with 0; a task that kills itself is freed (dispatcher returns 0) and leaves no return site. sync_scan sees that self-kill path return without a resume. |
| SFX_3DCONTROL | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| SFX_AUTOFREE | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| SFX_DSIK | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| SFX_ExportPrivateInfo | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c58a; only the play natives wait); group never analysed |
| SFX_FindSample | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c41f; only the play natives wait); group never analysed |
| SFX_FreeMOD | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c396; only the play natives wait); group never analysed |
| SFX_FreeSample | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c43f; only the play natives wait); group never analysed |
| SFX_GetFXVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c5cd; only the play natives wait); group never analysed |
| SFX_GetFlags | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c5ed; only the play natives wait); group never analysed |
| SFX_GetLanguage | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c635; only the play natives wait); group never analysed |
| SFX_GetMODVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c5ae; only the play natives wait); group never analysed |
| SFX_GetMasterVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c4b5; only the play natives wait); group never analysed |
| SFX_GetPanningState | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c49f; only the play natives wait); group never analysed |
| SFX_GetSampleDuration | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c56a; only the play natives wait); group never analysed |
| SFX_GetVoiceLanguage | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c67f; only the play natives wait); group never analysed |
| SFX_GetVoiceVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c6cc; only the play natives wait); group never analysed |
| SFX_INTERPOLATE | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| SFX_LOOP | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| SFX_LoadMOD | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c309; only the play natives wait); group never analysed |
| SFX_LoadSample | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c22e; only the play natives wait); group never analysed |
| SFX_ONE_MEM_BLOCK | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| SFX_PlayMOD | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c333; only the play natives wait); group never analysed |
| SFX_ReadMODAttributes | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c54a; only the play natives wait); group never analysed |
| SFX_SetChannelInfo | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c505; only the play natives wait); group never analysed |
| SFX_SetEAXRoom | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c762; only the play natives wait); group never analysed |
| SFX_SetFXVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c3b7; only the play natives wait); group never analysed |
| SFX_SetFlags | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c60d; only the play natives wait); group never analysed |
| SFX_SetLanguage | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c655; only the play natives wait); group never analysed |
| SFX_SetMODAttributes | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c4e3; only the play natives wait); group never analysed |
| SFX_SetMODVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c3de; only the play natives wait); group never analysed |
| SFX_SetMasterVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c460; only the play natives wait); group never analysed |
| SFX_SetMusicType | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c6ed; only the play natives wait); group never analysed |
| SFX_SetPanningState | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c481; only the play natives wait); group never analysed |
| SFX_SetVoiceVolume | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c69c; only the play natives wait); group never analysed |
| SFX_StopMOD | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c34d; only the play natives wait); group never analysed |
| SFX_StopSample | may wait | at once | answers before the dispatcher returns (pass, case 0x0049c3ff; only the play natives wait); group never analysed |
| SFX_TOBECONTINUED | may wait | at once | sound bank flag name, in no dispatcher name table: a call resolves to no native and answers int 0 at once (pass, from the x3_src interpreter) |
| TI_GetLastFrameDelay | may wait | at once | TiDispatch case resumes with ResumeTaskWithIntVm (AP and FL case lists); TI came only from the specs, which lacked it. |
| TI_GetTimeWarpMultiplier | may wait | at once | as TI_GetLastFrameDelay (ResumeTaskWithIntVm in the case). |
| TI_ReadSystemClock | may wait | at once | Main::TickGameClock, then ResumeTaskWithIntVm in the case (busy-waits up to 1 ms inside the call, no task wait). |
| TI_SetAbsTime | may wait | at once | sets the game clock, ResumeTaskWithIntVm in the case. |
| X2_GetScene | may wait | at once | not a case of the X2 jump table: the default arm AP 0x004076c4 calls Script::ResumeTask; sync_scan walks the switch cases only. |
| X2_GetSpace | may wait | at once | default arm AP 0x004076c4 (ResumeTask), as X2_GetScene. |
| X2_SetGalaxy | may wait | at once | default arm AP 0x004076c4 (ResumeTask), as X2_GetScene. |

## Savegame impact if the lists followed the pass entirely (not applied)

Return sites (calls a task can wait in, kcsave.waiting_sites, target x3_src-<edition>) of the x3_src KC objects, with the lists as they were before this review and with a temporary variant that follows the pass (every disagreement above, INS_CockpitSetTracking included). Built with `python tools/kcbuild.py <ed> <obj> -o build/tmp/natref/... --x3kc <this checkout>` from x3_src main (ddeab7a91) once per variant; both builds pass (base return sites all mapped, 0 gone, 0 WRONG), the objects are byte-identical (the lists change only the check). "release" is the release object in kc/<ed>/base the build is checked against.

| object | before | following the pass | disappear | appear |
|---|---|---|---|---|
| ap/x3story built | 11541 | 10510 | 1035 | 4 |
| ap/x3story release | 11538 | 10507 | 1035 | 4 |
| ap/x3galedit built | 9702 | 8778 | 928 | 4 |
| ap/x3galedit release | 9702 | 8778 | 928 | 4 |
| fl/x3story built | 15700 | 14377 | 1327 | 4 |
| fl/x3story release | 15697 | 14374 | 1327 | 4 |
| fl/x3galedit built | 12892 | 11718 | 1178 | 4 |
| fl/x3galedit release | 12892 | 11718 | 1178 | 4 |

Appear (the same four in every object): the calls of SA_FlyObjectBeforeContainer and SA_FlyObjectIntoContainer in C2004.__LandInHangar and C2004.__EnterHyperspace (offsets per build:
ap/x3story 754161 __LandInHangar, 754285 __LandInHangar, 774269 __EnterHyperspace, 774470 __EnterHyperspace;
ap/x3galedit 760090 __LandInHangar, 760214 __LandInHangar, 780086 __EnterHyperspace, 780287 __EnterHyperspace;
fl/x3story 1402895 __LandInHangar, 1403019 __LandInHangar, 1426913 __EnterHyperspace, 1427186 __EnterHyperspace;
fl/x3galedit 1401460 __LandInHangar, 1401584 __LandInHangar, 1425478 __EnterHyperspace, 1425751 __EnterHyperspace).

Disappear: native call sites and method calls that only reached such natives. In the built objects:

- ap/x3story: 353 native sites (P 89, MOV 81, GS 68, SFX 47, SE 37, J3D 20, SA 8, INS 3; most: SE_KillTask 37, GS_RouteLen 33, P_GetSysD3DFlags2 25, MOV_LoadMovie 21, MOV_ConnectMovieToTexture 20, MOV_StopMovie 17) and 682 method-call sites (menu line builders such as C2200.AddMenuLineWithValue, C2296.AddKeyLine, C2251.AddOptionLine, SetPos, whose waits came only from natives the pass finds at once).
- ap/x3galedit: 298 native sites (MOV 81, P 65, GS 49, SFX 47, SE 28, J3D 21, SA 4, INS 3; most: SE_KillTask 28, GS_RouteLen 24, MOV_LoadMovie 21, MOV_ConnectMovieToTexture 20, MOV_StopMovie 17, SFX_GetLanguage 13) and 630 method-call sites (menu line builders such as C2200.AddMenuLineWithValue, C2296.AddKeyLine, C2251.AddOptionLine, SetPos, whose waits came only from natives the pass finds at once).
- fl/x3story: 373 native sites (P 87, GS 85, MOV 81, SFX 47, SE 40, J3D 20, SA 9, INS 3, TI 1; most: GS_RouteLen 41, SE_KillTask 40, P_GetSysD3DFlags2 23, GS_Route 22, MOV_LoadMovie 21, MOV_ConnectMovieToTexture 20) and 954 method-call sites (menu line builders such as C2200.AddMenuLineWithValue, C2296.AddKeyLine, C2251.AddOptionLine, SetPos, whose waits came only from natives the pass finds at once).
- fl/x3galedit: 320 native sites (MOV 81, P 67, GS 64, SFX 47, SE 31, J3D 21, SA 5, INS 3, TI 1; most: GS_RouteLen 32, SE_KillTask 31, MOV_LoadMovie 21, MOV_ConnectMovieToTexture 20, MOV_StopMovie 17, GS_Route 14) and 858 method-call sites (menu line builders such as C2200.AddMenuLineWithValue, C2296.AddKeyLine, C2251.AddOptionLine, SetPos, whose waits came only from natives the pass finds at once).

So following the pass entirely would drop about 9 % of the return sites (false alarms for `--base`) and add the four real ones; only the four real ones were taken (see "Applied"). Neither changes an object or a savegame, only what `--base` checks.
