import os, importlib, resource, asyncio, json, signal, threading, time, urllib.request, contextlib
import uvicorn
members=["kvservice","timeservice","llmpricing","logservice","messageservice","commentservice"]
base=21000; ROOT="/home/harness/store/scratch-pool"
dbs={"KV_DB_PATH":"kvservice/kv.db","LOG_DB_PATH":"logservice/log.db","MESSAGE_DB_PATH":"messageservice/m.db","COMMENT_DB_PATH":"commentservice/c.db"}
os.environ.update({k:f"{ROOT}/data/{v}" for k,v in dbs.items()})   # union of non-identity keys
os.environ.update({"REGISTRY_URL":"http://127.0.0.1:20100","AUTH_URL":"http://127.0.0.1:20101","SVC_DEV":"1"})
IDENT=("SVC_NAME","SVC_AUDIENCE","SVC_SECRET","PORT","AMS_DATA_DIR")
def ident(name,port): return {"SVC_NAME":name,"SVC_AUDIENCE":name,"SVC_SECRET":"x-"+name,"PORT":str(port),"AMS_DATA_DIR":f"{ROOT}/data/{name}"}
@contextlib.contextmanager
def swapped(env):
    os.environ.update(env)
    try: yield
    finally:
        for k in env: os.environ.pop(k,None)
apps=[]
for i,name in enumerate(members):
    os.makedirs(f"{ROOT}/data/{name}",exist_ok=True)
    with swapped(ident(name,base+i)):
        app=importlib.import_module(name+".main").build_app()
    apps.append((name,base+i,app))
async def fetch(url):
    return await asyncio.to_thread(lambda: urllib.request.urlopen(url,timeout=3).read().decode())
async def main():
    servers=[]; failed={}
    for n,p,a in apps:
        s=uvicorn.Server(uvicorn.Config(a,host="127.0.0.1",port=p,log_config=None,access_log=False))
        s.config.load(); s.lifespan=s.config.lifespan_class(s.config)
        with swapped(ident(n,p)):
            try: await s.startup()            # lifespan startup runs here, env swapped in
            except SystemExit as e: failed[n]=f"startup exit {e.code}"; continue
        servers.append((n,p,s))
    loop=asyncio.get_running_loop()
    def stop(*_):
        for n,p,s in servers: s.should_exit=True
    loop.add_signal_handler(signal.SIGTERM, stop)
    tasks=[asyncio.create_task(s.main_loop()) for n,p,s in servers]
    await asyncio.sleep(0.5)
    for n,p,a in apps:
        try:
            body=await fetch(f"http://127.0.0.1:{p}/health"); oa=json.loads(await fetch(f"http://127.0.0.1:{p}/openapi.json"))
            print(n,p,"200",body.strip()[:40],"title=",oa.get("info",{}).get("title"))
        except Exception as e: print(n,p,"ERR",str(e)[:60], failed.get(n,""))
    print("rss",resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024,"MiB threads",threading.active_count(),"failed",failed)
    t0=time.time(); os.kill(os.getpid(), signal.SIGTERM); await asyncio.gather(*tasks)
    for n,p,s in servers:
        with swapped(ident(n,p)): await s.shutdown()
    print("stopped in",round(time.time()-t0,2),"s")
asyncio.run(main())
