#!/usr/bin/env python3
"""
nan_monitor.py — Login-node NaN / divergence watcher for GILBM solver.

Runs persistently on the login node. Tails the latest slurm log file for
this project and raises alerts on:
  - NaN / Inf in any solver field
  - Ma_max exceeding safety threshold (default 0.15)
  - Force sign reversal (sustained negative body force)
  - Solver crash (Segfault, CUDA error, OOM, etc.)
  - Job completion / chain continuation

Usage:
  python3 nan_monitor.py [project_dir]        # default: cwd
  python3 nan_monitor.py . --ma-limit 0.10    # tighter Ma threshold
  python3 nan_monitor.py . --tail-only        # just tail, no chain monitor
"""

import argparse
import glob
import os
import re
import sys
import time

# ── Thresholds ��─
DEFAULT_MA_LIMIT = 0.15
DEFAULT_POLL_SEC = 2.0
STALE_WARN_SEC = 600  # warn if no new output for 10 min

# ── Patterns ──
STEP_RE = re.compile(
    r'\[Step\s+(\d+)\s*\|\s*FTT=([\d.Ee+-]+)\]'
    r'.*Ub=([\d.Ee+-]+)'
    r'.*Force=([\d.Ee+-]+)'
    r'.*Re=([\d.Ee+-]+)'
    r'.*Ma=([\d.Ee+-]+)'
    r'.*Ma_max=([\d.Ee+-]+)'
    r'.*Error=([\d.Ee+-]+)'
)

CRASH_RE = re.compile(
    r'NaN|nan|inf(?!o)|Inf(?!o)'
    r'|Segmentation fault|SIGSEGV|bus error|illegal instruction'
    r'|CUDA error|cudaError|out of memory|OOM'
    r'|FATAL|ABORT|abort\(\)'
    r'|MPI_ABORT|mpi_abort'
    r'|Killed\b',
    re.IGNORECASE
)

MPIRUN_EXIT_RE = re.compile(r'mpirun exit.*RC=(\d+)')

JOB_START_RE = re.compile(r'Chain\s+(\d+)\s+round\s+(\d+)')

CHECKPOINT_RE = re.compile(r'Restart from:\s+(\S+)')


def find_latest_log(project_dir):
    logs = glob.glob(os.path.join(project_dir, 'slurm_*.log'))
    if not logs:
        logs = glob.glob(os.path.join(project_dir, 'slurm-*.out'))
    if not logs:
        return None
    return max(logs, key=os.path.getmtime)


def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def alert(level, msg):
    print(f'[{ts()}] [{level}] {msg}', flush=True)


def tail_follow(filepath, poll_sec=0.5):
    with open(filepath, 'r') as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                yield line.rstrip('\n')
            else:
                time.sleep(poll_sec)
                if not os.path.exists(filepath):
                    return


class SolverState:
    def __init__(self, ma_limit):
        self.ma_limit = ma_limit
        self.last_step = 0
        self.last_ftt = 0.0
        self.last_update = time.time()
        self.neg_force_count = 0
        self.total_steps_seen = 0
        self.alerts_fired = 0
        self.current_job = None
        self.current_round = None
        self.checkpoint_path = None

    def process_line(self, line):
        # Crash patterns
        if CRASH_RE.search(line):
            if 'nan_monitor' in line.lower():
                return
            alert('CRITICAL', f'Crash/NaN detected: {line}')
            self.alerts_fired += 1
            return

        # mpirun exit
        m = MPIRUN_EXIT_RE.search(line)
        if m:
            rc = int(m.group(1))
            if rc == 124:
                alert('INFO', f'Job ended normally (RC=124 SIGUSR1 chain)')
            elif rc == 0:
                alert('INFO', f'Job ended (RC=0)')
            else:
                alert('WARNING', f'mpirun exit with RC={rc}')
            return

        # Job start
        m = JOB_START_RE.search(line)
        if m:
            self.current_job = m.group(1)
            self.current_round = m.group(2)
            alert('INFO', f'Job {self.current_job} round {self.current_round} started')
            return

        # Checkpoint loaded
        m = CHECKPOINT_RE.search(line)
        if m:
            self.checkpoint_path = m.group(1)
            alert('INFO', f'Restart from: {self.checkpoint_path}')
            return

        # Step line
        m = STEP_RE.search(line)
        if not m:
            return

        step = int(m.group(1))
        ftt = float(m.group(2))
        ub = float(m.group(3))
        force = float(m.group(4))
        re_eff = float(m.group(5))
        ma = float(m.group(6))
        ma_max = float(m.group(7))
        error = float(m.group(8))

        self.last_step = step
        self.last_ftt = ftt
        self.last_update = time.time()
        self.total_steps_seen += 1

        import math
        if math.isnan(ma_max) or math.isnan(ub) or math.isnan(force):
            alert('CRITICAL', f'NaN in solver fields at step {step}')
            self.alerts_fired += 1
            return

        if ma_max > self.ma_limit:
            alert('WARNING', f'Ma_max={ma_max:.4f} > {self.ma_limit} at step {step} (compressibility risk)')
            self.alerts_fired += 1

        if force < 0:
            self.neg_force_count += 1
            if self.neg_force_count >= 20:
                if self.neg_force_count == 20:
                    alert('WARNING', f'Force negative for 20+ reports at step {step} (Force={force:.6e})')
                    self.alerts_fired += 1
        else:
            self.neg_force_count = 0


def main():
    parser = argparse.ArgumentParser(description='GILBM NaN/divergence monitor')
    parser.add_argument('project_dir', nargs='?', default='.')
    parser.add_argument('--ma-limit', type=float, default=DEFAULT_MA_LIMIT)
    parser.add_argument('--poll', type=float, default=DEFAULT_POLL_SEC)
    parser.add_argument('--tail-only', action='store_true')
    args = parser.parse_args()

    project = os.path.abspath(args.project_dir)
    alert('INFO', f'nan_monitor started for {project}')
    alert('INFO', f'Ma_max limit={args.ma_limit}, poll={args.poll}s')

    state = SolverState(args.ma_limit)
    current_log = None

    while True:
        log_file = find_latest_log(project)

        if log_file is None:
            alert('INFO', 'No slurm log found yet, waiting...')
            time.sleep(10)
            continue

        if log_file != current_log:
            current_log = log_file
            alert('INFO', f'Tailing: {os.path.basename(log_file)}')

        try:
            for line in tail_follow(log_file, args.poll):
                state.process_line(line)

                elapsed = time.time() - state.last_update
                if state.total_steps_seen > 0 and elapsed > STALE_WARN_SEC:
                    alert('WARNING', f'No solver step output for {int(elapsed)}s (last step={state.last_step})')
                    state.last_update = time.time()

        except KeyboardInterrupt:
            alert('INFO', f'Stopped. Total steps monitored: {state.total_steps_seen}, alerts: {state.alerts_fired}')
            sys.exit(0)

        alert('INFO', f'Log file ended/rotated, scanning for new log...')
        time.sleep(5)


if __name__ == '__main__':
    main()
