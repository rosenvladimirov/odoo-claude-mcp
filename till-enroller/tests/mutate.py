#!/usr/bin/env python3
"""Мутации: всяка мутация чупи едно правило в кода; тестовете трябва да паднат.

    python till-enroller/tests/mutate.py <лог>

Файлът се връща в изходния вид след всяка мутация (и при прекъсване).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable

MUTATIONS = [
    ("dns-zone: без проверката за .lan.mcpworks.net", "till-enroller/enroller.py",
     'if name != name.lower() or not name.endswith("." + LAN_SUFFIX):',
     "if name != name.lower():", "till-enroller/tests"),
    # Пазачите в upsert/delete сами по себе са еквивалентни мутации (те викат dns_find, който също
    # пази) — затова се махат трите наведнъж: в класа Cloudflare не остава проверка.
    ("dns-zone: Cloudflare без нито един пазач", "till-enroller/enroller.py",
     [('        name = check_lan_name(name)\n        q = urllib.parse.urlencode({"type": "A"',
       '        q = urllib.parse.urlencode({"type": "A"'),
      ('        name = check_lan_name(name)\n        body = {"type": "A"', '        body = {"type": "A"'),
      ("        name = check_lan_name(name)\n        n = 0", "        n = 0")],
     None, "till-enroller/tests"),
    ("rollback: undo-ът не се изпълнява", "till-enroller/enroller.py",
     "        for what, fn in reversed(undo):\n            try:\n                fn()",
     "        for what, fn in reversed(undo):\n            try:\n                pass", "till-enroller/tests"),
    ("rollback: без метенето по списъка в CF", "till-enroller/enroller.py",
     "            failed += self._sweep(client, till)\n", "", "till-enroller/tests"),
    ("rollback: без сверката с CF (винаги „clean“)", "till-enroller/enroller.py",
     "            left = self._leftovers(client, till)\n            note",
     "            left = []\n            note", "till-enroller/tests"),
    ("rollback: cert-issuer не се връща", "till-enroller/enroller.py",
     '            undo.append(("cert-issuer", lambda: self.ci.remove(host)))\n', "", "till-enroller/tests"),
    ("lan: публичен IP минава", "till-enroller/enroller.py",
     "    if addr.version != 4 or not any(addr in n for n in PRIVATE_NETS):",
     "    if addr.version != 4:", "till-enroller/tests"),
    ("тайни: филтърът не чисти", "till-enroller/enroller.py",
     "            text = text.replace(s, \"***\")", "            pass", "till-enroller/tests"),
    ("crt: не влиза в EFP1", "till-enroller/enroller.py",
     "                              list(self.ssh_keys), crt=crt)", "                              list(self.ssh_keys))",
     "till-enroller/tests"),
    ("crt: тайната не се регистрира в редактора", "till-enroller/enroller.py",
     "        REDACT.add(secret)\n        return secret", "        return secret", "till-enroller/tests"),
    ("заварен тунел: взема се", "till-enroller/enroller.py",
     "        if self.cf.tunnel_by_name(tname):\n", "        if False:\n", "till-enroller/tests"),
    ("access: без проверка на common_name", "cert-issuer/issuer.py",
     '                or not any(hmac.compare_digest(str(claims.get("common_name", "")), c) for c in self.client_ids)):',
     "                ):", "cert-issuer/tests/test_admin_api.py"),
    ("access: без проверка на подписа", "cert-issuer/issuer.py",
     '            key.verify(_b64u_dec(s64), f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())\n', "",
     "cert-issuer/tests/test_admin_api.py"),
    ("access: без проверка на aud", "cert-issuer/issuer.py",
     "        if (self.aud not in auds\n", "        if (False\n", "cert-issuer/tests/test_admin_api.py"),
    ("ci: пази тайната, не хеша", "cert-issuer/issuer.py",
     't.update({"secret_sha256": _sha256(secret),', 't.update({"secret_sha256": _sha256(secret), "secret": secret,',
     "cert-issuer/tests/test_admin_api.py"),
]


def main(logpath):
    out = open(logpath, "w")
    killed = 0
    for name, rel, old, new, tests in MUTATIONS:
        f = ROOT / rel
        orig = f.read_text()
        pairs = old if isinstance(old, list) else [(old, new)]
        mutated = orig
        for o, n in pairs:
            assert mutated.count(o) == 1, f"{name}: мястото не е намерено еднозначно"
            mutated = mutated.replace(o, n)
        try:
            f.write_text(mutated)
            r = subprocess.run([PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", tests],
                               cwd=ROOT, capture_output=True, text=True)
        finally:
            f.write_text(orig)
        tail = (r.stdout.strip().splitlines() or ["?"])[-1]
        dead = r.returncode != 0
        killed += dead
        line = f"{'УБИТА ' if dead else 'ЖИВА  '} {name}: {tail}"
        print(line)
        out.write(line + "\n")
        if dead:
            failed = [x for x in r.stdout.splitlines() if x.startswith("FAILED")]
            out.write("        " + (failed[0] if failed else "") + "\n")
    summary = f"убити {killed}/{len(MUTATIONS)}"
    print(summary)
    out.write(summary + "\n")
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
