"""Redfish-compliant emulator backed by incus or LXD.

Exposes every incus/LXD instance (across all projects) as a Redfish
ComputerSystem so MAAS's built-in Redfish power driver can manage them. The
MAAS BMC's `node_id` field must be set to the system Id used here:
``{project}_{vm}`` (e.g. ``default_node01``). Bare ``{vm}`` is also accepted
and resolved by searching projects, with ``default`` preferred.

Backend is selected via REDFISH_BACKEND=incus (default) or REDFISH_BACKEND=lxd.
The latter shells out to the `lxc` CLI instead of `incus`. REDFISH_CLI may
override the binary name directly.

Auth: HTTP Basic. Credentials come from REDFISH_USERNAME / REDFISH_PASSWORD
(defaults: admin / password). Set REDFISH_NO_AUTH=1 to disable.

TLS: enabled by default (MAAS's Redfish driver assumes HTTPS when no scheme
is given in power_address). REDFISH_TLS_CERT / REDFISH_TLS_KEY may point at
existing PEM files; otherwise a self-signed cert is generated and cached at
~/.cache/incus-redfish/. Set REDFISH_TLS=0 to serve plain HTTP instead.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from functools import wraps
from hashlib import sha1

from flask import Flask, Response, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s incus-redfish %(message)s",
)
log = logging.getLogger("incus_redfish")

app = Flask(__name__)


@app.after_request
def _log_response(resp):
    body = resp.get_data(as_text=False)
    preview = body[:300].decode("utf-8", errors="replace") if body else ""
    log.info(
        "%s %s → %s ct=%s body=%r",
        request.method, request.path,
        resp.status, resp.content_type, preview,
    )
    return resp


USERNAME = os.environ.get("REDFISH_USERNAME", "admin")
PASSWORD = os.environ.get("REDFISH_PASSWORD", "password")
AUTH_DISABLED = os.environ.get("REDFISH_NO_AUTH") == "1"

# Backend CLI: "incus" (default) or "lxd" (uses the `lxc` binary).
# REDFISH_CLI overrides the binary name directly.
_BACKEND = os.environ.get("REDFISH_BACKEND", "incus").lower()
_BACKEND_BINARIES = {"incus": "incus", "lxd": "lxc", "lxc": "lxc"}
if _BACKEND not in _BACKEND_BINARIES:
    raise SystemExit(
        f"REDFISH_BACKEND must be one of {sorted(_BACKEND_BINARIES)}, got {_BACKEND!r}"
    )
CLI = os.environ.get("REDFISH_CLI", _BACKEND_BINARIES[_BACKEND])

ID_SEP = "_"


# ---------- incus helpers ----------

_CLI_TIMEOUT = 15  # seconds; keeps gunicorn workers from blocking indefinitely


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True,
                           timeout=_CLI_TIMEOUT)
        return True, r.stdout
    except subprocess.TimeoutExpired:
        return False, f"{CLI} CLI timed out after {_CLI_TIMEOUT}s"
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or "").strip()
    except FileNotFoundError:
        return False, f"{CLI} CLI not found on PATH"


_IDEMPOTENT_PHRASES = ("already running", "already stopped", "not running")


def _run_nowait(cmd):
    """Start cmd in a background thread; return immediately.

    Used for power-state changes (stop/start/restart) that can take longer
    than the gunicorn worker timeout. MAAS polls PowerState separately, so it
    is safe to return 204 as soon as the operation is dispatched.
    """
    def _bg():
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                out = (r.stderr or r.stdout or "").strip()
                lowered = out.lower()
                if not any(p in lowered for p in _IDEMPOTENT_PHRASES):
                    log.error("background command %s exited %d: %s",
                              cmd, r.returncode, out)
        except Exception as exc:
            log.error("background command %s failed: %s", cmd, exc)

    t = threading.Thread(target=_bg, daemon=True)
    t.start()


def list_instances():
    """Return a list of dicts: {project, name, status} for every instance."""
    ok, out = _run([CLI, "list", "--all-projects", "--format", "json"])
    if not ok:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    result = []
    for item in data:
        result.append({
            "project": item.get("project") or "default",
            "name": item.get("name", ""),
            "status": (item.get("status") or "").lower(),
        })
    return result


def encode_id(project: str, name: str) -> str:
    return f"{project}{ID_SEP}{name}"


def resolve_id(system_id: str):
    """Map a Redfish system Id back to (project, name).

    Accepts ``{project}_{vm}`` first; falls back to bare ``{vm}`` matched
    against the live instance list (preferring the ``default`` project).
    Returns (project, name) or (None, None) if not found.
    """
    instances = list_instances()
    # Exact {project}_{vm} match
    exact = [i for i in instances if encode_id(i["project"], i["name"]) == system_id]
    if len(exact) > 1:
        log.warning("system_id %r is ambiguous — matches %s; using first",
                    system_id, [(i["project"], i["name"]) for i in exact])
    if exact:
        return exact[0]["project"], exact[0]["name"]
    # Bare VM name fallback
    matches = [i for i in instances if i["name"] == system_id]
    if matches:
        for m in matches:
            if m["project"] == "default":
                return m["project"], m["name"]
        return matches[0]["project"], matches[0]["name"]
    return None, None


def power_state(status: str) -> str:
    """Translate incus status to Redfish PowerState."""
    s = status.lower()
    if s == "running":
        return "On"
    if s == "stopped":
        return "Off"
    if s in ("starting", "restarting"):
        return "PoweringOn"
    if s in ("stopping", "freezing"):
        return "PoweringOff"
    if s == "frozen":
        return "Paused"
    return "Off"


# ---------- auth ----------

def requires_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if AUTH_DISABLED:
            return fn(*args, **kwargs)
        a = request.authorization
        if not a or a.username != USERNAME or a.password != PASSWORD:
            return Response(
                json.dumps({"error": {"code": "Base.1.0.AccessDenied",
                                      "message": "Authentication required"}}),
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Redfish"',
                         "Content-Type": "application/json"},
            )
        return fn(*args, **kwargs)
    return wrapper


# ---------- response helpers ----------

def redfish_json(payload, status=200, etag=None):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["OData-Version"] = "4.0"
    if etag is not None:
        resp.headers["ETag"] = etag
    return resp


def system_etag(system_id: str, state: str) -> str:
    raw = f"{system_id}:{state}".encode()
    return 'W/"' + sha1(raw).hexdigest()[:16] + '"'


# ---------- service root / collections ----------

@app.route("/redfish/v1", methods=["GET"])
@app.route("/redfish/v1/", methods=["GET"])
@requires_auth
def service_root():
    return redfish_json({
        "@odata.type": "#ServiceRoot.v1_5_0.ServiceRoot",
        "@odata.id": "/redfish/v1/",
        "Id": "RootService",
        "Name": "Incus Redfish Service",
        "RedfishVersion": "1.6.0",
        "Systems": {"@odata.id": "/redfish/v1/Systems"},
        "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
        "Managers": {"@odata.id": "/redfish/v1/Managers"},
        "Links": {"Sessions": {"@odata.id": "/redfish/v1/SessionService/Sessions"}},
    })


@app.route("/redfish/v1/Systems", methods=["GET"])
@requires_auth
def systems_collection():
    instances = list_instances()
    members = [
        {"@odata.id": f"/redfish/v1/Systems/{encode_id(i['project'], i['name'])}"}
        for i in instances
    ]
    return redfish_json({
        "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
        "@odata.id": "/redfish/v1/Systems",
        "Name": "Computer System Collection",
        "Members@odata.count": len(members),
        "Members": members,
    })


@app.route("/redfish/v1/Chassis", methods=["GET"])
@requires_auth
def chassis_collection():
    return redfish_json({
        "@odata.type": "#ChassisCollection.ChassisCollection",
        "@odata.id": "/redfish/v1/Chassis",
        "Name": "Chassis Collection",
        "Members@odata.count": 0,
        "Members": [],
    })


@app.route("/redfish/v1/Managers", methods=["GET"])
@requires_auth
def managers_collection():
    return redfish_json({
        "@odata.type": "#ManagerCollection.ManagerCollection",
        "@odata.id": "/redfish/v1/Managers",
        "Name": "Manager Collection",
        "Members@odata.count": 0,
        "Members": [],
    })


# ---------- system resource ----------

def _system_payload(system_id, project, name, status):
    state = power_state(status)
    payload = {
        "@odata.type": "#ComputerSystem.v1_10_0.ComputerSystem",
        "@odata.id": f"/redfish/v1/Systems/{system_id}",
        "Id": system_id,
        "Name": name,
        "SystemType": "Virtual",
        "Manufacturer": "LXD" if _BACKEND in ("lxd", "lxc") else "Incus",
        "Model": "Virtual Machine",
        "SerialNumber": system_id,
        "PowerState": state,
        "Status": {"State": "Enabled", "Health": "OK"},
        "Boot": {
            "BootSourceOverrideEnabled": "Disabled",
            "BootSourceOverrideTarget": "None",
            "BootSourceOverrideMode": "UEFI",
            "BootSourceOverrideTarget@Redfish.AllowableValues": [
                "None", "Pxe", "Hdd", "Cd", "BiosSetup",
            ],
        },
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": f"/redfish/v1/Systems/{system_id}/Actions/ComputerSystem.Reset",
                "ResetType@Redfish.AllowableValues": [
                    "On", "ForceOff", "GracefulShutdown",
                    "GracefulRestart", "ForceRestart", "PushPowerButton",
                ],
            }
        },
        "Oem": {"Incus": {"Project": project, "Instance": name}},
    }
    return payload, state


@app.route("/redfish/v1/SessionService/Sessions", methods=["POST"])
@requires_auth
def create_session():
    return _error(405, "Base.1.0.ActionNotSupported",
                  "Session auth not supported; use HTTP Basic")


@app.route("/redfish/v1/Systems/<system_id>", methods=["GET"])
@requires_auth
def get_system(system_id):
    project, name = resolve_id(system_id)
    if project is None:
        return _not_found(system_id)
    # Re-query the specific instance for fresh status.
    ok, out = _run([CLI, "list", name, "--project", project, "--format", "json"])
    status = None
    if ok:
        try:
            arr = json.loads(out)
            for item in arr:
                if item.get("name") == name:
                    status = (item.get("status") or "").lower()
                    break
        except json.JSONDecodeError:
            pass
    if status is None:
        # Instance vanished between resolve_id and the list call.
        return _not_found(system_id)
    payload, state = _system_payload(system_id, project, name, status)
    return redfish_json(payload, etag=system_etag(system_id, state))


@app.route("/redfish/v1/Systems/<system_id>", methods=["PATCH"])
@requires_auth
def patch_system(system_id):
    """Accept Boot override PATCH from MAAS.

    Incus VMs configured for MAAS already PXE-boot via firmware boot order,
    so we acknowledge the change without persisting it. Returning success
    keeps the MAAS deployment workflow happy.
    """
    project, name = resolve_id(system_id)
    if project is None:
        return _not_found(system_id)
    body = request.get_json(silent=True) or {}
    boot = body.get("Boot")
    if boot is not None and not isinstance(boot, dict):
        return _error(400, "Base.1.0.PropertyValueTypeError", "Boot must be an object")
    return Response(status=204)


# ---------- reset action ----------

RESET_MAP = {
    "On":               [CLI, "start"],
    "ForceOn":          [CLI, "start"],
    "ForceOff":         [CLI, "stop", "--force"],
    "GracefulShutdown": [CLI, "stop"],
    "GracefulRestart":  [CLI, "restart"],
    "ForceRestart":     [CLI, "restart", "--force"],
    "PushPowerButton":  None,  # handled specially: toggle
    "Nmi":              None,  # unsupported
}


@app.route("/redfish/v1/Systems/<system_id>/Actions/ComputerSystem.Reset",
           methods=["POST"])
@requires_auth
def reset_system(system_id):
    project, name = resolve_id(system_id)
    if project is None:
        return _not_found(system_id)

    body = request.get_json(silent=True) or {}
    reset_type = body.get("ResetType")
    if reset_type not in RESET_MAP:
        return _error(400, "Base.1.0.ActionParameterUnknown",
                      f"Unsupported ResetType: {reset_type!r}")

    if reset_type == "PushPowerButton":
        ok, out = _run([CLI, "list", name, "--project", project, "--format", "json"])
        running = False
        if ok:
            try:
                arr = json.loads(out)
                for item in arr:
                    if item.get("name") == name:
                        running = (item.get("status") or "").lower() == "running"
                        break
            except json.JSONDecodeError:
                pass
        base = [CLI, "stop", "--force"] if running else [CLI, "start"]
    elif reset_type == "Nmi":
        return _error(501, "Base.1.0.ActionNotSupported",
                      "Nmi is not supported on this backend")
    else:
        base = list(RESET_MAP[reset_type])

    _run_nowait(base + [name, "--project", project])
    return Response(status=204)


# ---------- errors ----------

def _error(status, code, message):
    body = {
        "error": {
            "code": code,
            "message": message,
            "@Message.ExtendedInfo": [
                {"MessageId": code, "Message": message, "Severity": "Critical"}
            ],
        }
    }
    return redfish_json(body, status=status)


def _not_found(system_id):
    return _error(404, "Base.1.0.ResourceNotFound",
                  f"System '{system_id}' not found")


@app.errorhandler(404)
def _404(_):
    return _error(404, "Base.1.0.ResourceNotFound", "Resource not found")


@app.errorhandler(405)
def _405(_):
    return _error(405, "Base.1.0.ActionNotSupported", "Method not allowed")


# ---------- TLS ----------

def _default_cert_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "incus-redfish")


def ensure_self_signed(cert_path, key_path):
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "incus-redfish")])
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    extra = os.environ.get("REDFISH_TLS_SAN", "")
    for entry in filter(None, (s.strip() for s in extra.split(","))):
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(entry)))
        except ValueError:
            sans.append(x509.DNSName(entry))

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(key_path, 0o600)


def build_ssl_context():
    if os.environ.get("REDFISH_TLS") == "0":
        return None
    cert = os.environ.get("REDFISH_TLS_CERT")
    key = os.environ.get("REDFISH_TLS_KEY")
    if not cert or not key:
        cert_dir = _default_cert_dir()
        cert = cert or os.path.join(cert_dir, "cert.pem")
        key = key or os.path.join(cert_dir, "key.pem")
        ensure_self_signed(cert, key)
    return (cert, key)


if __name__ == "__main__":
    host = os.environ.get("REDFISH_HOST", "0.0.0.0")
    default_port = "8443" if os.environ.get("REDFISH_TLS") != "0" else "8000"
    port = int(os.environ.get("REDFISH_PORT", default_port))
    app.run(host=host, port=port, ssl_context=build_ssl_context())
