"""T0306: policy-only JSON receipts; NEVER execute any command in this file."""
import pytest

from litetui import tool_policy as tp

READ = "$d=Get-Content -LiteralPath data.json -Raw | ConvertFrom-Json"


@pytest.fixture
def decide(tmp_path, monkeypatch):
    # Avoid the independent protected owner run.bat floor depending on test cwd.
    monkeypatch.chdir(tmp_path)

    def evaluate(command, profile=tp.INTERACTIVE):
        return tp.evaluate(profile, tp.SHELL_POLICY, {'command': command}, tmp_path,
                           shell='powershell', tool_name='powershell')

    return evaluate


@pytest.mark.parametrize('command', [
    # Historical grammar shapes with neutral fixtures; exact evidence stays private.
    "$d=Get-Content -LiteralPath C:/example/data/queued-items.json -Raw | ConvertFrom-Json; $d.tasks | Where-Object { $_.id -eq 'ITEM001' } | ConvertTo-Json -Depth 8 | Write-Output",
    "$s = Get-Content -Raw C:/example/data/reviewing-snapshot.json | ConvertFrom-Json; $s.reviewing_rows | Select-Object ITEM001,ITEM002,ITEM003,ITEM004,ITEM005 | ConvertTo-Json -Depth 8",
    "$s = Get-Content -Raw C:/example/data/reviewing-snapshot.json | ConvertFrom-Json; $s.reviewing_rows | Where-Object { $_.id -in @('ITEM001','ITEM002','ITEM003','ITEM004','ITEM005') } | Select-Object id,title,tier,thinking,assignee | ConvertTo-Json -Depth 5",
    READ + '; $d.tasks',
    READ + '; $D.tasks | Select-Object -Property id,title | Sort-Object id',
    READ + '; $d.PSObject.Properties.Name; $d.method_proofs.Count',
    READ + "; $d.tasks | Where-Object { $_.id -like 'ITEM*' -or $_.parent_id -eq 'GROUP001' } | ConvertTo-Json -Depth 5",
    READ + '; $d.tasks | Where-Object { $_.active -eq $true -and $_.count -ne 0 } | ConvertTo-Json -Compress',
    READ + "; $d.tasks | Where-Object { $_.title -match 'Example|approval' } | Select-Object -First 4",
    READ + "; $d.tasks | Where-Object { $_.title -eq 'it''s inert; Remove-Item x | $(evil)' }",
    '$d=Get-Content "literal file.json" -Raw | ConvertFrom-Json\n$d.tasks | Select-Object -ExpandProperty id',
    READ + '; $e=Get-Content other.json | ConvertFrom-Json; $e.tasks; $d.tasks;',
    'Get-Content data.json -Raw | ConvertFrom-Json',  # already allowed
])
def test_file_json_queries_allow(decide, command):
    result = decide(command)
    assert result.action == tp.ALLOW, result
    assert not result.danger
    assert decide(command, tp.STRICT).action == tp.CONFIRM


@pytest.mark.parametrize('tail', [
    '$d.tasks.Clear()', '$d.tasks[0]', '$d.$member', '$d.tasks.$member',
    '$d.tasks | Where-Object { Remove-Item old }',
    '$d.tasks | Where-Object { $_.id -eq (Invoke-Expression payload) }',
    "$d.tasks | Where-Object { $_.id -eq 'x'; Stop-Process -Id 42 }",
    '$d.tasks | Where-Object { $_.id.StartsWith("T") }',
    "$d.tasks | Where-Object { $_.id -eq 'x' } | ForEach-Object { & $tool }",
    '$d.tasks | ForEach-Object { $_.id }',  # not a general scriptblock exemption
    '$d.tasks | Select-Object @{Name="x";Expression={Remove-Item old}}',
    '$d.tasks | Select-Object -Property id -OutVariable state',
    '$d.tasks | ConvertTo-Json -Depth 4 -PipelineVariable state',
    '$d.tasks | Sort-Object -Property id -ErrorVariable state',
    '$d.tasks | Out-File output.json', '$d.tasks | Set-Content output.json',
    '$d.tasks > output.json', '$d.tasks | Tee-Object -FilePath output.json',
    '$d.tasks | Where-Object { $_.id -eq "$(Remove-Item old)" }',
    '$d.tasks | Where-Object { $_.id -eq "$external" }',
    '$d.tasks | Where-Object { $_.id -in @("x", $(evil)) }',
    '$d.tasks | Where-Object { $_.id -eq $external }',
    '$d.tasks | Where-Object { $_.id -eq "x" } garbage',
    '$d.tasks | Where-Object { $_.id -eq "x" } |',
    '$d.tasks; Expand-Archive package.zip', '$d.tasks; Remove-Item old',
    '$d.tasks; & C:/foreign/program.exe status',
    '$d.tasks; Invoke-RestMethod -Method Delete -Uri http://127.0.0.1:8000/items/example',
    '$d.tasks; $d=Get-Process; $d.Path', '$unknown.tasks',
])
def test_unsupported_or_side_effectful_tail_still_asks(decide, tail):
    assert decide(READ + '; ' + tail).action == tp.CONFIRM


@pytest.mark.parametrize('command', [
    # Neutral lease-status fixture: command output is NOT proven inert JSON
    # merely because ConvertFrom-Json appears downstream.
    '$s=python X:/example/tools/service_status.py status --project X:/example/project/example.project | ConvertFrom-Json; $s.leases.worker | ConvertTo-Json -Depth 5; $s.leases.session | ConvertTo-Json; Get-Date -Format o; [math]::Round((Get-PSDrive X).Free/1GB,2)',
    '$d=arbitrary.exe | ConvertFrom-Json; $d.tasks',
    '$d=Invoke-RestMethod http://localhost/data | ConvertFrom-Json; $d.tasks',
    '$d=Get-Content $path -Raw | ConvertFrom-Json; $d.tasks',
    '$d=Get-Content "$(arbitrary.exe)" -Raw | ConvertFrom-Json; $d.tasks',
    '$d=Get-Content "$env:HOME/data.json" -Raw | ConvertFrom-Json; $d.tasks',
    '$d=Get-Content data.json -OutVariable side | ConvertFrom-Json; $d.tasks',
    '$d=Get-Content data.json | ConvertFrom-Json -ErrorVariable side; $d.tasks',
    '$d=Get-Content data.json | ConvertFrom-Json; $e=$d; $e.tasks',
    '$d.tasks; ' + READ,
    READ + '; $d=arbitrary.exe; $d.tasks',
    READ + '; $d=Get-Content other.json | ConvertFrom-Json | arbitrary.exe; $d.tasks',
    "$p=[Diagnostics.ProcessStartInfo]::new('git'); $p.ArgumentList.Add('log'); [Diagnostics.Process]::Start($p)",
])
def test_unproved_provenance_and_process_wrappers_still_ask(decide, command):
    assert decide(command).action == tp.CONFIRM


@pytest.mark.parametrize('command', [
    '& C:/foreign/program.exe status',
    'Invoke-RestMethod -Method Delete -Uri http://127.0.0.1:8000/items/example',
])
def test_preexisting_unrestricted_argv_allowances_are_not_claimed_fixed(decide, command):
    # Characterization, NOT an endorsement of safety. fd5fd19 removed the launch
    # gate; direct HTTP mutation also lacks a danger row. Reported to leader.
    assert decide(command).action == tp.ALLOW


def test_floor_still_sees_entire_original_input(decide):
    for profile in tp.PROFILE_NAMES:
        assert decide(READ + '; $d.tasks; Remove-Item $HOME -Recurse -Force', profile).action == tp.DENY
