"""Test połączenia z robotem G4 po OPC UA, etapy E1-E5 z instrukcji odbioru.

Użycie:
  python tools/check_robot.py R1               pełny test robota R1 (ustawienia z config.json i nodes.json)
  python tools/check_robot.py R1 --samples 30  dłuższa próbka wartości (domyślnie 10 s)
  python tools/check_robot.py --make-cert      generuje certyfikat klienta (certs/ wg config.json)

Skrypt tylko czyta dane z robota; niczego do niego nie zapisuje.
Uwaga: warstwa OPC UA nie była jeszcze sprawdzona na prawdziwym sterowniku G4."""
import argparse
import asyncio
import datetime
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from monitor.settings import robot_password  # noqa: E402
APP_URI = "urn:freeopcua:client"  # domyślny URI klienta asyncua; musi być w certyfikacie (SAN)

HINTS = [
    ("BadCertificateUntrusted", "robot nie ufa certyfikatowi klienta: zaimportuj go w Trusted Certificates (krok 11 checklisty), albo klient nie ufa serwerowi: sprawdź plik server_cert"),
    ("BadSecurityChecksFailed", "to samo co BadCertificateUntrusted albo niezgodne ustawienia bezpieczeństwa; sprawdź certyfikaty i politykę"),
    ("BadCertificateUriInvalid", "URI w certyfikacie klienta nie zgadza się z URI klienta; wygeneruj certyfikat przez --make-cert"),
    ("BadCertificateTimeInvalid", "certyfikat nieważny czasowo albo zegar robota/komputera jest przestawiony"),
    ("BadSecurityPolicyRejected", "robot nie oferuje tej polityki; porównaj z listą endpointów powyżej i popraw security_policy"),
    ("BadSecurityModeRejected", "robot nie oferuje tego trybu; popraw security_mode w config.json"),
    ("BadIdentityTokenRejected", "zły login lub hasło (krok 9 checklisty)"),
    ("BadUserAccessDenied", "brak uprawnień użytkownika albo zły login/hasło"),
    ("BadNodeIdUnknown", "zły node_id (np. zmienił się indeks ns= po restarcie robota); porównaj z data/nodes_<ID>.txt"),
    ("TimeoutError", "brak odpowiedzi w czasie: robot nie w AUTO, funkcja OPC UA nie jest Valid albo zapora blokuje ruch"),
]


def hint(err):
    text = f"{type(err).__name__}: {err}"
    for key, msg in HINTS:
        if key in text:
            return msg
    return None


def path(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


def make_cert(cfg):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    sec = cfg.get("opcua", {})
    cert_path = path(sec.get("client_cert", "certs/client.der"))
    key_path = path(sec.get("client_key", "certs/client_key.pem"))
    if os.path.exists(cert_path) or os.path.exists(key_path):
        print(f"Plik już istnieje ({cert_path} / {key_path}); nic nie nadpisuję. Usuń go ręcznie, jeśli chcesz wygenerować nowy.")
        return 1
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    host = socket.gethostname()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Monitor stanowisk spawalniczych"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Monitor")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(APP_URI), x509.DNSName(host)]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=True,
                                         data_encipherment=True, key_agreement=False, key_cert_sign=True,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.DER))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    print(f"Certyfikat klienta: {cert_path}\nKlucz prywatny:     {key_path}  (nie udostępniaj tego pliku)")
    print(f"URI aplikacji w certyfikacie: {APP_URI}, ważny 10 lat.")
    print("Teraz zaimportuj plik .der w robocie: Trusted Certificates → Import (z karty SD/USB).")
    return 0


def step(n, title):
    print(f"\n[{n}] {title}")


async def run(cfg, nodes, rid, samples):
    from asyncua import Client

    r = next((x for x in cfg["robots"] if x["id"] == rid), None)
    if not r:
        print(f"Nie ma robota {rid} w config.json (są: {', '.join(x['id'] for x in cfg['robots'])})")
        return 1
    sec = cfg.get("opcua", {})
    host, port = r["ip"], r.get("port", 4840)
    url = f"opc.tcp://{host}:{port}"
    results = []

    def ok(msg):
        print("  OK     " + msg)

    def bad(msg, err=None):
        print("  BŁĄD   " + msg)
        if err is not None:
            print(f"         {type(err).__name__}: {err}")
            h = hint(err)
            if h:
                print("         Wskazówka: " + h)
        results.append(msg)

    step("E1", f"TCP do {host}:{port}")
    try:
        socket.create_connection((host, port), timeout=5).close()
        ok("port otwarty")
    except Exception as e:
        bad("nie można otworzyć portu (adres, port, tryb AUTO, funkcja OPC UA Valid, zapora)", e)
        return finish(results)

    step("E3", "Endpointy oferowane przez robota")
    try:
        probe = Client(url=url, timeout=10)
        eps = await probe.connect_and_get_server_endpoints()
        for e in eps:
            tokens = ", ".join(t.TokenType.name for t in e.UserIdentityTokens)
            print(f"         {e.SecurityPolicyUri.split('#')[-1]:<18} {e.SecurityMode.name:<15} logowanie: {tokens}")
        want = sec.get("security_policy", "None")
        if want != "None" and not any(e.SecurityPolicyUri.endswith("#" + want) and e.SecurityMode.name == sec.get("security_mode") for e in eps):
            bad(f"robot nie oferuje pary {want} / {sec.get('security_mode')} z config.json")
        else:
            ok("lista endpointów pobrana, zgodna z config.json")
    except Exception as e:
        bad("nie udało się pobrać endpointów", e)

    step("E5", "Sesja z naszym klientem")
    client = Client(url=url, timeout=10)
    if r.get("username"):
        client.set_user(r["username"])
        client.set_password(robot_password(r))
    try:
        if sec.get("security_policy") and sec["security_policy"] != "None":
            s = f"{sec['security_policy']},{sec.get('security_mode', 'SignAndEncrypt')},{path(sec['client_cert'])},{path(sec['client_key'])}"
            if r.get("server_cert"):
                s += "," + path(r["server_cert"])
            await client.set_security_string(s)
        await client.connect()
    except Exception as e:
        bad("sesja nie została nawiązana", e)
        return finish(results)
    try:
        ok("sesja nawiązana")
        metrics = nodes.get("metrics", {})
        mapped = {k: m for k, m in metrics.items() if m.get("node_id")}
        for req in ("state", "piece_counter"):
            if req not in mapped:
                bad(f"w nodes.json brak node_id dla „{req}” (wymagane do rozliczeń)")
        if not mapped:
            print("         nodes.json nie ma żadnych node_id; uruchom tools/browse_nodes.py i uzupełnij plik")
            return finish(results)
        history = {k: [] for k in mapped}
        t_end = time.time() + samples
        while True:
            for k, m in mapped.items():
                try:
                    history[k].append(await client.get_node(m["node_id"]).read_value())
                except Exception as e:
                    bad(f"odczyt „{k}” ({m['node_id']}) nie powiódł się", e)
                    mapped = {kk: mm for kk, mm in mapped.items() if kk != k}
                    break
            if time.time() >= t_end or not mapped:
                break
            await asyncio.sleep(1.0)
        step("E5", f"Próbka wartości ({samples} s)")
        for k, vals in history.items():
            if not vals:
                continue
            uniq = list(dict.fromkeys(vals))
            print(f"         {k:<14} {metrics[k]['node_id']:<28} wartości: {uniq[:8]}{' …' if len(uniq) > 8 else ''}")
            if k == "state":
                smap = metrics[k].get("state_map", {})
                unk = [v for v in uniq if str(v) not in smap]
                if unk:
                    bad(f"wartości stanu bez wpisu w state_map: {unk}")
                else:
                    ok("wszystkie zaobserwowane wartości stanu są w state_map")
            if k == "piece_counter":
                try:
                    int(vals[-1])
                    ok(f"licznik odczytany jako liczba ({vals[-1]})")
                except Exception:
                    bad(f"licznik nie jest liczbą: {vals[-1]!r}")
        print("         Podczas próbki wykonaj w robocie cykl lub zmień stan, żeby zobaczyć zmianę wartości.")
    finally:
        await client.disconnect()
    return finish(results)


def finish(results):
    print("\n" + "=" * 60)
    if results:
        print(f"WYNIK: BŁĄD ({len(results)}). Wklej cały wynik do protokołu i prześlij.")
        return 1
    print("WYNIK: OK. Wpisz w protokole etapy E1, E3 i E5.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("robot", nargs="?", help="id robota z config.json, np. R1")
    ap.add_argument("--samples", type=int, default=10, help="czas próbki wartości w sekundach")
    ap.add_argument("--make-cert", action="store_true", help="wygeneruj certyfikat klienta")
    a = ap.parse_args()
    cfg = load("config.json")
    if a.make_cert:
        return make_cert(cfg)
    if not a.robot:
        ap.print_help()
        return 2
    try:
        import asyncua  # noqa: F401
    except ImportError:
        print("Brak biblioteki asyncua: pip install asyncua")
        return 2
    nodes = load("nodes.json") if os.path.exists(os.path.join(ROOT, "nodes.json")) else {}
    return asyncio.run(run(cfg, nodes, a.robot, a.samples))


if __name__ == "__main__":
    sys.exit(main())
