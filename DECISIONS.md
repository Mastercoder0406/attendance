# DECISIONS.md

1. **Indexes.** On `attendance_logs`: unique `(emp_code, date)` (one record per day, race-safe punch-in, monthly and trend queries, `$lookup`); `(date desc, emp_code)` for the list sort and leaderboard date range; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` for finding the record to punch out. On `employees`: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)`. I rejected an index on `late_minutes`: the date range already narrows the scan.
2. **Punch-in race.** Both requests try one `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no read-then-write gap.
3. **Ties.** Ranks come from `$rank` before the limit is applied. Every row with `rank <= limit` is returned, so a tie at the cutoff returns more than `limit` rows.
4. **Headcount.** The summary starts from `employees` and does `$lookup` per person with `preserveNullAndEmptyArrays`, so someone with no logs still counts once.
5. **100x data.** Stop recomputing months from raw logs: keep a monthly roll-up per employee, updated on punch and correction.