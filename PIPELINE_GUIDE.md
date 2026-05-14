# Regrid Pipeline Guide

Canonical regrid pipeline guidance now lives in `Markdown/PIPELINE_GUIDE.md`.

When a task touches `phase1_generategrid/` or `phase2_generatecheckpoint/`,
read `Markdown/PIPELINE_GUIDE.md` first and drive the workflow through
`./run.sh` or `./run`. Do not call `phase2_generatecheckpoint/interp_checkpoint.py`
directly for production submission.
