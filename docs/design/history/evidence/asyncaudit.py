import ast, sys, pathlib
root=pathlib.Path(sys.argv[1])
BLOCKING={"sqlite3","time.sleep","requests","urllib.request","subprocess","smtplib","boto3","httpx.Client","httpx.get","httpx.post"}
for svc in sorted(list((root/"services").iterdir())+list((root/"apps").iterdir())):
    files=list(svc.rglob("src/**/*.py"))
    if not files: continue
    n_async=n_sync=0; hits=set()
    for py in files:
        src=py.read_text(); tree=ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node,ast.AsyncFunctionDef):
                body=ast.unparse(node)
                if any(d for d in node.decorator_list if "get" in ast.unparse(d) or "post" in ast.unparse(d) or "put" in ast.unparse(d) or "delete" in ast.unparse(d) or "route" in ast.unparse(d)): n_async+=1
                for b in BLOCKING:
                    if b in body: hits.add(b)
            elif isinstance(node,ast.FunctionDef):
                if any(d for d in node.decorator_list if "get" in ast.unparse(d) or "post" in ast.unparse(d) or "put" in ast.unparse(d) or "delete" in ast.unparse(d)): n_sync+=1
    print(f"{svc.name:20s} async_handlers={n_async:3d} sync_handlers={n_sync:3d} blocking_in_async={sorted(hits)}")
