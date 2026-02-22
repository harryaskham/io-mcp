{
  description = "io-mcp Android app - native frontend for io-mcp MCP server";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config = {
            android_sdk.accept_license = true;
            allowUnfree = true;
          };
        };

        jdk = pkgs.openjdk17;
        buildToolsVersion = "34.0.0";
        androidComposition = pkgs.androidenv.composeAndroidPackages {
          buildToolsVersions = [ buildToolsVersion ];
          platformVersions = [ "34" ];
          abiVersions = [ "arm64-v8a" "x86_64" ];
        };
        androidSdk = androidComposition.androidsdk;
        ANDROID_SDK_ROOT = "${androidSdk}/libexec/android-sdk";

        gradle = pkgs.gradle.override { java = jdk; };
        gradleWrapped = pkgs.runCommandLocal "gradle-wrapped" {
          nativeBuildInputs = [ pkgs.makeBinaryWrapper ];
        } ''
          mkdir -p $out/bin
          ln -s ${gradle}/bin/gradle $out/bin/gradle
          wrapProgram $out/bin/gradle \
            --add-flags "-Dorg.gradle.project.android.aapt2FromMavenOverride=${ANDROID_SDK_ROOT}/build-tools/${buildToolsVersion}/aapt2"
        '';

      in {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            jdk
            androidSdk
            gradleWrapped
            pkgs.android-tools  # adb
          ];
          JAVA_HOME = jdk.home;
          inherit ANDROID_SDK_ROOT;
          shellHook = ''
            echo "io-mcp Android dev environment"
            echo "  JAVA_HOME=$JAVA_HOME"
            echo "  ANDROID_SDK_ROOT=$ANDROID_SDK_ROOT"
            echo ""
            echo "Build: gradle assembleDebug"
            echo "Install: adb install app/build/outputs/apk/debug/app-debug.apk"
          '';
        };
      });
}
