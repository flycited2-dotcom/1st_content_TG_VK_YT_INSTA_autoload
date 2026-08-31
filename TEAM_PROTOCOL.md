# Codex–Claude team protocol

This file is the single source of truth for joint Codex and Claude Code work in this workspace.

## 1. Canonical workspace and bridge

- Canonical coordination workspace: `C:\Users\TLT-1\Documents\GitHub\Codex_Cluade_partners`.
- One user task uses one `bridge_session_id` and one absolute canonical `cwd` for its entire lifetime.
- Before relying on the bridge, call `bridge_status`. Stop if `defaultCwd`, `allowedRoots`, executable health, or authentication do not match the expected configuration.
- A project outside the current allowlist must be added as one explicit project path. Never broaden the allowlist to all of `Documents`, `Documents\GitHub`, or another collection of unrelated projects.
- Old coordination folders and their transcripts are legacy/read-only. Do not start new sessions there.

## 2. Roles

- Every task names one `lead` and one `reviewer`.
- Default for code, tests, and repository changes: Codex leads; Claude reviews.
- Default for product reasoning, content, UX, or desktop/browser work: Claude leads; Codex reviews.
- The lead owns implementation and final corrections. The reviewer independently checks requirements, risks, and evidence.
- A role handoff is recorded with `mirror_sync` and, for UI sessions, a `message_send` note.

## 3. Access and secrets

- `read-only` is the default.
- `workspace-write` is used only when the user asked for changes, only for the current explicit project, and only after the write preconditions below pass.
- `full-access` is prohibited. Do not set `CODEX_CLAUDE_BRIDGE_ALLOW_FULL_ACCESS`.
- Never copy API keys, passwords, tokens, `.env` values, or credentials between agents or into bridge messages/transcripts. Existing account authentication is preferred over shared secrets.
- Delegated children must not call the bridge or launch another Codex/Claude process; the bridge recursion guard remains enabled.

## 4. Preconditions for writing

- The target must be a Git repository. If it is not, do not use `workspace-write` until the user has authorized repository initialization or another rollback mechanism.
- Before edits, inspect `git status --short`. Preserve unrelated user changes and explicitly account for overlapping changes.
- Record file ownership for the current stage in `.codex-claude/state/OWNERSHIP.md` as `path | owner | stage | status`.
- Two agents must never edit the same file concurrently. To change ownership, the current owner finishes or checkpoints work, updates the manifest, synchronizes state, and notifies the peer.

## 5. Task lifecycle

1. Create or reuse exactly one bridge session for the task; bind it to the exact project `cwd`.
2. Call `mirror_sync` with `objective`, `status`, current `decisions`, and `next_actions`.
3. Both agents analyze independently. Use `cooperate` when `readyForDelegation=true`; otherwise use the degraded UI inbox procedure.
4. Reconcile proposals against explicit acceptance criteria. Assign lead, reviewer, and file ownership.
5. Lead implements. Reviewer does not edit lead-owned files during this stage.
6. Lead runs proportionate tests and records exact results. Claims such as “works” require command output or another inspectable artifact.
7. Reviewer performs an independent audit and reports concrete defects only.
8. Lead addresses valid findings, reruns relevant verification, and updates `mirror_sync`.
9. Send the peer a completion note and call `conversation_export`. Report the session ID, changed files, tests, remaining risks, and transcript path to the user.

## 6. Synchronization and conflicts

- `mirror_sync` is required before and after each material stage. No silent solo continuation after a handoff.
- Treat filesystem contents and Git state as authoritative; chat summaries are coordination aids, not proof of file state.
- If agents disagree, first test the disagreement against the acceptance criteria. If evidence does not resolve it, present both positions and tradeoffs to the user. Do not silently override the peer.
- Never claim the peer participated unless a successful peer call or direct message is present in the exported bridge transcript.

## 7. Operating modes

- Normal mode: `bridge_status.readyForDelegation=true`; prefer `cooperate` for a complete joint task.
- Degraded mode: if headless Claude is not authenticated, do not call `ask_claude`, `collaborate`, or `cooperate` from Codex. Exchange through `message_send`/`message_receive` between the two open UI sessions and export the conversation after every material exchange.
- Configuration changes take effect only after both clients restart. A fresh `bridge_status` from each client is required before declaring the new configuration active.
