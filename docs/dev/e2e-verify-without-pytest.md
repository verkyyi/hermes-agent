# Standard workflow: e2e-verify a gateway change with the real harness (no pytest)

After a gateway behavior change lands on `verky/deploy`, verify it **end-to-end
against the real gateway machinery** — not just with unit tests. This catches
wiring bugs that mocked unit tests miss, while staying safe (no live reboot, no
real messages).

This is a **standard step for dev jobs that change gateway behavior**, alongside
(not instead of) the pytest suite under `tests/local/`.

## The recipe

Write a standalone script under `scripts/e2e/` that:

1. **Isolates state.** Point the gateway at a throwaway home so you never touch
   `~/.hermes`:
   ```python
   import gateway.run as gateway_run
   gateway_run._hermes_home = Path(tempfile.mkdtemp(prefix="hermes-e2e-"))
   ```
2. **Boots the real pipeline** via the existing e2e harness helpers (importable
   without pytest):
   ```python
   from tests.e2e.conftest import make_runner, make_adapter, send_and_capture
   ```
   `make_adapter` builds a **real** platform adapter (the platform client lib is
   mocked at import, so no network) wired to the **real**
   `GatewayRunner._handle_message`. The method(s) under test are real bound
   methods — only `adapter.send()` is recorded and the truly destructive edges
   (`request_restart`, the live reboot) are stubbed.
3. **Drives the real flow** end to end. For multi-process contracts (e.g. the
   `/restart` marker handoff), simulate the reboot by constructing a *fresh*
   runner that reads the same isolated home — that file is the cross-process
   contract, so this faithfully reproduces the boundary.
4. **Asserts on recorded behavior** and prints `PASS`/`FAIL` per phase, exiting
   non-zero on any failure (so it's CI- and `/loop`-friendly).

## Don'ts

- **Don't** `hermes gateway restart` the live launchd service to "verify" — that
  reboots the user's production agent and can fire a real message. Simulate the
  reboot in-process instead.
- **Don't** send through real platform credentials. The harness records
  `send()`; keep it that way.
- **Don't** run against `~/.hermes`. Always redirect `_hermes_home` to a tempdir.

## Run it

```bash
cd /Users/verkyyi/.hermes/hermes-agent
venv/bin/python -m scripts.e2e.<your_check> -v   # 0 = PASS, 1 = FAIL
```

> **Use `-m` from the repo root.** This project is an editable install, so
> `python scripts/e2e/foo.py` sets `sys.path[0]` to the *script's* directory and
> resolves `import gateway` via the install — which can silently be a **different
> checkout** than your cwd (e.g. when you're in a git worktree, it loads the
> primary checkout, not your edits). `python -m` from the repo root puts the cwd
> on the path so you test the code you're actually standing in. Confirm a new
> check is a real guard, not a tautology, by temporarily breaking the behavior
> and seeing it go red.

## Reference example

`scripts/e2e/verify_restart_comeback.py` — proves that the channel which runs
`/restart` always gets the "gateway restarted" reply (even with
`gateway_restart_notification=false`), while a non-initiating home channel stays
silent. It exercises the real `/restart` handler, the real `.restart_notify.json`
handoff, and the real `_send_restart_notification` / home-channel-broadcast paths.

Related: deterministic policy/responsiveness e2e lives under
`evals/responsiveness/` (`python -m evals.responsiveness.run`); pytest e2e
fixtures live in `tests/e2e/`.
