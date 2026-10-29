#![cfg(windows)]
//! Exercise the actual PowerShell argument forwarding with build/runtime tripwires.
use std::{fs, path::PathBuf, process::Command};

#[test]
#[ignore = "controlled PowerShell launcher test; no Python or provider"]
fn launchers_forward_only_explicit_profiles_and_reject_invalid_choices() {
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/profile-launcher-tests")
        .join(std::process::id().to_string());
    fs::create_dir_all(&base).unwrap();
    for (name, source) in [
        ("chat", include_str!("../open-chat.ps1")),
        ("live", include_str!("../open-live.ps1")),
    ] {
        let original = r#"& "$PSScriptRoot/target/debug/litetui-rs.exe""#;
        assert_eq!(
            source.matches(original).count(),
            1,
            "unique runtime boundary"
        );
        let script = base.join(format!("{name}.ps1"));
        fs::write(&script, source.replace(original, "& Test-RustLaunch")).unwrap();
        let wrapper = base.join("wrapper.ps1");
        fs::write(
            &wrapper,
            r#"
param([string]$Launcher,[string]$Capture,[string]$Profile)
$ErrorActionPreference='Stop'
function cargo { $global:LASTEXITCODE=0 }
function Test-RustLaunch {
    [System.IO.File]::WriteAllText($Capture,(ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) -Compress))
    $global:LASTEXITCODE=0
}
if ($Profile -eq 'absent') { & $Launcher } else { & $Launcher -ToolProfile $Profile }
"#,
        )
        .unwrap();
        for profile in ["absent", "strict", "interactive", "autonomous", "invalid"] {
            let capture = base.join(format!("{name}-{profile}.json"));
            assert!(!capture.exists(), "fresh test root");
            let result = Command::new("pwsh.exe")
                .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
                .arg(&wrapper)
                .arg("-Launcher")
                .arg(&script)
                .arg("-Capture")
                .arg(&capture)
                .args(["-Profile", profile])
                .output()
                .unwrap();
            if profile == "invalid" {
                assert!(!result.status.success() && !capture.exists());
                continue;
            }
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
            let args: Vec<String> = serde_json::from_slice(&fs::read(&capture).unwrap()).unwrap();
            let position = args.iter().position(|a| a == "--tool-profile");
            if profile == "absent" {
                assert!(
                    position.is_none(),
                    "launcher must preserve unresolved selection"
                );
            } else {
                let position = position.expect("explicit profile forwarded");
                assert_eq!(args[position + 1], profile);
                assert_eq!(
                    args.iter()
                        .filter(|a| a.as_str() == "--tool-profile")
                        .count(),
                    1
                );
            }
        }
    }
}

#[test]
#[ignore = "controlled PowerShell launcher test; no Python or provider"]
fn chat_launcher_forwards_explicit_resume_without_creating_a_new_root() {
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/resume-launcher-tests")
        .join(std::process::id().to_string());
    fs::create_dir_all(&base).unwrap();
    let script = base.join("chat.ps1");
    fs::write(
        &script,
        include_str!("../open-chat.ps1").replace(
            r#"& "$PSScriptRoot/target/debug/litetui-rs.exe""#,
            "& Test-RustLaunch",
        ),
    )
    .unwrap();
    let wrapper = base.join("wrapper.ps1");
    fs::write(&wrapper,r#"
param([string]$Launcher,[string]$Capture,[string]$Retained,[switch]$MissingRoot)
$ErrorActionPreference='Stop'
function cargo { $global:LASTEXITCODE=0 }
function Test-RustLaunch {
    [System.IO.File]::WriteAllText($Capture,(ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) -Compress))
    $global:LASTEXITCODE=0
}
if ($MissingRoot) { & $Launcher -Resume '12345678-1234-4123-8123-123456789abc' }
else { & $Launcher -Resume '12345678-1234-4123-8123-123456789abc' -DataRoot $Retained -Backend codex -Model saved-model }
"#).unwrap();
    for missing in [true, false] {
        let capture = base.join(format!("resume-{missing}.json"));
        let retained = base.join("retained root");
        let mut command = Command::new("pwsh.exe");
        command
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
            .arg(&wrapper)
            .arg("-Launcher")
            .arg(&script)
            .arg("-Capture")
            .arg(&capture)
            .arg("-Retained")
            .arg(&retained);
        if missing {
            command.arg("-MissingRoot");
        }
        let output = command.output().unwrap();
        assert_eq!(
            output.status.success(),
            !missing,
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        if missing {
            assert!(!capture.exists());
        } else {
            let args: Vec<String> = serde_json::from_slice(&fs::read(capture).unwrap()).unwrap();
            for (key, value) in [
                ("--resume", "12345678-1234-4123-8123-123456789abc"),
                ("--backend", "codex"),
                ("--model", "saved-model"),
                ("--data-root", retained.to_str().unwrap()),
            ] {
                let i = args.iter().position(|v| v == key).unwrap();
                if key == "--data-root" {
                    assert_eq!(PathBuf::from(&args[i + 1]), PathBuf::from(value));
                } else {
                    assert_eq!(args[i + 1], value);
                }
            }
            assert!(
                !retained.exists(),
                "launcher must not initialize a resumed root"
            );
            assert!(!args.iter().any(|v| v == "--tool-profile"));
        }
    }
}
