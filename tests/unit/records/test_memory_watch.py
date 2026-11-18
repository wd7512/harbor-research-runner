from harbor_research_runner.records.memory_watch import memory_watch_stats


def test_memory_watch_stats_aggregates_by_location_and_trial():
    rows = [
        {
            "type": "sample",
            "t": 1.0,
            "trial": "trial",
            "location": "harbor-job",
            "used_mb": 10.5,
            "limit_mb": 100,
        },
        {
            "type": "sample",
            "t": 2.0,
            "trial": "trial",
            "location": "harbor-job",
            "used_mb": 20,
            "limit_mb": 100,
        },
        {
            "type": "sample",
            "t": 2.0,
            "trial": "trial",
            "location": "canary-job",
            "used_mb": 30,
            "limit_mb": 200,
        },
    ]

    assert memory_watch_stats(rows) == {
        ("harbor-job", "trial"): (20, 100),
        ("canary-job", "trial"): (30, 200),
    }


def test_memory_watch_stats_restricts_rows_to_an_inclusive_time_window():
    rows = [
        {
            "type": "sample",
            "t": timestamp,
            "trial": "trial",
            "location": "harbor-job",
            "used_mb": used,
            "limit_mb": 100,
        }
        for timestamp, used in ((1.0, 10), (2.0, 20), (3.0, 30))
    ]

    assert memory_watch_stats(rows, start=2.0, end=2.0) == {("harbor-job", "trial"): (20, 100)}
