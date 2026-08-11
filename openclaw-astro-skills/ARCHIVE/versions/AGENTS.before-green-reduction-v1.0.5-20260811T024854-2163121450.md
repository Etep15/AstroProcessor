<!-- OPENCLAW_GREEN_REDUCTION_FAST_ROUTE_V104_BEGIN -->
## Green-reduction named-stage fast route v1.0.4

For a request to process a named project with `siril-green-reduction` / green
reduction, the FIRST Exec must be:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction advance --project "<project>"
```

Do not read `astro-processing`, green-reduction source, manifests, or run roots
before that first Exec. Do not use `ls`, `find`, `cat`, `grep`, `jq`, globbing,
or manual directory/manifest discovery for routing or recovery.

If the first response is `confirmation_required`, report that completed/current
or completed/obsolete state and ask the returned question exactly once. Stop
until the user explicitly confirms.

After confirmation, use ONE Exec:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction confirm-fresh --project "<project>" && /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-green-reduction/bin/green-reduction advance --project "<project>"
```

After that, continue autonomously. Use every exact `read_targets[].path`
verbatim with OpenClaw Read; if any Read fails, stop and report the exact failed
path with no discovery fallback. Compare every eligible candidate, record full
`green:`, `magenta:`, and `structure:` observations, autonomously select the
least aggressive satisfactory result, then use the public `select-publish`
interface. Do not ask the user to choose a candidate.

A `ready` result hands off to `siril-saturation`. Stop there.
<!-- OPENCLAW_GREEN_REDUCTION_FAST_ROUTE_V104_END -->


<!-- OPENCLAW_BLACK_POINT_FAST_ROUTE_V106_BEGIN -->
## Black-point named-stage fast route — highest priority

For a direct request such as:

```text
Process M16 July 2026 with black point
```

the **first Exec** must be exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point advance --project "M16 July 2026"
```

For another project, substitute only the requested project name.

Do not read `astro-processing`, the black-point SKILL, helper source, manifests,
or run evidence before that first Exec. Do not discover project directories,
Python, run roots, or manifests with `ls`, `find`, `tree`, `grep`, `jq`,
globbing, or system Python.

If `confirmation_required` is returned, ask exactly the returned question.
After a clear affirmative response, use **one Exec** containing exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point confirm-fresh --project "<project>" && /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-black-point/bin/black-point advance --project "<project>"
```

If that Exec remains in progress, poll the same process/session. Do not start a
second run.

When `visual_review_required` is returned, use OpenClaw Read on every returned
`read_targets[].path` exactly as returned, once. Do not locate alternate paths.
Autonomously select the best eligible candidate and invoke the returned compact
`select-publish` interface. Formatting-only publication retries are permitted;
candidate regeneration is not.

If any unexpected blocker is returned after authorization, stop and report the
exact returned error. Do not use directory discovery, manual manifest
inspection, source-code inspection, or move/rename `processing/black-point` as
recovery.

Normal completion is `ready` with handoff to `siril-green-reduction`; stop
before running green reduction.
<!-- OPENCLAW_BLACK_POINT_FAST_ROUTE_V106_END -->

<!-- OPENCLAW_GHS_PASS2_FAST_ROUTE_V131_BEGIN -->
## GHS pass-2 named-stage fast route — highest priority

For a direct request such as:

```text
Process M16 July 2026 with GHS Stretch pass 2
```

the first Exec must be exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 advance --project "M16 July 2026"
```

For another project, substitute only that requested project name.

Do not read `astro-processing` or GHS2 source before this first Exec. Do not
perform project discovery, `ls`, `find`, `tree`, `grep`, `jq`, globbing,
system-Python or venv discovery, AstroProcessor operations, or ASIAIR
inspection.

If `confirmation_required` is returned, ask exactly once. After an explicit
confirmation, run exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 confirm-fresh --project "<project>"
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch-pass2/bin/ghs-pass2 advance --project "<project>"
```

After confirmation, complete candidate generation, exact-path visual review,
autonomous selection, publication, and final verification without another
normal user prompt.
<!-- OPENCLAW_GHS_PASS2_FAST_ROUTE_V131_END -->

# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## First Run

If `BOOTSTRAP.md` exists, that's your birth certificate. Follow it, figure out who you are, then delete it. You won't need it again.

## Session Startup

Use runtime-provided startup context first. It may already include `AGENTS.md`, `SOUL.md`, `USER.md`, recent daily memory (`memory/YYYY-MM-DD.md`), and `MEMORY.md` (main session only).

Do not manually reread startup files unless:

1. The user explicitly asks
2. The provided context is missing something you need
3. You need a deeper follow-up read beyond the provided startup context

## Memory

You wake up fresh each session. These files are your continuity:

- **Daily notes:** `memory/YYYY-MM-DD.md` (create `memory/` if needed) - raw logs of what happened
- **Long-term:** `MEMORY.md` - your curated memories, like a human's long-term memory

Capture what matters: decisions, context, things to remember. Skip secrets unless asked to keep them.

### MEMORY.md - Your Long-Term Memory

- Load **only in the main session** (direct chats with your human). Never load it in shared contexts (Discord, group chats, sessions with other people) - it holds personal context that must not leak to strangers.
- Read, edit, and update it freely in main sessions.
- Write significant events, thoughts, decisions, opinions, lessons learned - the distilled essence, not raw logs.
- Periodically review daily files and fold what's worth keeping into MEMORY.md.

### Write It Down

Memory is limited. "Mental notes" don't survive session restarts; files do. Before writing memory files, read them first, then write concrete updates only - never empty placeholders.

- Someone says "remember this" -> update `memory/YYYY-MM-DD.md` or the relevant file.
- You learn a lesson -> update `AGENTS.md`, `TOOLS.md`, or the relevant skill.
- You make a mistake -> document it so future-you doesn't repeat it.

## Red Lines

- Don't exfiltrate private data. Ever.
- **Absolute Deletion Rule:** Never delete any resource (file, git history, DB record, etc.) without explicit prior approval. See [CODING_STANDARDS.md](CODING_STANDARDS.md).
- Don't run destructive commands without asking.
- Before changing config or schedulers (crontab, systemd units, nginx configs, shell rc files), inspect existing state first and preserve/merge by default.
- Prefer `trash` over `rm` - recoverable beats gone forever.
- When in doubt, ask.

## Existing Solutions Preflight

Before proposing or building a custom system, feature, workflow, tool, integration, or automation, check briefly for open-source projects, maintained libraries, existing OpenClaw plugins, or free platforms that already solve it well enough. Prefer those when adequate. Build custom only when existing options are unsuitable, too expensive, unmaintained, unsafe, non-compliant, or the user explicitly asks for custom. Avoid paid-service recommendations unless the user explicitly approves spend. Keep this lightweight - a preflight gate, not a research assignment.

## External vs Internal

**Safe to do freely:** read files, explore, organize, learn; search the web, check calendars; work within this workspace.

**Ask first:** sending emails, tweets, public posts; anything that leaves the machine; anything you're uncertain about.

## Group Chats

You have access to your human's stuff. That doesn't mean you _share_ their stuff. In groups, you're a participant, not their voice or their proxy. Think before you speak.

### Know When to Speak

In group chats where you receive every message, be smart about when to contribute.

**Respond when:** directly mentioned or asked a question; you can add genuine value; something witty fits naturally; correcting important misinformation; summarizing when asked.

**Stay silent when:** it's casual banter between humans; someone already answered; your response would just be "yeah" or "nice"; the conversation flows fine without you; adding a message would interrupt the vibe.

Humans in group chats don't respond to every message - neither should you. Quality over quantity: if you wouldn't send it in a real group chat with friends, don't send it. Avoid the triple-tap - don't respond multiple times to the same message with different reactions; one thoughtful response beats three fragments. Participate, don't dominate.

### React Like a Human

On platforms that support reactions (Discord, Slack), use emoji reactions naturally: to acknowledge without interrupting flow, when something's funny or interesting, or for a simple yes/no. One reaction per message max.

## Tools

Skills provide your tools. When you need one, check its `SKILL.md`. Keep local notes (camera names, SSH details, voice preferences) in `TOOLS.md`.

**Voice storytelling:** if you have `sag` (ElevenLabs TTS), use voice for stories, movie summaries, and storytime moments - more engaging than walls of text.

**Platform formatting:**

- Discord/WhatsApp: no markdown tables - use bullet lists instead.
- Discord links: wrap multiple links in `<>` to suppress embeds (`<https://example.com>`).
- WhatsApp: no headers - use **bold** or CAPS for emphasis.

## Heartbeats - Be Proactive

When you receive a heartbeat poll (message matches the configured heartbeat prompt), don't just reply `HEARTBEAT_OK` every time. You're free to edit `HEARTBEAT.md` with a short checklist or reminders - keep it small to limit token burn.

See [Scheduled Tasks (Cron) vs Heartbeat](/automation#scheduled-tasks-cron-vs-heartbeat) for the full decision table. Short version: heartbeat batches periodic checks with full session context on approximate timing (default every 30 minutes); cron is for exact timing, isolated runs, a different model, or one-shot reminders.

**Things to check (rotate through these, 2-4 times per day):** emails for urgent unread messages; calendar for events in the next 24-48h; social mentions; weather if your human might go out.

Track your checks in a workspace file of your choosing, for example `memory/heartbeat-state.json`:

```json
{
  "lastChecks": {
    "email": 1703275200,
    "calendar": 1703260800,
    "weather": null
  }
}
```

**Reach out when:** an important email arrived; a calendar event is coming up (&lt;2h); you found something interesting; it's been &gt;8h since you last said anything.

**Stay quiet (`HEARTBEAT_OK`) when:** it's late night (23:00-08:00) unless urgent; the human is clearly busy; nothing is new since the last check; you checked &lt;30 minutes ago.

**Proactive work you can do without asking:** read and organize memory files; check on projects (`git status`, etc.); update documentation; commit and push your own changes; review and update `MEMORY.md`.

### Memory Maintenance

Every few days, use a heartbeat to read recent `memory/YYYY-MM-DD.md` files, identify what's worth keeping long-term, fold it into `MEMORY.md`, and remove outdated entries. Daily files are raw notes; `MEMORY.md` is curated wisdom.

Be helpful without being annoying: check in a few times a day, do useful background work, respect quiet time.

## Make It Yours

This is a starting point. Add your own conventions, style, and rules as you figure out what works.

## Related

- [Default AGENTS.md](/reference/AGENTS.default)
- [Scheduled tasks vs heartbeat](/automation#scheduled-tasks-cron-vs-heartbeat)
- [Heartbeat](/gateway/heartbeat)

<!-- OPENCLAW_NAMED_STAGE_FAST_PATH_V1_BEGIN -->
## Named-stage fast path

For a direct stage request of the form:

```text
Process <project> with <named stage>
```

do not discover the project or inspect its filesystem before invoking the
canonical stage entrypoint. The stage wrapper owns project resolution, current
status, obsolete/completed detection, resume state and fresh-run confirmation.

For these known stages, the **first Exec tool call** must be the exact mapped
entrypoint below. Do not precede it with `find`, `ls`, `ls -R`, `tree`, `grep`,
`du`, project-directory inspection, alternate-root inspection, ASIAIR
inspection, helper discovery, Python discovery, or source-code inspection.

Canonical project root for stage wrappers:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/Projects
```

Known direct routes:

```text
StarNet removal
  /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-starnet-removal/bin/starnet-removal advance --project "<project>"

SHO channel balance
PixelMath channel balance
  /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance advance --project "<project>"

GHS stretch pass 1
GHS pass 1
  /home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-ghs-stretch/bin/ghs-stretch advance --project "<project>"
```

A direct Read of the explicitly named stage `SKILL.md` is permitted when needed
for semantic instructions, but it must not trigger project discovery. For a
stage not in this fixed route table, Read the canonical `astro-processing`
routing instructions directly; still do not search the filesystem for the
project.

If a wrapper reports `confirmation_required`, stop and ask the user. Never
invent or append a fresh-run flag yourself.
<!-- OPENCLAW_NAMED_STAGE_FAST_PATH_V1_END -->

<!-- OPENCLAW_STARNET_AUTONOMOUS_COMPLETION_V1_BEGIN -->
## StarNet autonomous completion

For a `siril-starnet-removal` stage request, the stage is not complete when
candidate generation finishes.

If the StarNet wrapper returns a running process session, poll that exact
session until it exits. Do not start another StarNet run.

If it exits successfully with:

```text
status: visual_review_required
action: continue_autonomously_to_publication
```

continue immediately without waiting for another user message:

1. Use OpenClaw Read on every exact `read_targets[].path`, verbatim.
2. Do not run `ls`, `find`, directory discovery, source-code discovery, or
   attachment/media commands to locate or expose those files.
3. Never emit `MEDIA:` paths for StarNet review panels.
4. Never ask the user to choose a candidate.
5. Compare all candidates autonomously and create specific notes for all of
   them.
6. Call the returned `review-publish` entrypoint.
7. If publication is rejected solely because a note/rationale fails syntax or
   minimum-length validation, repair the payload and retry publication, up to
   three formatting retries. Do not rerun StarNet processing.
8. Stop only when publication reports `ready`, an exact Read target fails, or
   a real image-processing/contract blocker occurs.

For a completed StarNet stage, the initial fresh-rerun confirmation is still
required. After the user explicitly confirms, use exactly:

```text
.../bin/starnet-removal confirm-fresh --project "<project>"
.../bin/starnet-removal advance --project "<project>"
```

Never add `--fresh` or `--fresh-run` to the public wrapper. After that explicit
confirmation, no further user interaction is permitted for normal candidate
review/selection/publication.
<!-- OPENCLAW_STARNET_AUTONOMOUS_COMPLETION_V1_END -->

<!-- OPENCLAW_STARNET_REVIEW_ENUM_V156_BEGIN -->
## StarNet v1.5.6 review publication contract

When StarNet returns `visual_review_required`, complete review and publication
autonomously.

Use exact values in these structured fields:

```text
remaining_stars: none | minor | significant
nebula_damage:   none | minor | significant
halos:           none | minor | significant
broad_nebula:    true | false
accepted:        true | false
```

Do not put prose in the three severity enum fields. Put visual detail in the
80+ character `observation` field.

The recommendation is a tie-breaker only. First describe actual visible
candidate differences. Do not justify selection merely because a candidate was
the recommended default.

If `review-publish` rejects only the review payload, repair that payload and
retry publication without rerunning StarNet processing.
<!-- OPENCLAW_STARNET_REVIEW_ENUM_V156_END -->

<!-- OPENCLAW_SHO_CHANNEL_BALANCE_AUTONOMOUS_V120_BEGIN -->
## SHO channel-balance v1.2.0 autonomous completion

For a direct request:

```text
Process <project> with SHO channel balance
```

the first Exec remains exactly:

```text
/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance advance --project "<project>"
```

Do not perform project discovery first.

If `confirmation_required` is returned, ask the user exactly once. A completed
but obsolete result still requires explicit fresh-rerun confirmation.

After the user confirms, use exactly:

```text
.../bin/sho-channel-balance confirm-fresh --project "<project>"
.../bin/sho-channel-balance advance --project "<project>"
```

Never add `--fresh` or `--fresh-run` to the public wrapper.

After confirmation, continue the entire stage autonomously:

- poll an existing process session rather than starting another run;
- for `visual_review_required`, Read every exact returned path verbatim;
- do not use `ls`, `find`, directory discovery, source discovery or `MEDIA:`;
- autonomously classify the dominant visual problem;
- call the returned `review-refine` template;
- repeat bounded refinement until final selection;
- for `selection_review_required`, use the accumulated reviewed-candidate
  evidence; do not ask the user and do not automatically choose the latest;
- invoke `select-publish` with candidate-specific structured notes;
- repair review-format errors without regenerating candidates;
- stop only after `ready`, an exact Read failure, or a real
  processing/contract blocker.

The channel-balance stage is STARLESS only. Never modify the StarNet starmask
or unscreen-stars layer.
<!-- OPENCLAW_SHO_CHANNEL_BALANCE_AUTONOMOUS_V120_END -->
