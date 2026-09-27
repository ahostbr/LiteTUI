"""LiteTUI passes only its marker-captured spawner to register/heartbeat."""
from litetui.harness import Seat


def test_presence_argv_names_captured_spawner_on_each_write():
    seat = Seat("child", "Child", "gpt-6-sol")
    assert "--spawned-by" not in seat._presence_argv()
    seat.spawned_by = "leader-id"
    args = seat._presence_argv()
    assert args[args.index("--spawned-by") + 1] == "leader-id"
    assert seat._presence_argv() == args
