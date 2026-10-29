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
