# Kanban bookkeeping/crash/recovery diagnosis for t_10e18ec6

Scope: evidence-backed research on repeated same-day Kanban lifecycle failures, focused on t_2243f558, t_53403d98, t_ae8debc5, t_f509f358, plus related same-day patterns. No code/config/service changes were made.

Evidence sources inspected:
- SQLite board DB: `/Users/verkyyi/.hermes/kanban.db`
- Worker logs: `/Users/verkyyi/.hermes/kanban/logs/<task_id>.log`
- Gateway log: `/Users/verkyyi/.hermes/logs/gateway.log`
- Worker profile logs: `/Users/verkyyi/.hermes/profiles/worker-ops/logs/agent.log`, `/Users/verkyyi/.hermes/profiles/worker-ops/logs/errors.log`, `/Users/verkyyi/.hermes/profiles/worker-code/logs/errors.log`
- Kanban code paths: `/Users/verkyyi/.hermes/hermes-agent/hermes_cli/kanban_db.py`, `/Users/verkyyi/.hermes/hermes-agent/tools/kanban_tools.py`

## Executive diagnosis

There was not one single root cause. There were four interacting mechanisms:

1. Forced task skills caused immediate worker startup exits earlier in the day.
   - Evidence: worker logs for multiple same-day tasks contain only `Error: Unknown skill(s): ...` repeated once per retry:
     - `/Users/verkyyi/.hermes/kanban/logs/t_4dac423b.log`: `Error: Unknown skill(s): systematic-debugging`
     - `/Users/verkyyi/.hermes/kanban/logs/t_2e6dea9d.log`: `Error: Unknown skill(s): systematic-debugging, test-driven-development`
     - `/Users/verkyyi/.hermes/kanban/logs/t_f15ffc68.log`: `Error: Unknown skill(s): tailnet-service-ops`
     - `/Users/verkyyi/.hermes/kanban/logs/t_b5b71fcf.log`: `Error: Unknown skill(s): hermes-agent`
     - `/Users/verkyyi/.hermes/kanban/logs/t_9bc35ddf.log`: `Error: Unknown skill(s): founder-command-center`
   - Board evidence: these tasks show 60–61 second crash/retry cycles until failure_limit/gave_up. Example t_9bc35ddf runs 215/216/218/219/221 all crashed with `pid ... not alive`, then gave_up at 09:59:52.
   - Mechanism: `_default_spawn()` successfully Popened `hermes -p <profile> --skills ...`, so the dispatcher considered spawn successful. The child then exited almost immediately because the profile could not resolve the forced skill. The next dispatcher tick saw the PID gone and counted it as a crash. Because exit status was not available in the DB/event, the visible error collapsed to `pid not alive` instead of the real `Unknown skill(s)` message.

2. Heartbeats did not extend claim expiry for the long hermes-hk AgentFeeds jobs at the time they ran.
   - Evidence: t_f509f358 run 246 was claimed 13:14:59 with 15 min TTL and had heartbeat at 13:27:40, but was reclaimed at 13:30:01 anyway. If heartbeat had extended `claim_expires`, it should not have expired at 13:30.
   - Evidence: t_ae8debc5 run 253 was claimed 13:48:32, heartbeat 13:52:54, reclaimed 14:03:34; run 254 was claimed 14:03:34, heartbeat 14:10:49, reclaimed 14:18:36. Both reclaims align with the original 900s TTL, not with the heartbeat time.
   - Code path: current `DEFAULT_CLAIM_TTL_SECONDS` is 900 seconds in `kanban_db.py:94-98`. Current `tools/kanban_tools.py:438-472` has a comment saying `kanban_heartbeat` must call `heartbeat_claim` before recording heartbeat, exactly to avoid this trap. The evidence shows the affected runs happened before or without that effective behavior.
   - Mechanism: worker was alive and working, but dispatcher saw expired `claim_expires`, ended the run as `reclaimed`, and spawned a replacement. The original worker then became a stale/zombie worker from the board’s perspective.

3. Stale workers continued after reclaim and then correctly failed `kanban_complete` because expected run ID no longer matched current run.
   - Evidence: t_f509f358 run 246 was reclaimed at 13:30:01, yet the old worker wrote a durable comment at 13:33:52 saying it completed remote work but `kanban_complete` rejected because run 249 had been spawned.
   - Evidence: t_ae8debc5 run 253 was reclaimed at 14:03:34, then commented at 14:19:02 saying the stale/reclaimed worker completed remote config but completion was rejected. Run 254 similarly reclaimed at 14:18:36; run 257 later completed at 14:26:57.
   - Code path: `tools/kanban_tools.py:334-392` passes `expected_run_id=_worker_run_id(tid)` to `complete_task()`. `_worker_run_id()` reads `HERMES_KANBAN_RUN_ID` only for the current task. `complete_task()` then requires `current_run_id = expected_run_id` in `kanban_db.py:2281-2296`; if not, it returns False and the tool reports `could not complete ... (unknown id or already terminal)`.
   - Interpretation: the completion rejection is a safety feature, not the initial bug. It prevented a stale process from completing a run it no longer owned. The UX is bad because the error message does not distinguish `stale expected_run_id`, `blocked/gave_up terminal state`, and unknown task.

4. Later t_2243f558 / t_53403d98 ended blocked/gave_up because the dispatcher counted two worker process deaths, then manual/continued work wrote comments instead of terminal completion.
   - t_2243f558 timeline:
     - created 14:16:47, run 256 claimed/spawned 14:17:36 pid 36538
     - run 256 crashed 14:28:38, error `pid 36538 not alive`
     - run 297 claimed/spawned 14:29:38 pid 54548
     - run 297 crashed 14:31:00, then gave_up with failures=2/effective_limit=2
     - comments at 14:32:21 and 14:36:22 contain the actual PR handoff; final result includes PR https://github.com/NousResearch/hermes-agent/pull/21523
     - worker log `/Users/verkyyi/.hermes/kanban/logs/t_2243f558.log` lines 1019-1060 show final `kanban_complete` rejected because the card had already been moved to blocked/gave_up.
   - t_53403d98 timeline:
     - created 14:25:17, run 258 claimed/spawned 14:25:37 pid 48649
     - run 258 crashed 14:28:38, error `pid 48649 not alive`
     - run 298 claimed/spawned 14:29:38 pid 54549
     - run 298 crashed 14:31:00, then gave_up with failures=2/effective_limit=2
     - comments at 14:32:50 and 14:33:27 contain final sample/handoff
     - worker log `/Users/verkyyi/.hermes/kanban/logs/t_53403d98.log` lines 179, 207, 253, 255-258 show completion attempts/rejections and durable fallback comment after blocked state.
   - Code/config: default/global failure limit is 2 (`kanban.failure_limit: 2`; `DEFAULT_SPAWN_FAILURE_LIMIT 2`, `DEFAULT_FAILURE_LIMIT 2`). `_record_task_failure()` increments `consecutive_failures` and blocks on reaching the effective limit (`kanban_db.py:3246-3397`).
   - Mechanism: once the task became blocked/gave_up, current worker env still carried old `HERMES_KANBAN_RUN_ID`, so `kanban_complete` could not match `current_run_id` and comments were the only non-destructive fallback.

## Same-day pattern summary

Non-test-worker failed/reclaimed runs on 2026-05-07:
- 36 failed/reclaimed runs across 13 affected tasks.
- Outcomes: 32 `crashed`, 4 `reclaimed`.
- By assignee: worker-ops 20, worker-code 7, worker-research 6, worker 3.
- A large early cluster was immediate startup failures from unknown forced skills, visible as 60-second retry loops.
- The hermes-hk AgentFeeds cluster was stale-claim reclaim around the 15-minute TTL despite heartbeats.
- The later PR/sample cluster was blocked by the two-failure circuit breaker after worker PIDs disappeared, with real useful work later captured in comments.

## Notification vs lifecycle

Notification was not the root cause for the main bookkeeping failures.
- Gateway log `/Users/verkyyi/.hermes/logs/gateway.log` lines 19352-19354 shows notifier successfully sent events for t_ae8debc5, t_2243f558, and t_53403d98 to Telegram around 14:27-14:28.
- A separate Telegram notifier bug appears at lines 19356-19386: `invalid literal for int() with base 10: 'thread-789'`, causing send failures for t_7f77df21 and later t_9a2f5c5c. That is a real notifier bug but not the lifecycle root cause for the named tasks.

## Prioritized fixes / tests

Immediate ops mitigations:
1. Keep `kanban_heartbeat` cadence below 15 min for long tasks, but also verify it actually extends `claim_expires` in the live code path. For remote AgentFeeds/SSH jobs, heartbeat every 5 minutes is reasonable.
2. For known long jobs, create tasks with larger `max_runtime_seconds` and/or a longer claim TTL if exposed, or avoid board-spawned workers for long remote scripts until heartbeat extension is verified.
3. Keep forced task skills limited to skills known to the assigned profile. The local forced-skill bypass/fix may already address this; validate by spawning a tiny task with `systematic-debugging`, `tailnet-service-ops`, `founder-command-center`, and `hermes-agent` against each intended worker profile.
4. Consider raising `kanban.failure_limit` above 2 for worker tasks while diagnostics are still noisy. This is only a mitigation; it should not hide deterministic startup failures.

Code fixes / hardening:
1. Preflight forced skills before Popen or make the child startup failure propagate as `spawn_failed` with the exact stderr. A task should not show only `pid not alive` when the log says `Error: Unknown skill(s): X`.
2. Persist child exit classification more reliably. Current `_record_worker_exit()` / `_classify_worker_exit()` tries to classify clean/nonzero/signaled exits from an in-memory reap registry, but many observed events still collapsed to `unknown -> pid not alive`. Add a pid wrapper/sentinel file or keep Popen handles so nonzero/clean exits are durable and visible.
3. Treat clean worker exit without `kanban_complete`/`kanban_block` as `protocol_violation` with a clear event. Current code has this intended behavior (`kanban_db.py:3131-3177`), but observed records did not show `protocol_violation`, likely because classification fell back to unknown.
4. Ensure reclaim kills the stale worker process/group and records termination metadata. Evidence from t_f509f358/t_ae8debc5 shows stale workers kept running and commenting after reclaim. Current `kanban_db.py:2889-2939` has `_terminate_reclaimed_worker`; add regression tests confirming stale workers receive SIGTERM/SIGKILL and cannot continue mutating terminal lifecycle.
5. Improve `kanban_complete` rejection UX: include `task_status`, `current_run_id`, `expected_run_id`, and `last terminal event` in the tool error. Current generic `unknown id or already terminal` hides the real stale-run mismatch.
6. Add an operator-only recovery completion path: e.g. `kanban recover-complete <task> --from-comment/run`, or `kanban_complete(recovery=True)` that can promote blocked/gave_up tasks only with explicit operator context and audit event. This matches the real workflow where work was complete but terminal transition was missed.

Regression tests to add:
1. Unknown forced skill in spawned worker: dispatcher should record `spawn_failed` or `crashed` with stderr excerpt `Unknown skill(s): ...`, not just `pid not alive`; circuit breaker should include that exact reason.
2. Heartbeat extends claim TTL: claim at T0, heartbeat at T0+13m, dispatch at T0+16m must not reclaim.
3. Stale run completion: old run env expected_run_id=N after run N reclaimed and run N+1 claimed must reject with structured stale-run error, and must not change task status.
4. Reclaim terminates worker: a long-running child with worker_pid should be signaled when stale/manual reclaim occurs; event metadata should include termination attempt/result.
5. Clean exit no terminal transition: worker exits 0 while task remains running should become `protocol_violation`, not generic `pid not alive`.
6. Recovery completion path: blocked/gave_up task with verified handoff can be marked done via explicit recovery API, creating an auditable `recovered_completed` event and notification.

## Bottom line

The repeated issue was a lifecycle observability/recovery problem, not the external work itself. Some workers never really started because forced skills were unknown; some long workers were reclaimed because heartbeat did not extend TTL; stale workers then correctly failed run-scoped completion; and the two-failure breaker converted later recoverable work into blocked/gave_up cards. Comments preserved the results, but Kanban needs better startup preflight, heartbeat/claim tests, durable exit classification, stale-worker termination, clearer completion errors, and an explicit recovery-complete path.
