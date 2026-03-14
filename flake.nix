{
  description = "canvas-claude: Claude Code tree navigation via Canvas Chat";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python311;
      in {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            python
            pkgs.uv
            pkgs.ruff
            pkgs.nodejs
            pkgs.stdenv.cc.cc.lib   # libstdc++ for tokenizers
          ];

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
            export CANVAS_CHAT_DIR="''${CANVAS_CHAT_DIR:-$HOME/lib/canvas-chat}"

            # Resolve project dir: prefer CANVAS_CLAUDE_DIR, fall back to
            # the directory containing flake.nix (assumes nix develop is
            # run from the project root or with a path argument).
            CANVAS_CLAUDE_DIR="''${CANVAS_CLAUDE_DIR:-$HOME/src/canvas-claude}"
            export CANVAS_CHAT_CONFIG_PATH="$CANVAS_CLAUDE_DIR/config.yaml"

            if [ ! -d .venv ]; then
              echo "Creating venv and installing canvas-chat..."
              uv venv --python ${python}/bin/python
              uv pip install -e "$CANVAS_CHAT_DIR"
              uv pip install -e "$CANVAS_CHAT_DIR[dev]"
              uv pip install textual
            fi

            source .venv/bin/activate

            alias cc-tree="python $CANVAS_CLAUDE_DIR/tui.py"

            echo ""
            echo "canvas-claude dev shell"
            echo "  start server:  uvicorn canvas_chat.app:app --reload --port 7865"
            echo "  tree viewer:   cc-tree /path/to/project"
            echo "  config:        $CANVAS_CHAT_CONFIG_PATH"
            echo "  canvas-chat:   $CANVAS_CHAT_DIR"
            echo ""
          '';
        };
      });
}
