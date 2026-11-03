# Windows Feature Parity and UI Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the Windows PowerShell/WPF edition to feature parity with the macOS edition where Windows supports an equivalent, preserve Windows-specific update and permission models, and ship a polished four-account UI in v1.7.0.

**Architecture:** Keep the existing single-file PowerShell core and WPF UI so installer/update delivery remains unchanged. Add Windows-native implementations using PowerShell, .NET, curl.exe and installed Chromium browsers; do not add third-party dependencies. Use streaming AES-256-CBC with PBKDF2-HMAC-SHA256 and encrypt-then-MAC for a Windows-specific `.csnap` snapshot format, not macOS DMG compatibility.

**Tech Stack:** Windows PowerShell 5.1+, WPF XAML, .NET Framework cryptography/compression, curl.exe, Python unittest static regressions, PowerShell isolated integration tests.

**Spec:** `AGENTS.md` plus the maintainer's 2026-10-29 instruction to complete Windows functionality against macOS, respect platform differences, improve the UI, and permit this release.

## Global Constraints

- All `.ps1` files remain UTF-8 with BOM and LF; no new third-party runtime dependency.
- Never weaken country consistency, proxy/TUN, Kill-Switch, bodyguard, account staging, or update SHA256 checks.
- Only the existing six optional checks may be skipped or downgraded.
- Never log or export proxy URLs/credentials, account data, passwords, public/private IPs, raw check details, `lastip`, or `iphistory` through client logs/diagnostics.
- Do not execute the installer/updater or touch the maintainer's installed app, accounts, system tasks, or proxy configuration.
- Windows Release update remains `check-update` / `update-now`; do not port macOS launchd or its updater.
- Windows snapshots use `.csnap` and are explicitly not compatible with macOS encrypted DMG files.
- Static tests on macOS must not be described as Windows hardware verification.

## Review Focus

- Malicious snapshot entries (`..`, rooted paths, links) must be rejected before extraction; wrong passwords/HMAC and account mismatches must leave no target snapshot.
- Pre-existing `.new`, `.previous`, `Claude.new`, `Claude.old`, or keep-stage content must never be overwritten or cleaned as if created by the current operation.
- Proxy diagnostics must not inherit ambient proxy variables, expose proxy credentials in argv/output, create account state, or fall back to direct connections.
- Keep paths must reject rooted/traversal/reparse-point paths; failed restore keeps the only staged copy and blocks Claude launch.
- UI must not allow account switching while checks fail or another operation is active; skipped checks stay gray and never become green passes.

---

### Task 1: Account, Client, and Check Parity

**Files:**
- Modify: `windows/claude-switch.ps1`
- Modify: `windows/claude-bodyguard.ps1`
- Modify: `windows/install.ps1`
- Test: `tests/test_windows_feature_parity_static.py`
- Test: `tests/windows-switch-integration.ps1`

**Interfaces:**
- Produces: `Invoke-Status` single-line A–D fields (`current`, `hasA`…`hasD`, `accounts`, `accA`…`accD`), `Get-Client`, `Set-ClientMode <mode>`, `Invoke-Check`, and keep staging/restoration functions consumed by `Invoke-Switch`.
- Supported clients: `clash-verge`, `flclash`, `sing-box`, `clashx`, `surge`, `wireguard`, `other`, `auto`.

- [ ] **Step 1: Write failing static and isolated integration tests** covering A–D status metadata, `check`, IP history only when enabled, keep path staging/restore/reparse rejection, client allowlists, and simultaneous core/`proxy-mode.txt` updates.
- [ ] **Step 2: Run the new tests and confirm the relevant assertions fail** with `python3 -m unittest tests.test_windows_feature_parity_static -v`.
- [ ] **Step 3: Implement A–D status and hard-fail `check`; add 48-hour unique-egress history without logging it to diagnostics.**
- [ ] **Step 4: Implement keep-in-workspace staging and merge restoration using reparse-point-safe relative paths and failure-preserving semantics in first enrollment, normal switch, and same-account retry.**
- [ ] **Step 5: Implement get/set-client and FlClash process detection in core, installer, and bodyguard; keep `auto`/`other` TUN-only behavior unchanged.**
- [ ] **Step 6: Run focused tests and the complete Python suite; run the PowerShell integration test only when a PowerShell runtime is available.**
- [ ] **Step 7: Commit this independently testable core slice.**

### Task 2: Read-Only Diagnostics and Safe Browser Tools

**Files:**
- Modify: `windows/claude-switch.ps1`
- Test: `tests/test_windows_tools_static.py`

**Interfaces:**
- Produces core commands `fingerprint`, `cli-info`, `open-safe <url>`, and `diagnose-proxy [--proxy URL|--config FILE]`.
- `diagnose-proxy` emits the same JSON schema/exit meanings as macOS: 0 reached, 1 transport failure, 2 configuration error.

- [ ] **Step 1: Write failing tests for command registration, Windows paths/APIs, browser isolation flags, scheme validation, cleanup, no-state diagnostic dispatch, sanitized JSON, explicit-proxy curl stdin, and timeout/TLS failure handling.**
- [ ] **Step 2: Run the focused tests and confirm they fail before implementation.**
- [ ] **Step 3: Implement read-only font/GPU/display/virtual-machine fingerprint output and Claude Code CLI installation/version/conflict reporting.**
- [ ] **Step 4: Implement one-shot Chromium profiles with proxy/TUN gating, `disable_non_proxied_udp`, safe Windows argument quoting, and a separate cleanup watcher.**
- [ ] **Step 5: Implement explicit HTTP/HTTPS/SOCKS5/SOCKS5H proxy diagnostics with clean child environment, credentials only on curl stdin, bounded timeouts, TLS/metric validation, and no private values in reports.**
- [ ] **Step 6: Run focused and complete static tests, then commit.**

### Task 3: Streaming Encrypted Snapshot Transfer

**Files:**
- Modify: `windows/claude-switch.ps1`
- Test: `tests/test_windows_snapshot_static.py`
- Test: `tests/windows-snapshot-integration.ps1`

**Interfaces:**
- Produces `snapshot-export <A|B|C|D> <file.csnap> [--password-stdin]` and `snapshot-import <file.csnap> <A|B|C|D> [--password-stdin]`.
- Format: magic/version header, 16-byte salt, 16-byte IV, 32-byte HMAC, ciphertext; 200,000-round PBKDF2-HMAC-SHA256, AES-256-CBC/PKCS7, HMAC-SHA256 over header and ciphertext.

- [ ] **Step 1: Write failing static tests and a PowerShell roundtrip/failure-matrix test for success, wrong password, tampering, short password, current-account export, existing target, account mismatch, and zip-slip entries.**
- [ ] **Step 2: Run available tests and confirm the new contracts are absent.**
- [ ] **Step 3: Implement password handling and streaming crypto without plaintext passwords in argv/logs or whole-snapshot in-memory reads.**
- [ ] **Step 4: Implement validated archive creation/import, metadata checks, entry traversal/link rejection, and staging that never overwrites existing snapshots.**
- [ ] **Step 5: Run static tests and the PowerShell integration test when available; commit.**

### Task 4: WPF Functional and Visual Completion

**Files:**
- Modify: `windows/SwitcherUI.ps1`
- Test: `tests/test_windows_ui_parity_static.py`

**Interfaces:**
- Consumes all core commands from Tasks 1–3.
- Provides four account cards with status/size/date, proxy selector, fingerprint/CLI/safe-link/region tools, encrypted snapshot dialogs, diagnostics, and update controls.

- [ ] **Step 1: Write failing XAML/static tests for unique controls, A–D cards, current/archived subtitles, proxy choices, tool commands, busy/disabled states, pre-switch audit gating, gray skip state, resizable layout, and password stdin handling.**
- [ ] **Step 2: Run the focused tests and confirm they fail.**
- [ ] **Step 3: Rebuild the existing dark WPF surface with shared styles, clear hierarchy, scalable/resizable layout, keyboard focus/tooltips, and consistent status/action feedback.**
- [ ] **Step 4: Move long check/fix/switch/tool operations to DispatcherTimer-polled jobs, prevent duplicate/conflicting operations and close-during-write, and require a complete passing audit before switching.**
- [ ] **Step 5: Wire four-account state, client switching, text tools, safe links, region refresh, diagnostics, and password-based snapshot export/import.**
- [ ] **Step 6: Parse all XAML, run focused and complete tests, perform one bounded source/UI defect pass, and commit.**

### Task 5: Documentation, Integrity, and Release Verification

**Files:**
- Modify: `windows/README.md`
- Modify: `README.md`
- Modify: `README.en.md` where corresponding Windows claims exist
- Modify: `AGENTS.md`
- Modify: `SHA256SUMS` via `scripts/gen-sums.sh` only if covered inputs changed
- Modify: `tests/windows-switch-integration.ps1` and `tests/windows-snapshot-integration.ps1`

- [ ] **Step 1: Update Windows command/UI documentation, platform differences, snapshot format/security/compatibility, and unverified-hardware disclaimers; remove obsolete missing-feature claims.**
- [ ] **Step 2: Run `python3 -B -m unittest discover -s tests -v`, shell syntax, Swift strict build, JavaScript syntax, BOM/LF checks, `git diff --check`, and checksum generation/verification as applicable.**
- [ ] **Step 3: Inspect the final diff and repository status; remove only artifacts created by this work.**
- [ ] **Step 4: Commit all implementation/docs/test changes, push `main`, and watch `checks.yml` to completion.**
- [ ] **Step 5: After green CI, tag `v1.7.0`, push the tag, watch `release.yml`, and verify the Release contains the Windows zip and `SHA256SUMS-release.txt`.**
- [ ] **Step 6: Report exact static/isolated/CI verification and explicitly state that Windows hardware UI, snapshot, browser, installer, and updater flows remain unverified without a Windows runtime.**
