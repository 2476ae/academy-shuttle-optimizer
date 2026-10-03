from shuttle import default_network
from shuttle.simulate import compare, format_table, main

NET = default_network()


def test_same_seed_gives_same_results():
    a = compare(NET, days=4, seed=5)
    b = compare(NET, days=4, seed=5)
    assert [s.__dict__ for s in a] == [s.__dict__ for s in b]


def test_optimized_beats_first_come_first_served():
    by_name = {s.policy: s for s in compare(NET, days=30, seed=2)}
    assert by_name["optimized"].total_mean < by_name["fcfs"].total_mean
    assert by_name["optimized"].wait_mean < by_name["fcfs"].wait_mean


def test_table_lists_every_policy():
    table = format_table(compare(NET, days=2, seed=1))
    for label in ("요청 순서대로", "정시 출발", "자동 배차"):
        assert label in table


def test_cli_runs(capsys):
    assert main(["--days", "2", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "자동 배차" in out
