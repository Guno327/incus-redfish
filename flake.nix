{
  description = "Incus Redfish emulator — Redfish BMC frontend for incus/LXD VMs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        packages = {
          default = pkgs.callPackage ./package.nix { };
          incus-redfish = pkgs.callPackage ./package.nix { };
        };

        # Development shell: run `nix develop` to get flask + gunicorn available
        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.flask ps.gunicorn ps.cryptography ]))
          ];
        };
      }
    ) // {
      # NixOS module — import in your system flake like:
      #
      #   inputs.incus-redfish.url = "github:you/incus-redfish";
      #
      #   nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      #     modules = [
      #       inputs.incus-redfish.nixosModules.default
      #       {
      #         services.incus-redfish = {
      #           enable = true;
      #           host = "0.0.0.0";
      #           port = 8443;
      #           environmentFile = "/run/secrets/incus-redfish.env";
      #         };
      #       }
      #     ];
      #   };
      nixosModules = {
        default = import ./module.nix;
        incus-redfish = import ./module.nix;
      };

      # Overlay that adds `pkgs.incus-redfish` to a nixpkgs instance
      overlays.default = final: _prev: {
        incus-redfish = final.callPackage ./package.nix { };
      };
    };
}
