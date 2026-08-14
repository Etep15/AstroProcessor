# astro-processing v2.0.0

Target-agnostic thin controller for the complete narrowband SHO workflow.

It keeps AstroProcessor setup and orchestration state/reporting at the top level while every image-processing stage remains owned by its installed child skill.

Typical request:

    Fully process project "Target" from /mnt/asiair/emmc in autorun.

When the ASIAIR folder name differs:

    Fully process project "Destination" from /mnt/asiair/emmc in autorun using source project "Exact ASIAIR Name".

