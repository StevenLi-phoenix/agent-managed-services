import os, importlib, resource, threading, time, urllib.request, json, sys
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.responses import JSONResponse
import uvicorn
members=[("kvservice","kvservice.main"),("timeservice","timeservice.main"),("llmpricing","llmpricing.main"),("pages","pages.main")]
apps=[]
for name,mod in members:
    env={"SVC_NAME":name,"SVC_SECRET":"x-"+name,"SVC_AUDIENCE":name,"SVC_DEV":"1","SVC_ROOT_PATH":"/"+name,"AMS_DATA_DIR":f"/home/harness/store/scratch-pool/data/{name}"}
    os.makedirs(env["AMS_DATA_DIR"],exist_ok=True)
    saved={k:os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        app=importlib.import_module(mod).build_app()
    finally:
        for k,v in saved.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v
    print(name,"root_path=",repr(app.root_path), "routes=",len(app.routes)); apps.append((name,app))
parent=Starlette(routes=[Mount("/"+n, app=a) for n,a in apps]+[])
cfg=uvicorn.Config(parent,host="127.0.0.1",port=20999,log_level="warning")
srv=uvicorn.Server(cfg); t=threading.Thread(target=srv.run,daemon=True); t.start()
for _ in range(50):
    try: urllib.request.urlopen("http://127.0.0.1:20999/kvservice/health",timeout=1); break
    except Exception: time.sleep(0.1)
for n,_ in apps:
    for path in (f"/{n}/health", f"/{n}/openapi.json", f"/{n}/docs"):
        try:
            r=urllib.request.urlopen("http://127.0.0.1:20999"+path,timeout=3); body=r.read(300)
            extra=""
            if path.endswith("openapi.json"): extra="servers="+str(json.loads(body if len(body)<300 else urllib.request.urlopen("http://127.0.0.1:20999"+path).read()).get("servers"))
            print(path, r.status, extra or body[:80])
        except Exception as e: print(path, "ERR", e)
print("rss", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024, "MiB"); srv.should_exit=True
