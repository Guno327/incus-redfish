{ config, lib, pkgs, ... }:

let
  cfg = config.services.incus-redfish;

  # TLS: gunicorn handles certs directly; env vars REDFISH_TLS_CERT/KEY are
  # only read inside webhook.py's __main__ block and are dead under gunicorn.
  autoCert = "/var/lib/incus-redfish/cert.pem";
  autoKey  = "/var/lib/incus-redfish/key.pem";
  certFile = if cfg.tlsCertFile != null then cfg.tlsCertFile else autoCert;
  keyFile  = if cfg.tlsKeyFile  != null then cfg.tlsKeyFile  else autoKey;

  # cryptography is intentionally omitted: ensure_self_signed (the only
  # consumer) is only reachable from __main__, which gunicorn never executes.
  pythonEnv = pkgs.python3.withPackages (ps: [ ps.flask ps.gunicorn ]);

  tlsFlags = lib.optionalString cfg.tls "--certfile ${certFile} --keyfile ${keyFile}";

  # Add the bind address to the cert's SAN list when it is a specific address
  # rather than a wildcard, so MAAS can reach the emulator without hostname
  # mismatch errors.
  hostIsWildcard = cfg.host == "0.0.0.0" || cfg.host == "::";
  hostIsIp = builtins.match "[0-9].*" cfg.host != null || lib.hasInfix ":" cfg.host;
  extraSan = lib.optionalString (!hostIsWildcard)
    (if hostIsIp then ",IP:${cfg.host}" else ",DNS:${cfg.host}");

  precertScript = pkgs.writeShellScript "incus-redfish-precert" ''
    if [ ! -f ${autoCert} ]; then
      ${pkgs.openssl}/bin/openssl req -x509 -newkey rsa:2048 \
        -keyout ${autoKey} \
        -out ${autoCert} \
        -days 3650 -nodes \
        -subj '/CN=incus-redfish' \
        -addext 'subjectAltName=IP:127.0.0.1,IP:::1,DNS:localhost${extraSan}'
      chmod 600 ${autoKey}
    fi
  '';
in
{
  options.services.incus-redfish = {
    enable = lib.mkEnableOption "Incus Redfish emulator — Redfish BMC frontend for incus/LXD VMs";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./package.nix { }";
      description = "The incus-redfish package (must contain lib/incus-redfish/webhook.py).";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/incus-redfish.env";
      description = ''
        Path to a file loaded by systemd as EnvironmentFile=. Suitable for
        secrets that must not appear in the Nix store, e.g.:
          REDFISH_USERNAME=admin
          REDFISH_PASSWORD=change-me
          REDFISH_NO_AUTH=0
      '';
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      example = "127.0.0.1";
      description = "Address the webserver binds to.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8443;
      example = 8000;
      description = "TCP port the webserver listens on.";
    };

    tls = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Serve HTTPS. MAAS's Redfish driver assumes https:// when no scheme is
        given in power_address, so this should stay true in most deployments.
        Set to false only if you terminate TLS elsewhere (e.g. a reverse proxy).
      '';
    };

    tlsCertFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/etc/ssl/incus-redfish.crt";
      description = ''
        Path to a PEM TLS certificate. When null and tls = true, a self-signed
        certificate is generated on first start and cached at
        /var/lib/incus-redfish/cert.pem. Delete the file to regenerate.
      '';
    };

    tlsKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/etc/ssl/incus-redfish.key";
      description = ''
        Path to a PEM private key matching tlsCertFile. When null and tls = true,
        the auto-generated key is stored at /var/lib/incus-redfish/key.pem.
      '';
    };

    backend = lib.mkOption {
      type = lib.types.enum [ "incus" "lxd" ];
      default = "incus";
      description = ''
        CLI backend to use. "incus" calls the incus binary; "lxd" calls lxc.
        Must match the hypervisor installed on the host.
      '';
    };

    extraGroups = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "incus-admin" ];
      example = [ "lxd" ];
      description = ''
        Extra groups for the service user. The user needs membership in
        incus-admin (incus default) or lxd (LXD) to invoke the CLI.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.incus-redfish = {
      isSystemUser = true;
      group = "incus-redfish";
      extraGroups = cfg.extraGroups;
    };
    users.groups.incus-redfish = { };

    systemd.services.incus-redfish = {
      description = "Incus Redfish emulator";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "incus.service" ];

      environment = {
        # REDFISH_BACKEND is read at module import time, before gunicorn serves
        # any requests, so it must be set here rather than in environmentFile.
        REDFISH_BACKEND = cfg.backend;
        # incus CLI writes its config to ~/.config/incus, but the service user
        # has no writable home. Point it at the state directory instead.
        INCUS_CONF = "/var/lib/incus-redfish/.config";
        # Use the absolute path so the service doesn't depend on PATH containing
        # the incus/lxc binary (systemd services run with a restricted PATH).
        REDFISH_CLI = if cfg.backend == "lxd"
          then "${pkgs.lxd}/bin/lxc"
          else "${pkgs.incus}/bin/incus";
      };

      serviceConfig = {
        User = "incus-redfish";
        Group = "incus-redfish";

        ExecStartPre = lib.mkIf (cfg.tls && cfg.tlsCertFile == null)
          "${precertScript}";

        ExecStart = lib.concatStringsSep " " (lib.filter (s: s != "") [
          "${pythonEnv}/bin/gunicorn"
          "--chdir ${cfg.package}/lib/incus-redfish"
          "--bind ${cfg.host}:${toString cfg.port}"
          "--workers 1"
          tlsFlags
          "webhook:app"
        ]);

        EnvironmentFile = lib.mkIf (cfg.environmentFile != null) cfg.environmentFile;

        StateDirectory = "incus-redfish";
        StateDirectoryMode = "0750";

        Restart = "on-failure";
        RestartSec = "5s";

        # Hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "/var/lib/incus-redfish" ];
      };
    };
  };
}
