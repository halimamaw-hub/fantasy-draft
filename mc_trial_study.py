"""
Diagnostic: how many MC rollouts does a candidate need in v12_fast_candidate_mc
before its sweep/podium probability estimate stops being noise?

Run from the repo root: python tools/mc_trial_study.py

Advances a draft to a realistic decision point (an alliance team on the
clock, several rounds in), then re-runs the MC estimator many times per
trial count to measure: how much the estimate bounces around at that trial
count (stdev across repeats), how far off it is from a much longer
reference run (bias/RMSE), and how often the *ranking* of the top-3
shortlist -- which is what actually drives the recommendation -- agrees
with the reference ranking.
"""
import engine, numpy as np, time

def main():
    tracker, weekly = engine.build_tracker(
        data_dir='.', my_slot=9, teams=10, rounds=14, reversal_round=3,
        alliance_allies=(6, 7), auto_recommend_teams=(), pool_size=engine.DEFAULT_POOL_SIZE)

    # Advance with best-consensus-available picks to a realistic decision
    # point: an alliance team on the clock, round 9+ (rosters mostly set,
    # which is when `top3`/`check` calls matter most in practice).
    while True:
        on_clock = tracker.pick_order[tracker.overall - 1]
        if on_clock in (9, 6, 7) and tracker.current_round >= 9:
            break
        if tracker.overall > len(tracker.pick_order):
            break
        taken = set(tracker._drafted_to_team)
        avail = tracker.pool[~tracker.pool['Player'].isin(taken)]
        tracker.record_pick(avail.sort_values('ADP').iloc[0]['Player'])

    team = tracker.pick_order[tracker.overall - 1]
    board = engine.v15_batch_top3(tracker, team, deadline=time.monotonic() + 5)
    names = board['Player'].tolist()[:3]
    print(f"Pick #{tracker.overall}, round {tracker.current_round}, team {team} (alliance)")
    print(f"Shortlist: {names}\n")

    trial_counts = [10, 20, 40, 80]
    REPEATS = 8
    REFERENCE_TRIALS = 500  # "ground truth" comparison point, not infinite but far steadier

    def one_run(player, n, seed):
        return engine.v12_fast_candidate_mc(
            tracker, team, player, min_trials=n, max_trials=n,
            batch=min(engine.V15_LIVE_BATCH, n), seed=seed, deadline=None)

    # --- Part 1: variance/bias of a single candidate's estimate ---
    candidate = names[0]
    print(f"--- Estimate stability for '{candidate}' ---")
    print(f"{'trials':>7} | {'mean podium2+':>13} | {'stdev':>7} | {'time/run(s)':>11}")
    per_n = {}
    for n in trial_counts:
        vals, t0 = [], time.time()
        for r in range(REPEATS):
            res = one_run(candidate, n, 1000 * r + 7)
            vals.append(res.get('mc_podium_2plus_prob', 0.0))
        vals = np.array(vals)
        per_n[n] = vals
        print(f"{n:>7} | {vals.mean():>13.3f} | {vals.std(ddof=1):>7.3f} | {(time.time()-t0)/REPEATS:>11.3f}")
    ref = one_run(candidate, REFERENCE_TRIALS, 424242)
    ref_p = ref.get('mc_podium_2plus_prob', 0.0)
    print(f"\nreference ({REFERENCE_TRIALS} trials): podium2+ = {ref_p:.3f}")
    for n in trial_counts:
        rmse = np.sqrt(np.mean((per_n[n] - ref_p) ** 2))
        print(f"  n={n:>4}: bias={per_n[n].mean()-ref_p:+.3f}  RMSE={rmse:.3f}")

    # --- Part 2: does the trial count change WHICH player wins? ---
    print(f"\n--- Ranking agreement across the shortlist {names} ---")
    def rank_with(n, seed):
        res = {p: one_run(p, n, seed + 7919 * i) for i, p in enumerate(names)}
        ranked = sorted(names, key=lambda p: (res[p].get('mc_podium_2plus_prob', 0),
                                               res[p]['mc_sweep_prob']), reverse=True)
        return ranked, res
    ref_rank, ref_res = rank_with(REFERENCE_TRIALS, 424242)
    print(f"Reference ({REFERENCE_TRIALS}-trial) ranking:", ref_rank,
          {p: round(ref_res[p].get('mc_podium_2plus_prob', 0), 3) for p in ref_rank})
    for n in trial_counts:
        agree = sum(rank_with(n, 2000 * r + 3)[0][0] == ref_rank[0] for r in range(REPEATS))
        print(f"  n={n:>4}: top pick matches reference top pick in {agree}/{REPEATS} repeats")

if __name__ == '__main__':
    main()
