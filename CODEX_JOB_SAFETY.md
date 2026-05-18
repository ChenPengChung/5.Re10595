# Codex SLURM Job Safety

This project shares the same HPC account with other simulation projects. Codex must keep job actions project-local.

## Prohibited

- Do not run bare `scancel <jobid>`.
- Do not run `scancel -u $USER`, broad filters, or batch cancellation.
- Do not use modifying `scontrol` actions: `update`, `hold`, `release`, `requeue`, `suspend`, `resume`.
- Do not cancel a job just because it appears in `squeue`.
- Do not remove or modify another project folder's `restart/`, locks, sentinels, or job state.

## Allowed

- Read-only inspection: `squeue`, `sinfo`, `sacct`, `scontrol show`.
- Reading, comparing, borrowing, or copying files/data from other project folders when useful.
- Project-local stop: `./run job-guard stop-chain`.
- Project-verified cancellation only: `./run job-guard scancel <jobid>`.

## Rule

If a job id is not recorded in this project state, treat it as another project's job. Do not cancel or modify it.
