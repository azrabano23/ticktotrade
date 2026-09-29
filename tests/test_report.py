"""README numbers must be the ones generated from results/results.json."""
import json

import pytest

from ticktotrade import report


def test_readme_results_block_matches_results_json():
    rj = report.RESULTS_DIR / "results.json"
    if not rj.exists():
        pytest.skip("run `ticktotrade report` first")
    res = json.loads(rj.read_text())
    readme = report.README.read_text()
    block = readme.split(report.BEGIN, 1)[1].split(report.END, 1)[0].strip()
    assert block == report.results_table(res).strip()
    assert all(r["pass"] for r in res["runs"].values())
    md = (report.RESULTS_DIR / "REPORT.md").read_text()
    assert report.results_table(res).strip() in md


def test_readme_real_itch_block_matches_json():
    from ticktotrade import replay
    rj = report.RESULTS_DIR / "real_itch.json"
    if not rj.exists():
        pytest.skip("run `ticktotrade real-report` first")
    res = json.loads(rj.read_text())
    readme = report.README.read_text()
    block = readme.split(replay.BEGIN, 1)[1].split(replay.END, 1)[0].strip()
    assert block == replay.real_table(res).strip()
    assert res["all_pass"]
