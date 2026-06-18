# DISCLAIMER
This entire codebase was created by claude, as such quality cannot be guaranteed....

# incus-redfish

A Redfish-compliant emulator that fronts [incus](https://linuxcontainers.org/incus/) or [LXD](https://canonical.com/lxd) virtual machines so [MAAS](https://maas.io/) can power-manage them using its built-in Redfish power driver — no custom webhook driver required.

Each instance (across every project) is exposed as a Redfish `ComputerSystem`. MAAS hits the standard endpoints to query power state, issue resets, and set one-shot PXE boot.

## How it works

The emulator implements the subset of Redfish that MAAS's driver actually calls:

| Endpoint | Method | Purpose |
|---|---|---|
| `/redfish/v1/` | GET | Service root |
| `/redfish/v1/Systems` | GET | Lists every incus VM as a member |
| `/redfish/v1/Systems/<id>` | GET | Returns `PowerState`, `Boot`, actions, ETag |
| `/redfish/v1/Systems/<id>` | PATCH | Accepts MAAS's one-shot PXE boot override (no-op) |
| `/redfish/v1/Systems/<id>/Actions/ComputerSystem.Reset` | POST | Translates `ResetType` to incus commands |

`ResetType` mapping (`<cli>` is `incus` or `lxc` depending on `REDFISH_BACKEND`):

| Redfish | Command |
|---|---|
| `On`, `ForceOn` | `<cli> start` |
| `ForceOff` | `<cli> stop -f` |
| `GracefulShutdown` | `<cli> stop` |
| `GracefulRestart` | `<cli> restart` |
| `ForceRestart` | `<cli> restart -f` |
| `PushPowerButton` | toggle (start if stopped, force-stop if running) |

System IDs use the format **`{project}_{vm}`**, e.g. `default_node01` or `sunbeam_juju`. A bare VM name is also accepted as a fallback (resolved against the live instance list, preferring the `default` project).

## Requirements

- Python 3.9+
- Either the `incus` CLI (default) or the `lxc` CLI (LXD mode), on `PATH` and authenticated against the local daemon
- Python packages: `flask`, `cryptography` (`cryptography` only needed if TLS is on and you don't supply your own cert)

```bash
pip install flask cryptography
```

## Configuration

All configuration is via environment variables. Nothing is required to start the service — the defaults produce a working HTTPS BMC on port 8443 with credentials `admin` / `password`.

### Backend

| Variable | Default | Description |
|---|---|---|
| `REDFISH_BACKEND` | `incus` | `incus` runs the `incus` CLI; `lxd` runs the `lxc` CLI. JSON output schemas are compatible. |
| `REDFISH_CLI` | derived from `REDFISH_BACKEND` | Override the binary name directly (e.g. `/usr/local/bin/incus`). |

### Auth

| Variable | Default | Description |
|---|---|---|
| `REDFISH_USERNAME` | `admin` | HTTP Basic username MAAS must present. |
| `REDFISH_PASSWORD` | `password` | HTTP Basic password MAAS must present. **Change this.** |
| `REDFISH_NO_AUTH` | _(unset)_ | Set to `1` to disable auth entirely. Only safe on a trusted network. |

### TLS

MAAS's Redfish driver prepends `https://` when no scheme is given in `power_address`, so TLS is on by default.

| Variable | Default | Description |
|---|---|---|
| `REDFISH_TLS` | _(unset, on)_ | Set to `0` to serve plain HTTP. |
| `REDFISH_TLS_CERT` | auto-generated | Path to a PEM certificate. |
| `REDFISH_TLS_KEY` | auto-generated | Path to a PEM private key. |
| `REDFISH_TLS_SAN` | _(unset)_ | Comma-separated extra SANs (DNS names or IPs) baked into the auto-generated cert. Ignored if you provide your own cert. |

If `REDFISH_TLS_CERT`/`REDFISH_TLS_KEY` are unset, a self-signed certificate is generated on first run and cached at `~/.cache/incus-redfish/{cert,key}.pem`. The cert is valid for 10 years and includes SANs for `localhost`, `127.0.0.1`, and `::1` by default. Delete those files to regenerate.

### Listener

| Variable | Default | Description |
|---|---|---|
| `REDFISH_HOST` | `0.0.0.0` | Bind address. |
| `REDFISH_PORT` | `8443` (TLS) / `8000` (plain) | TCP port. |

## Running

```bash
# Default: HTTPS on 0.0.0.0:8443, basic auth admin/password, incus backend
REDFISH_PASSWORD='change-me' python webhook.py

# LXD instead of incus
REDFISH_BACKEND=lxd REDFISH_PASSWORD='change-me' python webhook.py
```

With your own TLS cert and extra SANs on the auto-generated one:

```bash
# Custom cert
REDFISH_TLS_CERT=/etc/ssl/incus-redfish.crt \
REDFISH_TLS_KEY=/etc/ssl/incus-redfish.key \
REDFISH_PASSWORD='change-me' \
python webhook.py

# Or let it auto-generate but with your hostname/IPs in the SANs
REDFISH_TLS_SAN='bmc.lan,10.0.0.5' \
REDFISH_PASSWORD='change-me' \
python webhook.py
```

Plain HTTP (useful for local testing — remember to set `power_address` with an explicit `http://` scheme in MAAS):

```bash
REDFISH_TLS=0 REDFISH_PASSWORD='change-me' python webhook.py
# listens on http://0.0.0.0:8000
```

For production, run it behind systemd / a process manager and consider fronting it with a real WSGI server (e.g. `gunicorn webhook:app`) instead of Flask's development server.

## NixOS

A NixOS module and Nix flake are included. The module runs the service under gunicorn (not Flask's dev server) as a dedicated `incus-redfish` system user.

### Minimal configuration

Add the flake as an input and import the module:

```nix
# flake.nix (your system flake)
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    incus-redfish.url = "github:you/incus-redfish";
  };

  outputs = { nixpkgs, incus-redfish, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        incus-redfish.nixosModules.default
        {
          services.incus-redfish = {
            enable = true;
            # Secrets go in environmentFile, not here
            environmentFile = "/run/secrets/incus-redfish.env";
          };
        }
      ];
    };
  };
}
```

`/run/secrets/incus-redfish.env` (keep this file outside the Nix store, mode 0400):

```
REDFISH_USERNAME=admin
REDFISH_PASSWORD=change-me
```

This produces an HTTPS service on `0.0.0.0:8443`. On first start a self-signed TLS certificate is generated and cached at `/var/lib/incus-redfish/cert.pem` (delete to regenerate).

When `host` is set to a specific address (anything other than `0.0.0.0` or `::`), that address is automatically added to the certificate's Subject Alternative Names so MAAS can reach the emulator without a hostname mismatch. For example, `host = "10.0.0.5"` produces a cert with SANs for `localhost`, `127.0.0.1`, `::1`, and `10.0.0.5`. This applies only to the auto-generated certificate; bring-your-own certs are used as-is.

### Module options

| Option | Default | Description |
|---|---|---|
| `enable` | `false` | Enable the service. |
| `environmentFile` | `null` | Path to a file loaded as systemd `EnvironmentFile=`. Use for `REDFISH_PASSWORD` and other secrets. |
| `host` | `"0.0.0.0"` | Bind address. A specific address (not `0.0.0.0` / `::`) is automatically added to the auto-generated TLS certificate's SANs. |
| `port` | `8443` | TCP port. |
| `tls` | `true` | Serve HTTPS. Set to `false` if terminating TLS at a reverse proxy. |
| `tlsCertFile` | `null` | Path to a PEM certificate. When null a self-signed cert is auto-generated. |
| `tlsKeyFile` | `null` | Path to a PEM private key paired with `tlsCertFile`. |
| `backend` | `"incus"` | `"incus"` or `"lxd"`. |
| `extraGroups` | `["incus-admin"]` | Groups for the service user. LXD users should set `["lxd"]`. |
| `package` | derived | Override the webhook.py package. |

### Bring your own TLS certificate

```nix
services.incus-redfish = {
  enable = true;
  environmentFile = "/run/secrets/incus-redfish.env";
  tlsCertFile = "/etc/ssl/incus-redfish.crt";
  tlsKeyFile = "/etc/ssl/incus-redfish.key";
};
```

### LXD backend

```nix
services.incus-redfish = {
  enable = true;
  backend = "lxd";
  extraGroups = [ "lxd" ];
  environmentFile = "/run/secrets/incus-redfish.env";
};
```

### Plain HTTP (no TLS)

Only appropriate if a reverse proxy handles TLS termination. Remember to set `power_address` with an explicit `http://` scheme in MAAS.

```nix
services.incus-redfish = {
  enable = true;
  tls = false;
  port = 8000;
  environmentFile = "/run/secrets/incus-redfish.env";
};
```

### Development shell

```bash
nix develop   # drops you into a shell with flask + gunicorn + cryptography
python webhook.py
```

## Configuring MAAS

For each incus VM you want MAAS to manage:

1. In the MAAS UI, open the machine → **Configuration** → **Power configuration**.
2. **Power type:** `Redfish`.
3. Fill in the fields:

   | MAAS field | Value |
   |---|---|
   | **Power address** | `https://<emulator-host>:8443` (or `http://<host>:8000` if `REDFISH_TLS=0`). Without a scheme MAAS assumes `https://`. |
   | **Power user** | matches `REDFISH_USERNAME` |
   | **Power password** | matches `REDFISH_PASSWORD` |
   | **Node ID** | `{project}_{vm}`, e.g. `default_node01`, `sunbeam_juju` |

4. Save. MAAS will immediately probe `GET /redfish/v1/Systems/<node_id>` to validate.

To list the `node_id` values for every VM the emulator sees:

```bash
# incus mode
incus list --all-projects -c np --format csv | awk -F, '{print $2"_"$1}'
# LXD mode
lxc list --all-projects -c np --format csv | awk -F, '{print $2"_"$1}'
```

If you leave **Node ID** blank, MAAS auto-discovers the first member from `/redfish/v1/Systems` — only useful if you run one emulator per VM.

## Troubleshooting

- **MAAS can't connect / TLS errors:** make sure the `power_address` scheme matches what the emulator serves. With self-signed certs MAAS's Redfish driver accepts them; if your environment is strict, supply a proper cert via `REDFISH_TLS_CERT`/`REDFISH_TLS_KEY` (Flask dev server) or `tlsCertFile`/`tlsKeyFile` (NixOS module / gunicorn).
- **401 from emulator:** username/password mismatch between env vars and MAAS power config.
- **404 from emulator:** the `node_id` in MAAS doesn't correspond to a live incus instance. Verify with `incus list --all-projects`.
- **Power action fails with "already running"/"already stopped":** treated as success — no action needed.
- **`<cli> CLI not found on PATH`:** the user running the emulator can't see `incus`/`lxc`. Ensure the chosen backend's CLI is installed and the service runs as a user that's a member of the `incus-admin` / `lxd` group (or equivalent).

## Files

- `webhook.py` — the emulator (single file).
- `flake.nix` — Nix flake exposing the package and NixOS module.
- `module.nix` — NixOS module (systemd service, gunicorn, options).
- `package.nix` — Nix derivation for the webhook.py package.
- `README.md` — this document.
