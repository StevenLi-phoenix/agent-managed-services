import ast, sys, pathlib
root=pathlib.Path(sys.argv[1])
targets=["components/sdk","components/auth","services","apps"]
def enclosing(tree,node):
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent=parent
    n=node; chain=[]
    while hasattr(n,"parent"):
        n=n.parent
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)): chain.append(n.name)
    return ".".join(reversed(chain)) or "<module>"
for t in targets:
    for py in sorted((root/t).rglob("src/**/*.py")):
        src=py.read_text(); tree=ast.parse(src)
        hits=[]
        for node in ast.walk(tree):
            s=ast.get_source_segment(src,node) or ""
            if isinstance(node,ast.Call):
                name=ast.unparse(node.func)
                if name in ("os.environ.get","os.getenv","load_from_env","sdk_load_from_env","_os.environ.get") or name.endswith(".load_from_env"):
                    hits.append((node.lineno,name,enclosing(tree,node)))
            elif isinstance(node,ast.Subscript) and ast.unparse(node.value) in ("os.environ","_os.environ"):
                hits.append((node.lineno,"os.environ[]",enclosing(tree,node)))
        for ln,name,fn in hits:
            if fn in ("build_app","<module>") or py.name=="config.py": continue
            print(f"{py.relative_to(root)}:{ln} {name} in {fn}")
