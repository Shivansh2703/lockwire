"""Deterministic statistics for baseline profiling and the gate's envelope checks.

Authoritative design: `docs/stats_design.md` (locked 2026-07-12). Everything here is
order statistics, running sums, and counters — no ML, no distributional fitting, no
scipy. That is a scope fence, not an accident: the check path must stay deterministic
and auditable.

One module per concern, so parallel work never collides:
- wilks     distribution-free tolerance bounds (which order statistic, or under-sampled)
- neff      effective sample size after autocorrelation correction
- cusum     stuck-at / frozen-sensor detection
- drift     cubic residual separation and slow-drift CUSUM
- clusters  familywise error budget allocated across physical failure paths
"""
