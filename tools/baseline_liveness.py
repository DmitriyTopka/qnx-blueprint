"""Compare Blueprint with a plain liveness check on every recorded trace.

The liveness check fails a run if a server in the contract (a process on the
right side of an allow edge) died. Run from the repository root:
    PYTHONPATH=. python tools/baseline_liveness.py
"""
import os, yaml
from blueprint import kev, ipc, checks, contract, snapshot
def alive_check(trace, con, snap):
    """Baseline: every contract process seen in the trace is still alive at the end."""
    m = ipc.build(trace)
    servers = {srv for (cli, srv) in con.allow}
    died = sorted({m.name(p) for p in m.deaths if m.name(p) in servers})
    return died
rows=[]
def run(label, tracepath, conpath, expect_fail):
    tr=kev.read(tracepath); con=contract.load(conpath)
    sp=os.path.splitext(tracepath.replace('.gz',''))[0]+'.pidin'
    snap=snapshot.load(sp) if os.path.exists(sp) else None
    f,_=checks.run(tr,con,snap)
    errs=sorted({x.check for x in f if x.severity==checks.ERROR})
    died=alive_check(tr,con,snap)
    rows.append((label, 'FAIL' if errs else 'PASS', ','.join(errs) or '-', 'FAIL' if died else 'PASS', ','.join(died) or '-'))
base='demo/cockpit'
spec=yaml.safe_load(open(base+'/scenarios.yaml'))
for sc in spec['scenarios']:
    run('cockpit/'+sc['name'], f"{base}/traces/{sc['name']}.kev", f"{base}/blueprint.yaml", sc['expect']=='fail')
for t in ['healthy','hang','hang_attach','death']:
    p=f'demo/external/qpp/traces/{t}.kev.gz'
    if os.path.exists(p): run('qpp/'+t, p, 'demo/external/qpp/isolation.yaml', None)
for t in ['stall_quiet','hang','death','death_select']:
    p=f'demo/external/socketcan/traces/{t}.kev.gz'
    if os.path.exists(p): run('socketcan/'+t, p, 'demo/external/socketcan/isolation.yaml', None)
print(f"{'scenario':30}{'blueprint':10}{'checks':45}{'alive?':8}died")
for r in rows: print(f"{r[0]:30}{r[1]:10}{r[2]:45}{r[3]:8}{r[4]}")
bf=[r for r in rows if r[1]=='FAIL']; caught=[r for r in bf if r[3]=='FAIL']
bp=[r for r in rows if r[1]=='PASS']; fa=[r for r in bp if r[3]=='FAIL']
print(f"Blueprint PASS: {len(bp)}; liveness false alarms there: {len(fa)}")
print(f"\nBlueprint FAIL: {len(bf)}; of them a liveness check also fails: {len(caught)}; missed by liveness: {len(bf)-len(caught)}")
