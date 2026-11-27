"""Przegląda drzewo węzłów serwera OPC UA robota i zapisuje je do pliku tekstowego.
Dzięki temu poznamy, jakie zmienne G4 faktycznie udostępnia (instrukcja ich nie podaje).
NIEPRZETESTOWANE na prawdziwym robocie. Wymaga: pip install asyncua

Użycie:  python tools/browse_nodes.py R1 [głębokość]
Ustawienia (IP, login, certyfikaty) bierze z config.json."""
import asyncio
import json
import os
import sys

from asyncua import Client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from monitor.settings import robot_password  # noqa: E402


async def walk(node, depth, max_depth, out):
    if depth > max_depth:
        return
    for child in await node.get_children():
        try:
            name = (await child.read_browse_name()).Name
            cls = (await child.read_node_class()).name
            line = f"{'  ' * depth}{name}  [{cls}]  {child.nodeid.to_string()}"
            if cls == "Variable":
                try:
                    line += f"  = {await child.read_value()!r}"
                except Exception:
                    pass
            out.append(line)
            print(line)
            await walk(child, depth + 1, max_depth, out)
        except Exception as e:
            out.append(f"{'  ' * depth}(błąd: {e})")


async def main(rid, max_depth):
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    r = next(x for x in cfg["robots"] if x["id"] == rid)
    sec = cfg.get("opcua", {})
    c = Client(url=f"opc.tcp://{r['ip']}:{r.get('port', 4840)}", timeout=10)
    if r.get("username"):
        c.set_user(r["username"])
        c.set_password(robot_password(r))
    if sec.get("security_policy") and sec["security_policy"] != "None":
        s = f"{sec['security_policy']},{sec['security_mode']},{os.path.join(ROOT, sec['client_cert'])},{os.path.join(ROOT, sec['client_key'])}"
        if r.get("server_cert"):
            s += "," + os.path.join(ROOT, r["server_cert"])
        await c.set_security_string(s)
    out = []
    async with c:
        await walk(c.nodes.objects, 0, max_depth, out)
    path = os.path.join(ROOT, "data", f"nodes_{rid}.txt")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    print(f"\nZapisano: {path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 4))
