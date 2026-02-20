{
  description = "MCP server for agent I/O — scroll-wheel multi-choice input and TTS narration for hands-free Claude Code";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      eachDefaultSystem = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      pythonSets = eachDefaultSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312;
        in
        (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            overlay
          ]
        )
      );

    in
    {
      devShells = eachDefaultSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          venv = pythonSet.mkVirtualEnv "io-mcp" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              venv
              pkgs.uv
              pkgs.pulseaudio  # paplay
              pkgs.espeak-ng
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );

      packages = eachDefaultSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          pythonSet = pythonSets.${system};
          venv = pythonSet.mkVirtualEnv "io-mcp" workspace.deps.all;
          inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;
        in lib.fix (self: {
          default = self.io-mcp;
          io-mcp = mkApplication {
            inherit venv;
            package = pythonSet.io-mcp;
          };
        })
      );
    };
}
