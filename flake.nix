{
  description = "cctree: Claude Code session tree viewer TUI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python311;
        pythonEnv = pkgs.python3.withPackages (ps: [ ps.textual ]);
      in {
        packages.default = pkgs.writeShellScriptBin "cctree" ''
          exec ${pythonEnv}/bin/python ${./tui.py} "$@"
        '';

        devShells.default = pkgs.mkShell {
          buildInputs = [
            python
            pkgs.uv
            pkgs.ruff
          ];

          shellHook = ''
            if [ ! -d .venv ]; then
              echo "Creating venv..."
              uv venv --python ${python}/bin/python
              uv pip install textual
            fi

            source .venv/bin/activate

            alias cctree="python tui.py"

            echo ""
            echo "cctree dev shell"
            echo "  tree viewer:   cctree /path/to/project"
            echo ""
          '';
        };
      });
}
