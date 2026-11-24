"""Headline P&L glitch guard: one wild snapshot must not poison the
home headline or flatten the All-time chart.

Regression case (demonstrated 2026-11-23 on the pre-fix code): a
single $709,599.60 snapshot landing nearest the 24-hour mark made
change_24h return {'delta': -709349.6, 'pct': -99.96} for a $250
portfolio, and the same row anywhere in the series made chart_svg's
top label $709,599.60. The 24h-nearest baseline (2026-11-13) fixed
the poisoned-earliest-snapshot bug, but the baseline still trusted
any single point; this module proves the neighbour-rule filter
(sane_points) closes that hole without hiding genuine step changes.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from helpers import make_client, signup
from tradingbot.pnl import (change_24h, chart_points, chart_svg,
                            purge_absurd_snapshots, record_snapshot,
                            sane_points)


def _pt(total, created_at="2026-11-09T12:00:00Z"):
    return {"created_at": created_at, "total_value_usd": total}


class TestSanePoints(unittest.TestCase):
    def test_single_spike_dropped(self):
        points = [_pt(250.0, f"2026-11-09T1{i}:00:00Z") for i in range(5)]
        points.insert(2, _pt(709599.60, "2026-11-09T11:30:00Z"))
        kept = sane_points(points)
        self.assertEqual([p["total_value_usd"] for p in kept],
                         [250.0] * 5)

    def test_two_point_glitch_dropped(self):
        points = [_pt(250.0, f"2026-11-09T1{i}:00:00Z") for i in range(6)]
        points[2] = _pt(709599.60, "2026-11-09T12:00:00Z")
        points[3] = _pt(709599.60, "2026-11-09T13:00:00Z")
        kept = sane_points(points)
        self.assertEqual([p["total_value_usd"] for p in kept],
                         [250.0] * 4)

    def test_genuine_step_change_kept(self):
        # A real 1000x step persists: neighbours confirm the new
        # level, so neither the last old point nor the first new
        # points are flagged.
        totals = [250.0, 250.0, 250.0, 250000.0, 250000.0, 250000.0]
        points = [_pt(t, f"2026-11-09T1{i}:00:00Z") for i, t in
                  enumerate(totals)]
        self.assertEqual([p["total_value_usd"] for p in sane_points(points)],
                         totals)

    def test_genuine_gradual_climb_kept(self):
        totals = [100.0, 150.0, 220.0, 330.0, 500.0, 750.0]
        points = [_pt(t, f"2026-11-09T1{i}:00:00Z") for i, t in
                  enumerate(totals)]
        self.assertEqual([p["total_value_usd"] for p in sane_points(points)],
                         totals)

    def test_too_few_points_never_flagged(self):
        points = [_pt(250.0, "2026-11-09T10:00:00Z"),
                  _pt(709599.60, "2026-11-09T11:00:00Z")]
        self.assertEqual(len(sane_points(points)), 2)

    def test_zero_glitch_dropped(self):
        points = [_pt(250.0, f"2026-11-09T1{i}:00:00Z") for i in range(5)]
        points[2] = _pt(0.0, "2026-11-09T12:00:00Z")
        kept = sane_points(points)
        self.assertEqual([p["total_value_usd"] for p in kept],
                         [250.0] * 4)

    def test_non_finite_dropped(self):
        points = [_pt(250.0, f"2026-11-09T1{i}:00:00Z") for i in range(5)]
        points[2] = _pt(float("inf"), "2026-11-09T12:00:00Z")
        kept = sane_points(points)
        self.assertEqual([p["total_value_usd"] for p in kept],
                         [250.0] * 4)

    def test_order_preserved(self):
        totals = [250.0, 260.0, 709599.60, 255.0, 245.0]
        points = [_pt(t, f"2026-11-09T1{i}:00:00Z") for i, t in
                  enumerate(totals)]
        self.assertEqual([p["total_value_usd"] for p in sane_points(points)],
                         [250.0, 260.0, 255.0, 245.0])


class GlitchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.client = make_client(self.tmp.name)
        signup(self.client, "alice")
        self.db = self.app.config["DB"]
        self.user_id = self.db.execute(
            "SELECT id FROM users WHERE username = 'alice'"
        ).fetchone()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, total, created_at, cash=200.0, real_sol=None):
        self.db.execute(
            "INSERT INTO portfolio_snapshots (user_id, cash_usd, "
            "holdings_value_usd, total_value_usd, real_sol, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.user_id, cash, total - cash, total, real_sol,
             created_at))
        self.db.commit()

    def _day_of_normals(self, total=250.0, step_minutes=30,
                        offset_minutes=13):
        """~25h of normal snapshots on an offset grid, so no normal
        point sits on the 24h-ago mark."""
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        t = now - timedelta(hours=25, minutes=offset_minutes)
        while t <= now:
            self._insert(total, t.strftime(fmt))
            t += timedelta(minutes=step_minutes)


class TestChange24hGlitch(GlitchBase):
    def test_glitch_at_24h_mark_no_longer_poisons_headline(self):
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        self._day_of_normals()
        # The regression row: one $709,599.60 snapshot 60 seconds
        # inside the 24h window, nearest the mark. Pre-fix this was
        # the baseline: delta -709349.6, pct -99.96.
        self._insert(709599.60, (now - timedelta(hours=24)
                                 + timedelta(seconds=60)).strftime(fmt))
        result = change_24h(self.db, self.user_id, 250.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["delta"], 0.0, places=1)
        self.assertAlmostEqual(result["pct"], 0.0, places=1)

    def test_glitch_at_24h_mark_real_mode(self):
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        t = now - timedelta(hours=25, minutes=13)
        while t <= now:
            self._insert(250.0, t.strftime(fmt), real_sol=2.0)
            t += timedelta(minutes=30)
        self._insert(250.0, (now - timedelta(hours=24)
                             + timedelta(seconds=60)).strftime(fmt),
                     real_sol=20000.0)
        result = change_24h(self.db, self.user_id, 2.0, real=True)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["delta"], 0.0, places=1)
        self.assertAlmostEqual(result["pct"], 0.0, places=1)

    def test_chart_ignores_glitch_anywhere(self):
        self._day_of_normals()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        self._insert(709599.60,
                     (now - timedelta(hours=12)).strftime(fmt))
        points, _ = chart_points(self.db, self.user_id, "all")
        svg = chart_svg(sane_points(points))
        self.assertNotIn("$709,599.60", svg)
        self.assertIn("$250.00", svg)


class TestRecordSnapshotGuard(GlitchBase):
    def test_non_finite_total_refused_and_not_stored(self):
        with self.assertRaises(ValueError):
            record_snapshot(self.db, self.user_id, 200.0, float("inf"),
                            None, now="2026-11-09T12:00:00Z")
        with self.assertRaises(ValueError):
            record_snapshot(self.db, self.user_id, float("nan"), 50.0,
                            None, now="2026-11-09T12:00:00Z")
        rows = self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchall()
        self.assertEqual(len(rows), 0)

    def test_finite_snapshot_still_stored(self):
        record_snapshot(self.db, self.user_id, 200.0, 50.0, 2.0,
                        now="2026-11-09T12:00:00Z")
        row = self.db.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()
        self.assertAlmostEqual(row["total_value_usd"], 250.0)


class TestPurgeAbsurdSnapshots(GlitchBase):
    def test_dry_run_flags_only_the_glitch(self):
        self._day_of_normals()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        self._insert(709599.60,
                     (now - timedelta(hours=12)).strftime(fmt))
        flagged = purge_absurd_snapshots(self.db, dry_run=True)
        self.assertEqual(len(flagged), 1)
        self.assertAlmostEqual(flagged[0]["total_value_usd"], 709599.60)
        # Dry-run deletes nothing.
        count = self.db.execute(
            "SELECT COUNT(*) c FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]
        self.assertGreater(count, 40)

    def test_apply_deletes_only_the_glitch(self):
        self._day_of_normals()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        self._insert(709599.60,
                     (now - timedelta(hours=12)).strftime(fmt))
        before = self.db.execute(
            "SELECT COUNT(*) c FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]
        flagged = purge_absurd_snapshots(self.db, dry_run=False)
        self.assertEqual(len(flagged), 1)
        after = self.db.execute(
            "SELECT COUNT(*) c FROM portfolio_snapshots WHERE user_id = ?",
            (self.user_id,)).fetchone()["c"]
        self.assertEqual(after, before - 1)
        # Second run finds nothing left to clean.
        self.assertEqual(purge_absurd_snapshots(self.db, dry_run=True), [])
        # And the remaining totals are all the normal $250.
        totals = {r["total_value_usd"] for r in self.db.execute(
            "SELECT total_value_usd FROM portfolio_snapshots "
            "WHERE user_id = ?", (self.user_id,)).fetchall()}
        self.assertEqual(totals, {250.0})

    def test_purge_leaves_genuine_step_alone(self):
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        for i, total in enumerate([250.0, 250.0, 250.0,
                                   250000.0, 250000.0, 250000.0]):
            self._insert(total, (now - timedelta(hours=i)).strftime(fmt))
        self.assertEqual(purge_absurd_snapshots(self.db, dry_run=True), [])


class TestDashboardGlitch(GlitchBase):
    def test_home_chart_never_shows_glitch_value(self):
        self._day_of_normals()
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        self._insert(709599.60,
                     (now - timedelta(hours=12)).strftime(fmt))
        html = self.client.get("/wallet").get_data(as_text=True)
        self.assertNotIn("709,599", html)
        # The headline is computed from a normal baseline (a full
        # day of $250 snapshots surrounds the mark), so it must be
        # sane too — never the pre-fix -99.96%.
        self.assertNotIn("-99.96", html)


if __name__ == "__main__":
    unittest.main()
