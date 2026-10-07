import subprocess, pathlib
root=pathlib.Path.cwd(); log=root.parent/'pb21-final.log'; marker=root.parent/'pb21-final.exit'
cmd=['python','-m','pytest','tests/test_web_router.py','tests/test_harness_api.py','tests/test_harness_store.py','tests/test_evidence_ledger.py','tests/test_retrieval_quality_fixes.py','tests/test_helpers.py','tests/test_worker_queue.py','-q']
with log.open('w',encoding='utf-8') as f:
 p=subprocess.run(cmd,cwd=root,stdout=f,stderr=subprocess.STDOUT,text=True)
marker.write_text(str(p.returncode),encoding='utf-8')
