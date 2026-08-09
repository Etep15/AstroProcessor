# CodeWarrior skill archive

Active OpenClaw skills live directly under:

`/home/peter/.openclaw/workspace/agents/codewarrior/skills/<skill-name>/`

Historical copies are preserved beneath:

`/home/peter/.openclaw/workspace/agents/codewarrior/skills/ARCHIVE/versions/<archived-copy>/`

Do not place historical skill directories directly at `skills/ARCHIVE/<skill>/`.
OpenClaw supports a grouping level below a configured skill root, so keeping
archived SKILL.md files under `ARCHIVE/versions/` adds another level and keeps
them outside the intended active/grouped discovery layout.

Nothing in this archive should be treated as the live skill.

Future CodeWarrior skill installers should preserve replaced skill versions
directly into `skills/ARCHIVE/versions/` rather than leaving `.previous-*`,
`.before-*`, or `.failed-*` directories at the top level of `skills/`.
