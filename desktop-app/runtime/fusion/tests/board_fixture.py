"""Test-only keys/signatures. Never load the real board private key."""
import subprocess

from fusion.mutate import canonical


def keypair(directory):
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "TEST-ONLY-private.pem"
    public = directory / "TEST-ONLY-public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)],
                   capture_output=True, check=True, timeout=10)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   capture_output=True, check=True, timeout=10)
    return private, public


def signature(document, private):
    path = private.parent / "TEST-ONLY-document.json"
    path.write_bytes(canonical(document).encode("utf-8"))
    return subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(private),
                           "-rawin", "-in", str(path)],
                          capture_output=True, check=True, timeout=10).stdout.hex()


def envelope(document, private):
    return {"alg": "ed25519", "document": document, "signature": signature(document, private)}
