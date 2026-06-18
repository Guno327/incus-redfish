{ stdenv }:

stdenv.mkDerivation {
  pname = "incus-redfish";
  version = "0.1.0";
  src = ./.;
  dontBuild = true;
  installPhase = ''
    mkdir -p $out/lib/incus-redfish
    cp webhook.py $out/lib/incus-redfish/
  '';
}
