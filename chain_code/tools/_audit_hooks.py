#!/usr/bin/env python3
"""Comprehensive audit of all SLURM guard layers."""
import subprocess, json, sys, os

GLOBAL  = '/home/s8313697/.claude/hooks/scancel_guard.sh'
PROJECT = '/home/s8313697/5.Re10595/Edit2_restart/chain_code/tools/claude_slurm_guard.sh'
CODEGUARD = '/home/s8313697/5.Re10595/Edit2_restart/chain_code/tools/project_job_guard.sh'

def run_hook(script, cmd, cwd=None):
    inp = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': cmd}})
    r = subprocess.run(['bash', script], input=inp, capture_output=True, text=True,
                       timeout=10, cwd=cwd)
    return 'BLOCK' if r.returncode != 0 else 'ALLOW', r.stderr.strip()

def run_guard(subcmd, args, cwd=None):
    r = subprocess.run(['bash', CODEGUARD] + [subcmd] + args,
                       capture_output=True, text=True, timeout=10,
                       cwd=cwd or '/home/s8313697/5.Re10595/Edit2_restart')
    return 'BLOCK' if r.returncode != 0 else 'ALLOW', r.stdout.strip() + r.stderr.strip()

MY_JOBID = '35744'  # current project job

# ═════════════════════════════════════════════════════════���═════
# Test matrix: (name, command, expect_global, expect_project)
#
# Global hook: blocks cross-project scancel (WorkDir verify),
#              blocks modifying scontrol, blocks write to other projects
# Project hook: forces scancel through job-guard, blocks modifying scontrol
# ═══════════════════════════════════════════════════════════════
tests = [
    # --- SCANCEL: must block (various forms) ---
    ('sc bare (other job)',         f'sc'+'ancel 35738',                       'BLOCK', 'BLOCK'),
    ('sc -u (kill all)',            'sc'+'ancel -u s8313697',                  'BLOCK', 'BLOCK'),
    ('sc --name (by name)',         'sc'+'ancel --name=Edit2_restart',         'BLOCK', 'BLOCK'),
    ('sc --partition (by part)',    'sc'+'ancel --partition=dev',              'BLOCK', 'BLOCK'),
    ('sc bare (own job)',           f'sc'+'ancel {MY_JOBID}',                 'ALLOW', 'BLOCK'),
    #   ^ Global ALLOW because WorkDir matches; Project BLOCK because not via job-guard

    # --- SCANCEL: compound/tricky ---
    ('pipe xargs sc',              'squeue -h | awk \'{print $1}\' | xargs sc'+'ancel', 'BLOCK', 'BLOCK'),
    ('$() sc',                     'sc'+'ancel $(cat restart/chain_jobid)',    'BLOCK', 'BLOCK'),

    # --- SCONTROL: must block modification ---
    ('scontrol hold',              'scontrol hold 35744',                     'BLOCK', 'BLOCK'),
    ('scontrol release',           'scontrol release 35744',                  'BLOCK', 'BLOCK'),
    ('scontrol update',            'scontrol update JobId=35744 TimeLimit=5:00:00', 'BLOCK', 'BLOCK'),
    ('scontrol suspend',           'scontrol suspend 35744',                  'BLOCK', 'BLOCK'),
    ('scontrol resume',            'scontrol resume 35744',                   'BLOCK', 'BLOCK'),
    ('scontrol requeue',           'scontrol requeue 35744',                  'BLOCK', 'BLOCK'),

    # --- SCONTROL: compound (the bug we found) ---
    ('show ; hold (compound)',     'scontrol show job 123; scontrol hold 456','BLOCK', 'BLOCK'),
    ('show && hold (compound)',    'scontrol show job 123 && scontrol hold 456', 'BLOCK', 'BLOCK'),
    ('show | update (pipe)',       'scontrol show job 123 | grep -v x; scontrol update JobId=123 TimeLimit=2:00:00', 'BLOCK', 'BLOCK'),

    # --- SCONTROL/SLURM: read-only MUST ALLOW ---
    ('scontrol show job',          'scontrol show job 35744',                 'ALLOW', 'ALLOW'),
    ('scontrol show hostnames',    'scontrol show hostnames node[01-04]',     'ALLOW', 'ALLOW'),
    ('scontrol listpids',          'scontrol listpids 35744',                 'ALLOW', 'ALLOW'),
    ('squeue',                     'squeue -u s8313697',                      'ALLOW', 'ALLOW'),
    ('squeue all',                 'squeue',                                  'ALLOW', 'ALLOW'),
    ('sinfo',                      'sinfo -p dev',                            'ALLOW', 'ALLOW'),
    ('sacct',                      'sacct -j 35744 --format=JobID,State',     'ALLOW', 'ALLOW'),

    # --- JOB-GUARD PATH: must allow ---
    ('job-guard sc',               f'./run job-guard sc'+'ancel {MY_JOBID}',  'ALLOW', 'ALLOW'),
    ('guard.sh sc',                f'bash chain_code/tools/project_job_guard.sh sc'+'ancel {MY_JOBID}', 'ALLOW', 'ALLOW'),
    ('job-guard stop-chain',       './run job-guard stop-chain',              'ALLOW', 'ALLOW'),
    ('job-guard list',             './run job-guard list',                    'ALLOW', 'ALLOW'),

    # --- CROSS-PROJECT READ: must allow ---
    ('read other proj file',       'cat /home/s8313697/Channel_600/variables.h', 'ALLOW', 'ALLOW'),
    ('ls other proj',              'ls /home/s8313697/3.Re5600/',             'ALLOW', 'ALLOW'),
    ('grep other proj',            'grep -n "define NX" /home/s8313697/3.Re5600/variables.h', 'ALLOW', 'ALLOW'),
    ('diff with other proj',       'diff variables.h /home/s8313697/3.Re5600/variables.h', 'ALLOW', 'ALLOW'),
    ('cp FROM other proj',         'cp /home/s8313697/3.Re5600/result/data.bin .', 'ALLOW', 'ALLOW'),

    # --- NORMAL OPS: must allow ---
    ('ls',                         'ls -la',                                  'ALLOW', 'ALLOW'),
    ('./run build',                './run build H200',                        'ALLOW', 'ALLOW'),
    ('./run submit',               './run',                                   'ALLOW', 'ALLOW'),
    ('python',                     'python3 script.py',                       'ALLOW', 'ALLOW'),
    ('git status',                 'git status',                              'ALLOW', 'ALLOW'),
    ('nvcc compile',               'nvcc -o a.out main.cu',                   'ALLOW', 'ALLOW'),

    # --- KILL PROTECTION (global only) ---
    ('pkill mpirun',               'pkill mpirun',                            'BLOCK', 'ALLOW'),
    ('killall a.out',              'killall a.out',                           'BLOCK', 'ALLOW'),
    #   ^ Project hook doesn't cover kill; global hook does
]

def audit(label, script, col):
    print(f"\n{'='*65}")
    print(f" {label}")
    print(f"{'='*65}")
    passed = failed = 0
    failures = []
    for name, cmd, *expects in tests:
        expect = expects[col]
        status, err = run_hook(script, cmd)
        ok = status == expect
        if ok:
            passed += 1
            tag = 'OK'
        else:
            failed += 1
            failures.append((name, cmd, expect, status, err))
            tag = 'FAIL'
        print(f"  [{tag:4s}] {name:30s}  {status:5s} (expect {expect})")
        if not ok and err:
            print(f"          err: {err[:100]}")
    print(f"\n  Score: {passed}/{passed+failed}")
    return failures

g_fail = audit("GLOBAL HOOK (scancel_guard.sh)", GLOBAL, 0)
p_fail = audit("PROJECT HOOK (claude_slurm_guard.sh)", PROJECT, 1)

# --- Code-level guard audit ---
print(f"\n{'='*65}")
print(" CODE GUARD (project_job_guard.sh)")
print(f"{'='*65}")
code_tests = [
    ('check own job',    'check', [MY_JOBID],    'ALLOW'),
    ('check other job',  'check', ['35738'],      'BLOCK'),
    ('check garbage',    'check', ['abc'],        'BLOCK'),
    ('list',             'list',  [],             'ALLOW'),
    ('stop-chain',       'stop-chain', [],        'ALLOW'),
]
c_fail = []
for name, sub, args, expect in code_tests:
    status, out = run_guard(sub, args)
    ok = status == expect
    tag = 'OK' if ok else 'FAIL'
    print(f"  [{tag:4s}] {name:30s}  {status:5s} (expect {expect})")
    if not ok:
        c_fail.append((name, sub, args, expect, status))
        print(f"          out: {out[:100]}")

# Clean up stop-chain sentinel if created
os.remove('/home/s8313697/5.Re10595/Edit2_restart/restart/STOP_CHAIN') if os.path.exists('/home/s8313697/5.Re10595/Edit2_restart/restart/STOP_CHAIN') else None

print(f"\n{'='*65}")
total_fail = len(g_fail) + len(p_fail) + len(c_fail)
if total_fail == 0:
    print(" ALL TESTS PASSED — three-layer protection verified")
else:
    print(f" {total_fail} FAILURE(S):")
    for f in g_fail:  print(f"  [GLOBAL]  {f[0]}: got {f[3]}, want {f[2]}")
    for f in p_fail:  print(f"  [PROJECT] {f[0]}: got {f[3]}, want {f[2]}")
    for f in c_fail:  print(f"  [CODE]    {f[0]}: got {f[4]}, want {f[3]}")
print(f"{'='*65}")
sys.exit(1 if total_fail else 0)
